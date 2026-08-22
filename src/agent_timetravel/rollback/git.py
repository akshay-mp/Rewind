"""Reference :class:`RollbackHandler` backed by ``git``.

Targeted use case: agents whose side effects land in a working tree — file
mutators (``apply_patch``), MCP filesystem servers, code-editing tools
(Cursor / Aider / Devin-style).

**Snapshot strategy.** At :meth:`on_branch` time we capture two pieces
of state:

1. The current ``HEAD`` commit (the **anchor**). Anything the agent
   commits during the branch can be undone with ``git reset --hard <anchor>``.
2. Any uncommitted working-tree/index changes (the **delta**) via
   ``git stash``. These are popped back on timetravel.

On :meth:`on_timetravel` we run, in order: ``git reset --hard <anchor>``
(timetravel committed state), ``git clean -fd`` (drop untracked files the
agent wrote), and finally ``git stash pop`` if a delta was captured
(restore pre-branch uncommitted changes).

This dual strategy matters because most code-editing agents **commit**
their work (the natural unit of progress in a git-backed workspace), not
just leave it in the working tree. A stash-only handler would miss
those writes.

**Why stash and not a tag/branch ref?** Stashes are visible in
``git stash list`` and namespace cleanly via the message prefix; a stray
ref on every branch would clutter the reflog. ``git reset --hard``
onto a remembered commit SHA is the canonical "undo the agent's commits"
operation and works even when the agent has rewritten history.

**Security note (see Phase 4 threat model):** this shells out to ``git``.
The branch id is a UUID, never user input. We therefore avoid ``shell=True``
and pass the args list directly to :class:`subprocess.Popen`. The git CLI
is treated as a trusted binary; users who pin TimeTravel behind a sandbox
should additionally mount the working tree read-only except through this
handler.

Why not ``git worktree``? Worktrees are heavyweight and conflict with
agents that assume a single CWD. The anchor+stash approach stays in-place;
users who want worktrees can implement their own :class:`RollbackHandler`.
"""

from __future__ import annotations

# subprocess is required for git invocations; all calls use shell=False and
# trust only branch_id UUID hex values, never user input. See security
# review in docs/phases/phase-4.md.
import subprocess  # nosec B404 - subprocess is required for git invocations;
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from agent_timetravel.rollback.base import RollbackError

#: Tag prefix TimeTravel stamps onto every stash entry. Lets ``git stash list``
#: show TimeTravel-managed stashes distinctly from user stashes; also scopes
#: ``git stash drop`` to entries we own.
_REWIND_STASH_PREFIX = "timetravel-branch-"

#: Maximum wait for a single git invocation, in seconds. Git stash is fast
#: even on large trees; if it takes this long something is wrong (interactive
#: hook? NFS hang?). Aborting is safer than hanging the agent loop.
_GIT_TIMEOUT_SECONDS = 30


def _tag_for(branch_id: UUID) -> str:
    """Return the stable stash tag for ``branch_id``."""
    return f"{_REWIND_STASH_PREFIX}{branch_id.hex}"


@dataclass(slots=True)
class GitRollbackHandler:
    """A :class:`RollbackHandler` that stashes/pops a git working tree.

    The handler has no Python-level state across branch/timetravel pairs: the
    state lives in ``git stash list`` keyed by the branch tag. The class
    is a dataclass only so it can carry ``repo_path`` and the injectable
    ``runner`` (which defaults to :func:`subprocess.run`).

    Args:
        repo_path: Root of the git working tree this handler manages. The
            directory must be inside a git repo (it need not be the repo
            root). ``None`` means "use the process CWD".
        runner: Optional callable invoked in place of :func:`subprocess.run`.
            Test seams pass a mock here; production code leaves it default.
    """

    repo_path: str | None = None
    # ``Any`` here is intentional: ``subprocess.run`` has overloads that do
    # not narrow to ``CompletedProcess[str]`` cleanly under ``--strict``; we
    # invoke it with the union of kwargs and trust the unit tests to pin
    # behaviour, not the type system.
    runner: Any = field(default=subprocess.run, repr=False)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _git(self, *args: str) -> str:
        """Run ``git`` with ``args`` and return stdout (raises RollbackError).

        ``args`` are passed directly (no shell). A non-zero exit code is
        converted to :class:`RollbackError`; the original stderr is included.

        Args:
            *args: CLI flags/values, e.g. ``("stash", "list")``.

        Returns:
            stdout on success (stripped of trailing whitespace).

        Raises:
            RollbackError: if git is absent, the command times out, or
                returns a non-zero exit code.
        """
        cmd = ["git", *args]
        try:
            result = self.runner(
                cmd,
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                check=False,
                timeout=_GIT_TIMEOUT_SECONDS,
            )
        except FileNotFoundError as exc:
            raise RollbackError(
                "git binary not found on PATH; GitRollbackHandler requires git"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise RollbackError(
                f"git {' '.join(args)} timed out after "
                f"{_GIT_TIMEOUT_SECONDS}s in {self.repo_path or '<cwd>'}"
            ) from exc

        if result.returncode != 0:
            raise RollbackError(
                f"git {' '.join(args)} failed (rc={result.returncode}): "
                f"{result.stderr.strip() or '<no stderr>'}"
            )
        return (result.stdout or "").strip()

    def _git_or_none(self, *args: str) -> str | None:
        """Like :meth:`_git` but returns ``None`` on a non-zero exit.

        Used for lookups where a missing ref/entry is part of normal flow
        (e.g. verifying an anchor ref during :meth:`on_timetravel` — the ref
        may legitimately not exist for an unknown branch_id). All other
        failures (timeout, git missing) still raise.
        """
        cmd = ["git", *args]
        try:
            result = self.runner(
                cmd,
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                check=False,
                timeout=_GIT_TIMEOUT_SECONDS,
            )
        except FileNotFoundError as exc:
            raise RollbackError(
                "git binary not found on PATH; GitRollbackHandler requires git"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise RollbackError(
                f"git {' '.join(args)} timed out after "
                f"{_GIT_TIMEOUT_SECONDS}s in {self.repo_path or '<cwd>'}"
            ) from exc

        if result.returncode != 0:
            return None
        return (result.stdout or "").strip() or None

    # ------------------------------------------------------------------
    # RollbackHandler protocol
    # ------------------------------------------------------------------
    def on_branch(self, branch_id: UUID) -> None:
        """Snapshot ``HEAD`` + any uncommitted delta, tagged ``branch_id``.

        Captures two pieces of state the handler needs to restore on
        timetravel: the current HEAD commit (the *anchor*) and any uncommitted
        working-tree changes (the *delta*). The delta is stashed under a
        ``timetravel-branch-<branch_id.hex>`` message tag so it can be located
        later by message prefix.

        Idempotent on the tag — if a stash already exists for this branch_id
        it is *replaced* (a re-branch refreshes both anchor and delta). This
        matches the protocol's idempotency contract.

        Raises:
            RollbackError: if the working tree is not in a git repo, the
                repo has no commits yet (no anchor to capture), or git fails
                for any other reason.
        """
        tag = _tag_for(branch_id)
        # Drop any existing stash for this branch_id (idempotent refresh).
        self._drop_existing(tag)

        # Capture HEAD as the commit anchor. ``rev-parse HEAD`` is the
        # only state on timetravel that's strictly required — even if the
        # agent commits its writes, ``git reset --hard <anchor>`` undoes
        # them all in one step.
        anchor = self._git("rev-parse", "HEAD")
        # Persist the anchor alongside the stash entry. We do this by
        # reading a small ref that ``on_timetravel`` looks up. Using a real
        # git ref (not a sidecar state file) keeps state inside the repo
        # so concurrent branches on the same repo don't share Python state.
        self._git("update-ref", f"refs/timetravel/{tag}", anchor)

        # Capture any uncommitted delta as a stash. ``stash create`` returns
        # empty for a clean tree — in that case there's nothing else to
        # snapshot; ``on_timetravel`` will detect "no stash tag" and skip the
        # pop step.
        delta_commit = self._git("stash", "create")
        if delta_commit:
            self._git(
                "stash", "store", "-m", f"on_branch({tag})", delta_commit
            )

    def on_timetravel(self, branch_id: UUID) -> None:
        """Restore HEAD and working-tree state to the snapshot taken at branch.

        Order of operations matters for safety:

        1. Look up the recorded anchor via ``refs/timetravel/<tag>``.
        2. ``git reset --hard <anchor>`` — undo every commit the agent made.
        3. ``git clean -fd`` — delete untracked files the agent wrote
           (``reset --hard`` leaves untracked alone).
        4. If a stash with the branch tag exists, ``git stash pop`` it to
           restore pre-branch uncommitted changes.
        5. Drop the ``refs/timetravel/<tag>`` ref (cleanup; the branch is gone).

        Idempotent: an unknown ``branch_id`` is a no-op (no recorded
        anchor ref), matching the protocol contract.

        Raises:
            RollbackError: if any git step fails. The tree may be in a
                partial state — the error message names the step that
                failed so the user can recover manually via ``git reflog``.
        """
        tag = _tag_for(branch_id)

        # Step 1: locate the anchor. If no ref exists this is either an
        # unrecognised branch_id (no-op per protocol) OR the snapshot was
        # captured against a repo with no commits. Either way, no restore.
        anchor = self._git_or_none("rev-parse", "--verify", "--quiet", f"refs/timetravel/{tag}")
        if not anchor:
            return

        # Step 2: timetravel commits. ``--hard`` also resets the index, so any
        # staged changes are discarded. This is intentional — the agent's
        # in-flight index state is not something we restore.
        try:
            self._git("reset", "--hard", anchor)
        except RollbackError as exc:
            raise RollbackError(
                f"on_timetravel({tag}): git reset --hard {anchor[:8]} failed — "
                f"reflog is intact: {exc}"
            ) from exc

        # Step 3: drop untracked files the agent wrote. ``-f`` is needed
        # because some agents write read-only files (e.g. compiled output);
        # ``-d`` recurses into directories. We do NOT use ``-x`` — ignored
        # files (e.g. build outputs in .gitignore) are agent's own concern.
        try:
            self._git("clean", "-fd")
        except RollbackError as exc:
            raise RollbackError(
                f"on_timetravel({tag}): git clean -fd failed after reset — "
                f"working tree may have leftover untracked files: {exc}"
            ) from exc

        # Step 4: restore pre-branch uncommitted delta if we captured one.
        stash_ref = self._find_stash(tag)
        if stash_ref is not None:
            try:
                self._git("stash", "pop", stash_ref)
            except RollbackError as exc:
                raise RollbackError(
                    f"on_timetravel({tag}): stash pop failed — the reset+clean "
                    f"already succeeded; manual recovery via "
                    f"'git stash list'/'git stash pop' may be required: {exc}"
                ) from exc

        # Step 5: drop the anchor ref. Optional; we always run it last so
        # even an interrupted timetravel leaves a recoverable reflog trail.
        try:
            self._git("update-ref", "-d", f"refs/timetravel/{tag}")
        except RollbackError:
            # Cleanup failure is non-fatal: the snapshot has been applied.
            return

    # ------------------------------------------------------------------
    # Internals — kept private (no underscore-prefix-collision with Protocol)
    # ------------------------------------------------------------------
    def _drop_existing(self, tag: str) -> None:
        """Remove any stash entries previously tagged with ``tag``."""
        stash_ref = self._find_stash(tag)
        if stash_ref is not None:
            self._git("stash", "drop", stash_ref)

    def _find_stash(self, tag: str) -> str | None:
        """Return the ``stash@{n}`` ref for ``tag`` or ``None`` if absent.

        We match on the ``on_branch(...)`` message we wrote — git stash
        refs aren't named by tag, so message-prefix match is the supported
        way. Returns the raw ref string (e.g. ``"stash@{0}"``) so callers
        can pass it directly to ``git stash pop`` / ``drop``.
        """
        listing = self._git("stash", "list")
        if not listing:
            return None
        needle = f"on_branch({tag})"
        for line in listing.splitlines():
            if needle in line:
                # Lines look like ``stash@{0}: On main: on_branch(timetravel-...)``
                colon = line.find(":")
                if colon > 0:
                    return line[:colon]
        return None

    # ------------------------------------------------------------------
    # Internal: HEAD-anchor plumbing. No stash legacy code remains.
    # ------------------------------------------------------------------


__all__ = ["GitRollbackHandler"]
