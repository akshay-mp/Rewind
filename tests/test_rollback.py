"""Unit tests for :mod:`rewind.rollback.git` (Phase 4).

Uses a real git repo under ``tmp_path`` so the round-trip (stash on branch,
pop on rewind) is end-to-end. We also test the failure paths via the
injectable ``runner`` parameter (â€”substitute mock subprocess.run).

Why a real repo and not a mock throughout? ``git stash`` semantics are
subtle (index vs working tree, sentinel for empty tree, multiple stash
entries), and mocking the entire CLI surface would just be re-implementing
git in Python. A tmp repo is fast (~5ms per test) and exercises the
branch_id naming scheme in situ.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from rewind.rollback import RollbackError, RollbackHandler
from rewind.rollback.git import GitRollbackHandler


# ----------------------------------------------------------------------
# Test repo fixture
# ----------------------------------------------------------------------
@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A tmp git repo with one committed file. The handler operates here."""
    repo = tmp_path / "repo"
    repo.mkdir()
    # git needs an identity for stash create/store
    _run(repo, "git", "init")
    _run(repo, "git", "config", "user.email", "test@rewind.local")
    _run(repo, "git", "config", "user.name", "Rewind Test")
    (repo / "README.md").write_text("# initial\n")
    _run(repo, "git", "add", "README.md")
    _run(repo, "git", "commit", "-m", "initial")
    return repo


def _run(cwd: Path, *cmd: str) -> str:
    """Run a command, return stdout, fail loud.

    S603 ("untrusted subprocess input") is silenced here because ``cmd`` is
    hard-coded by the test (no user input flows into it). This is a test
    fixture, not a production execution path.
    """
    result = subprocess.run(  # noqa: S603 - test fixture with hard-coded command
        cmd, cwd=cwd, capture_output=True, text=True, check=True
    )
    return (result.stdout or "").strip()


# ----------------------------------------------------------------------
# Protocol conformance
# ----------------------------------------------------------------------
def test_git_handler_satisfies_protocol(git_repo: Path) -> None:
    """``GitRollbackHandler`` structurally implements :class:`RollbackHandler`."""
    handler = GitRollbackHandler(repo_path=str(git_repo))
    assert isinstance(handler, RollbackHandler)


# ----------------------------------------------------------------------
# Happy path round-trip
# ----------------------------------------------------------------------
def test_on_branch_then_on_rewind_restores_files(
    git_repo: Path, tmp_path: Path
) -> None:
    """After branch + writes + rewind, the working tree is restored exactly."""
    handler = GitRollbackHandler(repo_path=str(git_repo))
    branch_id = uuid4()

    # Snapshot pristine state.
    handler.on_branch(branch_id)

    # Agent mutates the working tree.
    (git_repo / "README.md").write_text("# CHANGED BY AGENT\n")
    (git_repo / "new_file.txt").write_text("agent wrote this\n")
    _run(git_repo, "git", "add", "-A")  # agent staged stuff too

    # Sanity: at this point the working tree is dirty.
    diff = _run(git_repo, "git", "status", "--porcelain")
    assert "README.md" in diff

    # Rewind: the handler should restore the pristine state.
    handler.on_rewind(branch_id)

    # README.md content is back to the initial state.
    assert (git_repo / "README.md").read_text() == "# initial\n"
    # The new file added during the branch is GONE.
    assert not (git_repo / "new_file.txt").exists()


def test_on_rewind_unknown_branch_is_noop(git_repo: Path) -> None:
    """Rewinding a branch_id we never snapshotted is a no-op (protocol)."""
    handler = GitRollbackHandler(repo_path=str(git_repo))
    # No exception expected — this is the documented idempotency contract.
    handler.on_rewind(uuid4())


def test_on_branch_is_idempotent(git_repo: Path) -> None:
    """Calling on_branch twice on the same branch_id refreshes the snapshot."""
    handler = GitRollbackHandler(repo_path=str(git_repo))
    bid = uuid4()
    handler.on_branch(bid)

    # Mutate, then re-branch (the protocol says this is a refresh).
    (git_repo / "README.md").write_text("# second snapshot\n")
    handler.on_branch(bid)  # should replace, not stack, the stash entry

    # Now restore — expect the SECOND snapshot to be served.
    handler.on_rewind(bid)
    assert (git_repo / "README.md").read_text() == "# second snapshot\n"


def test_on_branch_empty_working_tree_is_safe(git_repo: Path) -> None:
    """When the working tree is clean (no delta), branch/rewind are no-ops.

    ``git stash create`` returns empty for a clean tree; the handler skips
    the stash-store step and rewind finds nothing to pop. This is the
    correct behaviour (nothing to restore).
    """
    handler = GitRollbackHandler(repo_path=str(git_repo))
    bid = uuid4()
    handler.on_branch(bid)  # clean tree — nothing to snapshot
    handler.on_rewind(bid)  # no-op


# ----------------------------------------------------------------------
# Failure paths
# ----------------------------------------------------------------------
def test_handler_raises_when_not_a_git_repo(tmp_path: Path) -> None:
    """Shelling out outside any git repo raises RollbackError.

    On a non-repo directory, ``git stash create`` errors with "not a git
    repository". The handler converts this to ``RollbackError`` so the
    agent loop sees a uniform failure.
    """
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()
    handler = GitRollbackHandler(repo_path=str(not_a_repo))
    with pytest.raises(RollbackError, match="failed"):
        handler.on_branch(uuid4())


def test_handler_raises_with_helpful_message_when_git_missing(
    git_repo: Path,
) -> None:
    """FileNotFoundError on the git binary is converted to RollbackError.

    We inject a ``runner`` that simulates git missing from PATH. The
    blame-message must say "git binary not found" so users can self-diagnose.
    """
    bid = uuid4()

    def fake_runner_missing(*args: object, **kwargs: object) -> object:
        raise FileNotFoundError(args[0])

    handler = GitRollbackHandler(
        repo_path=str(git_repo), runner=fake_runner_missing  # type: ignore[arg-type]
    )
    with pytest.raises(RollbackError, match="git binary not found"):
        handler.on_branch(bid)


# ----------------------------------------------------------------------
# Naming / tagging invariant
# ----------------------------------------------------------------------
def test_stash_entries_get_rewind_tag(git_repo: Path) -> None:
    """A pre-branch uncommitted (tracked) change produces a stash w/ the tag.

    Anchor strategy (Phase 4): if the working tree has uncommitted tracked
    changes at branch time, the handler stashes them under a
    ``rewind-branch-<bid.hex>`` message so ``on_rewind`` can pop them back.
    Untracked files require explicit staging to be considered part of the
    delta (matches default ``git stash`` semantics — untracked is the
    agent's own concern).
    """
    handler = GitRollbackHandler(repo_path=str(git_repo))
    bid = uuid4()
    (git_repo / "scratch.txt").write_text("dirty\n")
    _run(git_repo, "git", "add", "scratch.txt")  # stage so stash catches it
    handler.on_branch(bid)

    listing = _run(git_repo, "git", "stash", "list")
    assert listing  # at least one entry
    assert any(f"rewind-branch-{bid.hex}" in line for line in listing.splitlines())

    # Cleanup: pop the stash so tmp_path teardown is clean.
    handler.on_rewind(bid)


# Silence "imported but unused" if a future check adds UUID back.
_ = UUID
