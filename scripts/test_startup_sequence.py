#!/usr/bin/env python3
"""
Test the container startup sequence for both podman compose and container.mjs.

Tests:
  1. podman compose: Qdrant healthcheck gates zotero-rag startup (correct ordering)
  2. container.mjs start: waitForQdrant fires and resolves before main container starts
  3. container.mjs restart: waitForQdrant fires and resolves before main container restarts

Requirements: podman with a running machine, all images pulled locally.
Run with: uv run python scripts/test_startup_sequence.py
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

PASS = "[PASS]"
FAIL = "[FAIL]"
INFO = "[INFO]"

# Timeouts in seconds
COMPOSE_TIMEOUT = 120
CONTAINER_MJS_TIMEOUT = 120


def run(cmd: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        timeout=timeout,
    )


def require_podman() -> None:
    if not shutil.which("podman"):
        print(f"{FAIL} podman not found in PATH")
        sys.exit(1)


def compose(*args: str, timeout: int = COMPOSE_TIMEOUT) -> subprocess.CompletedProcess:
    env = {**os.environ, "DATA_DIR": str(PROJECT_ROOT / "data")}
    return subprocess.run(
        ["podman", "compose", *args],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        env=env,
        timeout=timeout,
    )


def mjs(*args: str, timeout: int = CONTAINER_MJS_TIMEOUT) -> subprocess.CompletedProcess:
    return run(["node", "bin/container.mjs", *args], timeout=timeout)


def compose_down() -> None:
    compose("down", "--remove-orphans", timeout=60)


def mjs_stop() -> None:
    mjs("stop", timeout=60)


# ---------------------------------------------------------------------------
# Test 1 — podman compose healthcheck ordering
# ---------------------------------------------------------------------------

def test_compose_healthcheck() -> bool:
    print(f"\n{INFO} Test 1: podman compose — Qdrant healthcheck gates zotero-rag startup")

    compose_down()

    result = compose("up", "-d")
    # podman compose routes status lines through stderr when using docker-compose backend
    output = result.stdout + result.stderr

    if result.returncode != 0:
        print(f"{FAIL} compose up failed (exit {result.returncode}):\n{output[-2000:]}")
        compose_down()
        return False

    # Ordering evidence in compose output:
    #   "Healthy"                            <- qdrant passed healthcheck
    #   "zotero-rag-zotero-rag-1  Starting"  <- main app then starts
    healthy_pos = output.find("Healthy")
    rag_start_pos = output.find("zotero-rag-1  Starting")

    if healthy_pos == -1:
        print(f"{FAIL} Qdrant never reached 'Healthy' state in compose output")
        compose_down()
        return False

    if rag_start_pos == -1:
        print(f"{FAIL} zotero-rag container start marker not found in compose output")
        compose_down()
        return False

    if healthy_pos >= rag_start_pos:
        print(
            f"{FAIL} zotero-rag started (pos {rag_start_pos}) "
            f"before or at same time as Qdrant became healthy (pos {healthy_pos})"
        )
        compose_down()
        return False

    # Verify final state
    ps = run(["podman", "ps", "--format", "{{.Names}}\t{{.Status}}"])
    running = ps.stdout
    if "(healthy)" not in running:
        print(f"{FAIL} Qdrant not showing (healthy) in final container list:\n{running}")
        compose_down()
        return False

    print(
        f"{PASS} Qdrant healthy (pos {healthy_pos}) before "
        f"zotero-rag started (pos {rag_start_pos})"
    )
    compose_down()
    return True


# ---------------------------------------------------------------------------
# Test 2 — container.mjs start
# ---------------------------------------------------------------------------

def test_container_mjs_start() -> bool:
    print(f"\n{INFO} Test 2: container.mjs start — waitForQdrant before main container")

    mjs_stop()

    result = mjs("start")
    output = result.stdout + result.stderr

    if result.returncode != 0:
        print(f"{FAIL} container.mjs start failed (exit {result.returncode}):\n{output[-2000:]}")
        mjs_stop()
        return False

    wait_pos = output.find("[INFO] Waiting for Qdrant")
    ready_pos = output.find("[INFO] Qdrant is ready")
    started_pos = output.find("[SUCCESS] Container started")

    missing = [
        name for name, pos in [
            ("[INFO] Waiting for Qdrant", wait_pos),
            ("[INFO] Qdrant is ready", ready_pos),
            ("[SUCCESS] Container started", started_pos),
        ]
        if pos == -1
    ]
    if missing:
        print(f"{FAIL} Missing expected log lines: {missing}\nOutput:\n{output[-2000:]}")
        mjs_stop()
        return False

    if not (wait_pos < ready_pos < started_pos):
        print(
            f"{FAIL} Wrong ordering — wait={wait_pos} ready={ready_pos} started={started_pos}"
        )
        mjs_stop()
        return False

    print(f"{PASS} Correct order: Waiting for Qdrant -> Qdrant is ready -> Container started")
    # Leave containers running for the restart test
    return True


# ---------------------------------------------------------------------------
# Test 3 — container.mjs restart
# ---------------------------------------------------------------------------

def test_container_mjs_restart() -> bool:
    print(f"\n{INFO} Test 3: container.mjs restart — waitForQdrant before main container restarts")

    result = mjs("restart")
    output = result.stdout + result.stderr

    if result.returncode != 0:
        print(f"{FAIL} container.mjs restart failed (exit {result.returncode}):\n{output[-2000:]}")
        mjs_stop()
        return False

    wait_pos = output.find("[INFO] Waiting for Qdrant")
    ready_pos = output.find("[INFO] Qdrant is ready")
    success_pos = output.find("[SUCCESS] Restarted")

    missing = [
        name for name, pos in [
            ("[INFO] Waiting for Qdrant", wait_pos),
            ("[INFO] Qdrant is ready", ready_pos),
            ("[SUCCESS] Restarted", success_pos),
        ]
        if pos == -1
    ]
    if missing:
        print(f"{FAIL} Missing expected log lines: {missing}\nOutput:\n{output[-2000:]}")
        mjs_stop()
        return False

    if not (wait_pos < ready_pos < success_pos):
        print(
            f"{FAIL} Wrong ordering — wait={wait_pos} ready={ready_pos} success={success_pos}"
        )
        mjs_stop()
        return False

    print(f"{PASS} Correct order: Waiting for Qdrant -> Qdrant is ready -> Restarted")
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    require_podman()

    print("Container Startup Sequence Tests")
    print("=" * 40)

    results: list[tuple[str, bool]] = []
    try:
        results.append(("podman compose — healthcheck ordering", test_compose_healthcheck()))
        results.append(("container.mjs start — waitForQdrant", test_container_mjs_start()))
        results.append(("container.mjs restart — waitForQdrant", test_container_mjs_restart()))
    finally:
        print(f"\n{INFO} Cleaning up...")
        mjs_stop()

    print("\nResults")
    print("=" * 40)
    all_pass = True
    for name, passed in results:
        status = PASS if passed else FAIL
        print(f"  {status} {name}")
        if not passed:
            all_pass = False

    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    main()
