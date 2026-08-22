"""Safe workspace boundary for explicitly managed Git worktrees."""

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol


class WorkspaceExecutor(Protocol):
    def __call__(self, argv: Sequence[str], *, cwd: str) -> tuple[int, str, str]:
        """Run an argv array in cwd."""


class WorkspaceSafetyError(ValueError):
    """Raised when a path is outside the explicitly managed roots."""


class GitWorktreeWorkspaceProvider:
    """Inspect and diff managed worktrees without mutating caller checkouts."""

    def __init__(
        self, managed_paths: Sequence[str | Path], executor: WorkspaceExecutor | None = None
    ) -> None:
        self._managed = tuple(Path(path).resolve() for path in managed_paths)
        if not self._managed:
            raise WorkspaceSafetyError("at least one explicitly managed path is required")
        self._executor = executor

    def validate(self, path: str | Path) -> Path:
        candidate = Path(path).resolve()
        if not any(candidate == root or root in candidate.parents for root in self._managed):
            raise WorkspaceSafetyError(f"workspace is not managed: {candidate}")
        if not candidate.exists() or not candidate.is_dir():
            raise WorkspaceSafetyError(f"workspace directory does not exist: {candidate}")
        return candidate

    def _run(self, argv: Sequence[str], cwd: Path) -> tuple[int, str, str]:
        if self._executor is not None:
            return self._executor(argv, cwd=str(cwd))
        completed = subprocess.run(  # noqa: S603 - argv is explicit and shell=False
            argv, cwd=cwd, capture_output=True, text=True, check=False, shell=False
        )
        return completed.returncode, completed.stdout, completed.stderr

    def snapshot(
        self, path: str | Path, *, run_id: str = "", attempt_id: str | None = None
    ) -> dict[str, object]:
        workspace = self.validate(path)
        code, output, error = self._run(("git", "diff", "--no-ext-diff", "--binary"), workspace)
        if code != 0:
            raise RuntimeError(error or "git diff failed")
        return {
            "run_id": run_id,
            "attempt_id": attempt_id,
            "path": str(workspace),
            "tree_hash": hashlib.sha256(output.encode()).hexdigest(),
            "diff": output,
        }

    def diff(self, path: str | Path) -> str:
        workspace = self.validate(path)
        code, output, error = self._run(("git", "diff", "--no-ext-diff", "--binary"), workspace)
        if code != 0:
            raise RuntimeError(error or "git diff failed")
        return output

    def promote_artifact(self, path: str | Path) -> dict[str, str]:
        """Return a patch artifact; this method never applies it."""
        workspace = self.validate(path)
        return {"path": str(workspace), "patch": self.diff(workspace), "applied": "false"}


__all__ = ["GitWorktreeWorkspaceProvider", "WorkspaceSafetyError"]
