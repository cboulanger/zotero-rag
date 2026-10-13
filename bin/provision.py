#!/usr/bin/env python3
"""Provision, recreate or tear down a preset's remote endpoints from the command line.

Works for every provider that supports provisioning (see docs/providers.md).
Each side (embedding, llm) is handled by the provider the preset names for it.

The API key is never taken from the environment or argv: it comes from the
shared remote-config store, or is typed at a hidden prompt (or piped on stdin).
A prompted key is used for this run only and is never stored.

Examples:
    uv run python bin/provision.py --preset runpod
    uv run python bin/provision.py --preset runpod --side llm --recreate
    uv run python bin/provision.py --preset runpod --teardown --yes
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path
from typing import Callable, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.config.presets import HardwarePreset, get_preset  # noqa: E402
from backend.config.settings import get_settings  # noqa: E402
from backend.providers import ProviderConfigError, get_providers  # noqa: E402
from backend.providers.types import ProvisionContext  # noqa: E402
from backend.services.admin_settings_store import resolve_shared_value, update_remote_config  # noqa: E402

SIDES = ("embedding", "llm")


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--preset", required=True, help="Preset name, e.g. runpod")
    parser.add_argument("--side", choices=SIDES, action="append", help="Only this side (repeatable)")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--pause", action="store_true", help="Stop billing, keep the endpoints (providers that support it)")
    action.add_argument("--teardown", action="store_true", help="Delete the remote resources")
    parser.add_argument("--recreate", action="store_true", help="Delete and recreate existing resources")
    parser.add_argument("--skip-warmup", action="store_true", help="Do not wait for the endpoint to answer a request")
    parser.add_argument("--yes", action="store_true", help="Do not ask for confirmation before destructive actions")
    return parser.parse_args(argv)


def _credential_env(preset: HardwarePreset, side: str) -> Optional[str]:
    kwargs = (preset.embedding if side == "embedding" else preset.llm).model_kwargs
    return kwargs.get("shared_api_key_env") or kwargs.get("api_key_env")


def _credential(preset: HardwarePreset, side: str, prompt: Callable[[str], str]) -> Optional[str]:
    """Stored shared key if there is one, else a hidden prompt (never stored)."""
    env = _credential_env(preset, side)
    if not env:
        return None
    return resolve_shared_value(env) or prompt(f"API key for {env} (used for this run only): ") or None


def _confirm(message: str, yes: bool) -> bool:
    return yes or input(f"{message} [y/N] ").strip().lower() == "y"


def main(argv: Optional[Sequence[str]] = None, prompt: Callable[[str], str] = getpass.getpass) -> int:
    args = _parse_args(argv)
    try:
        preset = get_preset(args.preset)
        providers = get_providers(preset)
    except (ValueError, ProviderConfigError) as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 2

    sides = [s for s in SIDES if not args.side or s in args.side]
    sides = [s for s in sides if providers[s].supports_provisioning]
    if not sides:
        print(f"[FAIL] Preset '{args.preset}' has no side that can be provisioned.", file=sys.stderr)
        return 2
    if (args.teardown or args.recreate) and not _confirm(
        f"This deletes the remote resources of the {', '.join(sides)} side(s). Continue?", args.yes
    ):
        print("Aborted.")
        return 1

    data_path = get_settings().data_path
    failed = False
    for side in sides:
        provider = providers[side]
        ctx = ProvisionContext(
            side=side, preset=preset, credential=_credential(preset, side, prompt),
            data_path=data_path, recreate=args.recreate, skip_warmup=args.skip_warmup,
        )

        def progress(message: str, _side: str = side) -> None:
            print(f"[{_side}] {message}")

        try:
            if args.teardown:
                provider.teardown(ctx)
                print(f"[{side}] torn down")
            elif args.pause:
                provider.suspend(ctx, progress)
                print(f"[{side}] paused")
            else:
                values = provider.provision(ctx, progress)
                if values:
                    update_remote_config(values, data_path=data_path)
                print(f"[PASS] {side} ready")
        except Exception as exc:  # report per side, keep going
            failed = True
            print(f"[FAIL] {side}: {exc}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
