"""Backend plugin: the MPCDF LLM Inference Service provider.

Every side runs as a Slurm job on the calling user's own MPCDF account, started
from a login (username, password, one-time code) in the plugin's account card.
The per-user sessions are opened when the server starts and closed when it stops.
Needs the optional ``mpcdf-llm`` library; without it the provider stays
registered and says why it cannot start endpoints.
"""

from pathlib import Path

from backend.pluginhost import BackendPlugin, InferenceProvider, PresetDirectory

from . import sessions
from .provider import MpcdfProvider

_opened = None


async def _startup() -> None:
    global _opened
    if not sessions.available():
        return
    _opened = sessions.get_sessions()
    await _opened.open()


async def _shutdown() -> None:
    global _opened
    if _opened is not None:
        await _opened.close()
        _opened = None


def register() -> BackendPlugin:
    return BackendPlugin(
        id="mpcdf",
        name="MPCDF LLM Inference Service",
        version="1.0",
        description="Endpoints as Slurm jobs on each user's own MPCDF account (login with one-time code).",
        capabilities=[
            InferenceProvider(provider=MpcdfProvider),
            PresetDirectory(id="mpcdf", path=Path(__file__).parent / "presets"),
        ],
        on_startup=_startup,
        on_shutdown=_shutdown,
    )
