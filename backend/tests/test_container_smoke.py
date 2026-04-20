"""
Container smoke test.

Builds the local Dockerfile, starts all services via podman/docker compose,
and verifies that the API stack responds correctly.

Requires: podman or docker with compose support. Skipped if neither is found.
Run with: uv run pytest -m container -v -s
"""

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Generator

import httpx
import pytest

pytestmark = pytest.mark.container

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SMOKE_COMPOSE = PROJECT_ROOT / "docker-compose.smoke.yml"
SMOKE_IMAGE = "zotero-rag:smoke-test"
SMOKE_PORT = 18119
BASE_URL = f"http://localhost:{SMOKE_PORT}"
STARTUP_TIMEOUT = 210  # seconds; includes up to 5×5s VectorStore retries + image pull time


def _find_runtime() -> str | None:
    for rt in ("podman", "docker"):
        if shutil.which(rt):
            return rt
    return None


def _compose_cmd(runtime: str) -> list[str]:
    if runtime == "podman":
        return ["podman", "compose"]
    # Docker v2 ships 'docker compose' (plugin); fall back to standalone 'docker-compose'
    result = subprocess.run(["docker", "compose", "version"], capture_output=True)
    if result.returncode == 0:
        return ["docker", "compose"]
    return ["docker-compose"]


@pytest.fixture(scope="module")
def runtime() -> str:
    rt = _find_runtime()
    if rt is None:
        pytest.skip("No container runtime (podman/docker) found")
    return rt


@pytest.fixture(scope="module")
def built_image(runtime: str) -> Generator[str, None, None]:
    result = subprocess.run(
        [runtime, "build", "-t", SMOKE_IMAGE, "."],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"Image build failed (exit {result.returncode}):\n"
        f"STDOUT:\n{result.stdout[-3000:]}\n"
        f"STDERR:\n{result.stderr[-3000:]}"
    )
    yield SMOKE_IMAGE
    subprocess.run([runtime, "rmi", SMOKE_IMAGE], check=False, capture_output=True)


@pytest.fixture(scope="module")
def stack(runtime: str, built_image: str) -> Generator[str, None, None]:
    compose = [*_compose_cmd(runtime), "-f", str(SMOKE_COMPOSE)]
    env = {
        **os.environ,
        "ZOTERO_RAG_IMAGE": built_image,
        "BACKEND_PORT": str(SMOKE_PORT),
    }

    # Ensure a clean slate before starting
    subprocess.run(
        [*compose, "down", "--remove-orphans"],
        cwd=PROJECT_ROOT, env=env, check=False, capture_output=True,
    )

    up = subprocess.run(
        [*compose, "up", "-d"],
        cwd=PROJECT_ROOT, env=env, capture_output=True, text=True,
    )
    assert up.returncode == 0, f"compose up failed:\n{up.stderr}"

    # Wait for both the HTTP layer (health) and vector store (libraries) to be ready.
    # /health returns 200 as soon as FastAPI starts; /api/libraries only returns 200
    # once the VectorStore has successfully connected to Qdrant.
    deadline = time.monotonic() + STARTUP_TIMEOUT
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            health = httpx.get(f"{BASE_URL}/health", timeout=5.0)
            if health.status_code == 200:
                # /api/libraries/indexed returns 200 (empty list) when VectorStore
                # is initialised, and 503 while it's still starting up.
                libs = httpx.get(f"{BASE_URL}/api/libraries/indexed", timeout=5.0)
                if libs.status_code == 200:
                    break
        except Exception as exc:
            last_err = exc
        time.sleep(3)
    else:
        logs = subprocess.run(
            [*compose, "logs", "--tail=100"],
            cwd=PROJECT_ROOT, env=env, capture_output=True, text=True,
        )
        subprocess.run([*compose, "down"], cwd=PROJECT_ROOT, env=env, check=False, capture_output=True)
        pytest.fail(
            f"Stack did not become healthy within {STARTUP_TIMEOUT}s. "
            f"Last error: {last_err}\nContainer logs:\n{logs.stdout}"
        )

    yield BASE_URL

    subprocess.run(
        [*compose, "down", "--remove-orphans"],
        cwd=PROJECT_ROOT, env=env, check=False, capture_output=True,
    )


def test_health(stack: str) -> None:
    """Health endpoint returns HTTP 200 with status=healthy."""
    resp = httpx.get(f"{stack}/health", timeout=10.0)
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


def test_root(stack: str) -> None:
    """Root endpoint returns service info with correct service name and running status."""
    resp = httpx.get(f"{stack}/", timeout=10.0)
    assert resp.status_code == 200
    data = resp.json()
    assert data["service"] == "Zotero RAG API"
    assert data["status"] == "running"


def test_version(stack: str) -> None:
    """Version endpoint returns a non-empty version string."""
    resp = httpx.get(f"{stack}/api/version", timeout=10.0)
    assert resp.status_code == 200
    assert resp.text.strip()


def test_indexed_libraries(stack: str) -> None:
    """Indexed-libraries endpoint returns 200 with an empty list on a fresh stack."""
    resp = httpx.get(f"{stack}/api/libraries/indexed", timeout=10.0)
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
