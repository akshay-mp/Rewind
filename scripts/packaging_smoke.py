#!/usr/bin/env python3
"""Build and smoke-test Agent Timetravel's distributable artifacts.

The smoke test deliberately runs the installed wheel outside the repository so
the UI check cannot accidentally use ``web/dist`` from the checkout.
"""

from __future__ import annotations

import argparse
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import venv
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_PRIVATE_PLANNING_FILES = {
    "docs/debugger-roadmap.md",
    "docs/implementation_plan.md",
}
_EXCLUDED_ARCHIVE_PARTS = {
    ".pnpm-store",
    "artifacts",
    "node_modules",
    ".next",
    "__pycache__",
}
_EXCLUDED_ARCHIVE_SUFFIXES = {".pyc", ".pyo", ".pyd", ".tsbuildinfo"}


def _run(command: list[str], *, cwd: Path = REPO_ROOT) -> None:
    print("+", " ".join(command))
    subprocess.run(command, cwd=cwd, check=True)  # noqa: S603


def _archive_has_env_file(name: str) -> bool:
    return any(part == ".env" or part.startswith(".env.") for part in Path(name).parts)


def _archive_has_private_planning_file(name: str) -> bool:
    return any(name == path or name.endswith(f"/{path}") for path in _PRIVATE_PLANNING_FILES)


def _archive_has_excluded_local_path(name: str) -> bool:
    path = Path(name)
    return (
        any(part in _EXCLUDED_ARCHIVE_PARTS for part in path.parts)
        or path.suffix in _EXCLUDED_ARCHIVE_SUFFIXES
    )


def _wheel_members(wheel: Path) -> list[str]:
    with zipfile.ZipFile(wheel) as archive:
        members = archive.namelist()
    assert "agent_timetravel/_ui/index.html" in members, (
        "wheel is missing agent_timetravel/_ui/index.html"
    )
    assert any(name.startswith("agent_timetravel/_ui/assets/") for name in members)
    assert not any(_archive_has_env_file(name) for name in members), (
        "wheel contains an environment file"
    )
    assert not any(_archive_has_private_planning_file(name) for name in members), (
        "wheel contains a private planning file"
    )
    assert not any(_archive_has_excluded_local_path(name) for name in members), (
        "wheel contains an excluded local/generated path"
    )
    return members


def _sdist_members(sdist: Path) -> list[str]:
    with tarfile.open(sdist, "r:gz") as archive:
        members = archive.getnames()
    assert any(name.endswith("/web/dist/index.html") for name in members), (
        "sdist is missing web/dist/index.html"
    )
    assert any("/web/dist/assets/" in name for name in members)
    assert not any(_archive_has_env_file(name) for name in members), (
        "sdist contains an environment file"
    )
    assert not any(_archive_has_private_planning_file(name) for name in members), (
        "sdist contains a private planning file"
    )
    assert not any(_archive_has_excluded_local_path(name) for name in members), (
        "sdist contains an excluded local/generated path"
    )
    return members


def _build_artifacts(output: Path) -> tuple[Path, Path]:
    output.mkdir(parents=True, exist_ok=True)
    _run([sys.executable, "-m", "build", "--outdir", str(output)])
    wheels = sorted(output.glob("*.whl"))
    sdists = sorted(output.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise RuntimeError(f"expected one wheel and sdist in {output}")
    return wheels[0], sdists[0]


def _check_wheel_from_sdist(sdist: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="agent-timetravel-sdist-") as temp:
        unpacked = Path(temp) / "source"
        unpacked.mkdir()
        with tarfile.open(sdist, "r:gz") as archive:
            archive.extractall(unpacked, filter="data")
        source_dir = next(unpacked.iterdir())
        output = Path(temp) / "wheel"
        _run(
            [
                sys.executable,
                "-m",
                "build",
                "--wheel",
                "--outdir",
                str(output),
            ],
            cwd=source_dir,
        )
        wheel = next(output.glob("*.whl"))
        members = _wheel_members(wheel)
        print(f"sdist-built wheel: {wheel.name} ({len(members)} members)")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_health(url: str, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = process.stdout.read() if process.stdout else ""
            raise RuntimeError(f"installed CLI exited early:\n{output}")
        try:
            with urllib.request.urlopen(url, timeout=1) as response:  # noqa: S310
                if response.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError):
            time.sleep(0.2)
    raise RuntimeError(f"installed CLI did not become healthy: {url}")


def _smoke_installed_wheel(wheel: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="agent-timetravel-wheel-smoke-", dir="/tmp") as temp:
        temp_dir = Path(temp)
        venv_dir = temp_dir / "venv"
        venv.EnvBuilder(with_pip=True, clear=True).create(venv_dir)
        python = venv_dir / "bin" / "python"
        executable = venv_dir / "bin" / "agent-timetravel"
        _run([str(python), "-m", "pip", "install", "--quiet", str(wheel)])

        imported = subprocess.run(  # noqa: S603
            [str(python), "-c", "import agent_timetravel; print(agent_timetravel.__version__)"],
            cwd=temp_dir,
            check=True,
            capture_output=True,
            text=True,
        )
        if not imported.stdout.strip():
            raise RuntimeError("installed agent_timetravel import returned no version")
        _run([str(executable), "--help"], cwd=temp_dir)

        port = _free_port()
        process = subprocess.Popen(  # noqa: S603
            [
                str(executable),
                "ui",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--otlp-port",
                str(port),
                "--db",
                str(temp_dir / "agent_timetravel.db"),
            ],
            cwd=temp_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            base = f"http://127.0.0.1:{port}"
            _wait_for_health(f"{base}/healthz", process)
            with urllib.request.urlopen(f"{base}/ui/", timeout=5) as response:  # noqa: S310
                html = response.read().decode("utf-8")
            if response.status != 200 or "Agent Timetravel" not in html or "/ui/assets/" not in html:
                raise RuntimeError("installed wheel did not serve the packaged UI")
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--artifacts",
        type=Path,
        default=REPO_ROOT / "dist",
        help="directory for build artifacts (default: ./dist)",
    )
    args = parser.parse_args()
    output = args.artifacts if args.artifacts.is_absolute() else REPO_ROOT / args.artifacts
    if output.exists():
        shutil.rmtree(output)
    wheel, sdist = _build_artifacts(output)
    print(f"wheel: {wheel.name} ({len(_wheel_members(wheel))} members)")
    print(f"sdist: {sdist.name} ({len(_sdist_members(sdist))} members)")
    _check_wheel_from_sdist(sdist)
    _smoke_installed_wheel(wheel)
    print("packaging smoke test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
