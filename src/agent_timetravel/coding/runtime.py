"""Container runtime boundary for autonomous coding execution."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class RuntimeUnavailableError(RuntimeError):
    """Raised when autonomous execution cannot use the configured runtime."""


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    image: str = "python:3.11-slim"
    workspace: str = "/workspace"
    network_disabled: bool = True
    env_allowlist: tuple[str, ...] = ()
    memory_mb: int = 2048
    cpu_count: float = 2.0
    timeout_seconds: int = 1800
    output_limit_bytes: int = 1_000_000
    host_mounts: tuple[str, ...] = ()


class ContainerExecutor(Protocol):
    def __call__(self, argv: Sequence[str], *, timeout: int) -> tuple[int, str, str]:
        """Run argv and return exit status, stdout, stderr."""


class DockerContainerRuntime:
    """Production boundary around Docker; it never falls back to local shell."""

    def __init__(
        self, config: RuntimeConfig | None = None, executor: ContainerExecutor | None = None
    ) -> None:
        self.config = config or RuntimeConfig()
        self._executor = executor

    def available(self) -> bool:
        """Report Docker availability without attempting a host command."""
        return self._executor is not None or shutil.which("docker") is not None

    def run(
        self, command: Sequence[str], *, workspace: str | Path | None = None
    ) -> tuple[int, str, str]:
        """Run an argv command in Docker with a closed-by-default policy."""
        if not self.available():
            raise RuntimeUnavailableError(
                "Docker is unavailable; autonomous execution is fail-closed"
            )
        if not command or any("\x00" in part for part in command):
            raise ValueError("command must be a non-empty argv array")
        mount = str(Path(workspace).resolve()) if workspace is not None else None
        if mount is None:
            raise ValueError("an explicitly managed workspace is required")
        argv = ["docker", "run", "--rm", "--network", "none", "--workdir", self.config.workspace]
        argv.extend(["--memory", f"{self.config.memory_mb}m", "--cpus", str(self.config.cpu_count)])
        for key in self.config.env_allowlist:
            argv.extend(["--env", key])
        argv.extend(["--mount", f"type=bind,src={mount},dst={self.config.workspace}"])
        argv.extend([self.config.image, *command])
        if self._executor is not None:
            return self._executor(argv, timeout=self.config.timeout_seconds)
        completed = subprocess.run(  # noqa: S603 - argv is explicit and shell=False
            argv,
            capture_output=True,
            text=True,
            timeout=self.config.timeout_seconds,
            check=False,
            shell=False,
        )
        return (
            completed.returncode,
            completed.stdout[: self.config.output_limit_bytes],
            completed.stderr[: self.config.output_limit_bytes],
        )


__all__ = ["DockerContainerRuntime", "RuntimeConfig", "RuntimeUnavailableError"]
