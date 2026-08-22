"""Durable coding-run state machine and lease-protected controls."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from agent_timetravel.coding.adapters import CodingAdapter, DemoCodingAdapter
from agent_timetravel.coding.domain import (
    CodingAttempt,
    CodingEvent,
    CodingRun,
    ControlLease,
    EvaluationResult,
    RunStatus,
    Verdict,
    now_iso,
)
from agent_timetravel.coding.evaluator import evaluate_goal


class ControlConflict(RuntimeError):
    """Raised for stale revisions or an invalid control transition."""


class LeaseRequired(ControlConflict):
    """Raised when a mutating control lacks a valid lease."""


class CodingController:
    """Coordinate durable state, adapter calls, leases, and trusted evaluation."""

    def __init__(self, store: object, adapter: CodingAdapter | None = None) -> None:
        self.store = store
        self.adapter = adapter or DemoCodingAdapter()
        self._recover_interrupted()

    def _recover_interrupted(self) -> None:
        for run in self.store.list_coding_runs():
            if run.status in {RunStatus.RUNNING}:
                self.store.upsert_coding_run(
                    replace(run, status=RunStatus.INTERRUPTED_RECOVERABLE, updated_at=now_iso())
                )

    def create_run(self, run: CodingRun) -> CodingRun:
        self.store.upsert_coding_run(run)
        self._event(run.run_id, "run_created", {"revision": run.revision})
        return run

    def start(self, run: CodingRun | str) -> CodingRun:
        """Persist and execute the initial attempt."""
        if isinstance(run, str):
            return self.start_run(run)
        if self.store.get_coding_run(run.run_id) is None:
            self.create_run(run)
        return self.start_run(run.run_id)

    def start_run(self, run_id: str) -> CodingRun:
        run = self._require_run(run_id)
        if run.status in {RunStatus.COMPLETED, RunStatus.STOPPED}:
            raise ControlConflict(f"run is terminal: {run.status.value}")
        run = self._transition(run, RunStatus.RUNNING)
        attempt = self._new_attempt(run)
        self._execute(run, attempt)
        return self._require_run(run_id)

    def acquire_lease(
        self, run_id: str, owner: str, *, mode: str = "automation", ttl_seconds: int = 60
    ) -> ControlLease:
        now = datetime.now(UTC)
        lease = ControlLease(
            run_id=run_id,
            owner=owner,
            mode=mode,
            heartbeat_at=now.isoformat(),
            expires_at=(now + timedelta(seconds=ttl_seconds)).isoformat(),
        )
        result = self.store.acquire_control_lease(lease, preempt=mode == "human")
        self._event(run_id, "lease_acquired", {"lease_id": result.lease_id, "mode": mode})
        return result

    def heartbeat_lease(self, lease_id: str, *, ttl_seconds: int = 60) -> ControlLease:
        lease = self.store.get_control_lease(lease_id)
        if lease is None or not lease.active:
            raise LeaseRequired("lease is not active")
        now = datetime.now(UTC)
        updated = replace(
            lease,
            heartbeat_at=now.isoformat(),
            expires_at=(now + timedelta(seconds=ttl_seconds)).isoformat(),
        )
        self.store.update_control_lease(updated)
        self._event(lease.run_id, "lease_heartbeat", {"lease_id": lease_id})
        return updated

    def release_lease(self, lease_id: str) -> None:
        lease = self.store.get_control_lease(lease_id)
        if lease is None:
            return
        self.store.update_control_lease(replace(lease, active=False))
        self._event(lease.run_id, "lease_released", {"lease_id": lease_id})

    def control(
        self,
        run_id: str,
        action: str,
        *,
        lease_id: str,
        expected_revision: int,
        idempotency_key: str,
    ) -> CodingRun:
        """Apply one idempotent control exactly once."""
        run = self._require_run(run_id)
        prior = next(
            (
                event
                for event in self.store.list_coding_events(run_id)
                if event.kind == "control"
                and event.payload.get("idempotency_key") == idempotency_key
            ),
            None,
        )
        if prior is not None:
            return self._require_run(run_id)
        lease = self.store.get_control_lease(lease_id)
        if (
            lease is None
            or not lease.active
            or lease.run_id != run_id
            or self._expired(lease.expires_at)
        ):
            raise LeaseRequired("an active lease is required")
        if run.revision != expected_revision:
            raise ControlConflict(
                f"expected revision {expected_revision}, current revision {run.revision}"
            )
        if action in {"timetravel", "forward"}:
            raise ControlConflict("view navigation is not a mutating control")
        if action == "repair":
            evaluations = self.store.list_coding_evaluations(run_id)
            if not evaluations or evaluations[-1].verdict is not Verdict.FAIL:
                raise ControlConflict("repair is permitted only after a FAIL evaluation")
        next_status = {
            "start": RunStatus.RUNNING,
            "continue": RunStatus.RUNNING,
            "step": RunStatus.RUNNING,
            "pause": RunStatus.PAUSED,
            "stop": RunStatus.STOPPED,
            "evaluate": run.status,
            "repair": RunStatus.RUNNING,
            "restore-and-branch": RunStatus.RUNNING,
            "restore_and_branch": RunStatus.RUNNING,
        }.get(action)
        if next_status is None:
            raise ControlConflict(f"unsupported control: {action}")
        updated = self._transition(run, next_status)
        self._event(
            run_id,
            "control",
            {"action": action, "idempotency_key": idempotency_key},
            idempotency_key=idempotency_key,
        )
        if action in {"start", "continue", "step", "repair"}:
            failed = self._failed_attempt(updated) if action == "repair" else None
            if action == "start" and self.store.list_coding_attempts(run_id):
                return self._require_run(run_id)
            attempt = self._new_attempt(updated, repair_of=failed)
            self._execute(updated, attempt)
        elif action == "evaluate":
            self.evaluate(run_id)
        return self._require_run(run_id)

    def navigate(self, run_id: str, direction: str, sequence: int) -> list[CodingEvent]:
        """View-only event navigation; it performs zero adapter calls."""
        events = self.store.list_coding_events(run_id)
        if direction == "timetravel":
            return [event for event in events if event.sequence <= sequence]
        return [event for event in events if event.sequence >= sequence]

    def timetravel(self, run_id: str, sequence: int) -> list[CodingEvent]:
        """View-only timetravel convenience method."""
        return self.navigate(run_id, "timetravel", sequence)

    def forward(self, run_id: str, sequence: int) -> list[CodingEvent]:
        """View-only forward convenience method."""
        return self.navigate(run_id, "forward", sequence)

    def evaluate(self, run_id: str) -> EvaluationResult:
        run = self._require_run(run_id)
        profile = self.store.get_goal_profile(run.goal_profile_id)
        if profile is None:
            result = EvaluationResult(
                run_id=run_id, verdict=Verdict.UNKNOWN, error="goal profile not found"
            )
        else:
            attempts = self.store.list_coding_attempts(run_id)
            attempt = attempts[-1] if attempts else None
            context = dict(attempt.metadata.get("context", {})) if attempt else {}
            context.update(
                {
                    "tokens": attempt.tokens if attempt else None,
                    "cost_usd": attempt.cost_usd if attempt else None,
                    "duration_seconds": attempt.duration_seconds if attempt else None,
                }
            )
            result = evaluate_goal(
                profile,
                attempt.output if attempt else "",
                context=context,
                run_id=run_id,
                attempt_id=attempt.attempt_id if attempt else None,
            )
        self.store.insert_coding_evaluation(result)
        self._event(
            run_id,
            "evaluation",
            {"evaluation_id": result.evaluation_id, "verdict": result.verdict.value},
        )
        return result

    def _execute(self, run: CodingRun, attempt: CodingAttempt) -> None:
        try:
            result = self.adapter.execute(
                run, attempt, lambda event: self.store.append_coding_event(event)
            )
            attempt = replace(
                attempt,
                status="completed",
                finished_at=now_iso(),
                output=result.output,
                tokens=result.tokens,
                cost_usd=result.cost_usd,
                duration_seconds=result.duration_seconds,
                metadata={**attempt.metadata, "context": result.context},
            )
            self.store.upsert_coding_attempt(attempt)
            evaluation = self.evaluate(run.run_id)
            if evaluation.verdict == Verdict.PASS:
                self._transition(self._require_run(run.run_id), RunStatus.COMPLETED)
            elif evaluation.verdict == Verdict.FAIL:
                current = self._require_run(run.run_id)
                self._transition(current, RunStatus.PAUSED)
            else:
                self._transition(self._require_run(run.run_id), RunStatus.PAUSED)
        except Exception as exc:
            self.store.upsert_coding_attempt(
                replace(attempt, status="error", finished_at=now_iso(), error=str(exc))
            )
            self._transition(self._require_run(run.run_id), RunStatus.PAUSED)
            self._event(run.run_id, "execution_error", {"error": str(exc)})

    def _new_attempt(self, run: CodingRun, repair_of: CodingAttempt | None = None) -> CodingAttempt:
        attempts = self.store.list_coding_attempts(run.run_id)
        attempt = CodingAttempt(
            run_id=run.run_id,
            number=len(attempts) + 1,
            repair_of_attempt_id=repair_of.attempt_id if repair_of else None,
            before_snapshot_id=repair_of.after_snapshot_id if repair_of else None,
            metadata={"repair_feedback": self._repair_feedback(repair_of) if repair_of else {}},
        )
        self.store.upsert_coding_attempt(attempt)
        updated = replace(
            run,
            active_attempt_id=attempt.attempt_id,
            revision=run.revision + 1,
            updated_at=now_iso(),
        )
        self.store.upsert_coding_run(updated)
        return attempt

    def _failed_attempt(self, run: CodingRun) -> CodingAttempt | None:
        attempts = self.store.list_coding_attempts(run.run_id)
        return attempts[-1] if attempts and attempts[-1].status in {"completed", "error"} else None

    @staticmethod
    def _repair_feedback(attempt: CodingAttempt | None) -> dict[str, Any]:
        if attempt is None:
            return {}
        return {
            "failed_attempt_id": attempt.attempt_id,
            "after_snapshot_id": attempt.after_snapshot_id,
            "feedback": {"error": attempt.error, "output_length": len(attempt.output)},
        }

    def _transition(self, run: CodingRun, status: RunStatus) -> CodingRun:
        updated = replace(run, status=status, revision=run.revision + 1, updated_at=now_iso())
        self.store.upsert_coding_run(updated)
        self._event(run.run_id, "status", {"status": status.value, "revision": updated.revision})
        return updated

    def _event(
        self,
        run_id: str,
        kind: str,
        payload: dict[str, Any],
        *,
        attempt_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> CodingEvent:
        return self.store.append_coding_event(
            CodingEvent(run_id, kind, payload, attempt_id=attempt_id), idempotency_key
        )

    def _require_run(self, run_id: str) -> CodingRun:
        run = self.store.get_coding_run(run_id)
        if run is None:
            raise KeyError(f"coding run not found: {run_id}")
        return run

    @staticmethod
    def _expired(value: str) -> bool:
        try:
            return datetime.fromisoformat(value) <= datetime.now(UTC)
        except ValueError:
            return True


__all__ = ["CodingController", "ControlConflict", "LeaseRequired"]
