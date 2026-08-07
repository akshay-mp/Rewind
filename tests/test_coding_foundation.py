"""Focused checks for the coding control-plane backend foundation."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rewind.coding.adapters import AdapterResult, DemoCodingAdapter
from rewind.coding.controller import CodingController, ControlConflict
from rewind.coding.domain import CheckSpec, CodingEvent, CodingRun, GoalProfile, RunStatus, Verdict
from rewind.coding.evaluator import evaluate_goal
from rewind.coding.runtime import DockerContainerRuntime, RuntimeConfig, RuntimeUnavailableError
from rewind.coding.workspace import GitWorktreeWorkspaceProvider, WorkspaceSafetyError
from rewind.receiver import create_app
from rewind.storage import SCHEMA_VERSION, TraceStore


def test_schema_domain_crud_and_event_sequence(tmp_path: Path) -> None:
    store = TraceStore(tmp_path / "coding.db")
    assert SCHEMA_VERSION == 11
    profile = GoalProfile(name="goal", checks=(CheckSpec("required_text", "ok"),))
    run = CodingRun(workspace_path=str(tmp_path), goal_profile_id=profile.profile_id)
    store.upsert_goal_profile(profile)
    store.upsert_coding_run(run)
    first = store.append_coding_event(CodingEvent(run.run_id, "one", {"nested": [1, 2]}))
    second = store.append_coding_event(CodingEvent(run.run_id, "two"))
    assert [event.sequence for event in store.list_coding_events(run.run_id)] == [1, 2]
    assert store.get_coding_event(first.event_id).payload == {"nested": [1, 2]}
    assert store.get_goal_profile(profile.profile_id).checks[0].value == "ok"
    assert store.get_coding_run(run.run_id).run_id == run.run_id
    assert second.sequence == 2


def test_verdict_precedence_and_control_idempotency(tmp_path: Path) -> None:
    profile = GoalProfile(checks=(CheckSpec("required_text", "ok"), CheckSpec("json_valid")))
    result = evaluate_goal(profile, "nope")
    assert result.verdict is Verdict.FAIL
    store = TraceStore(tmp_path / "control.db")
    store.upsert_goal_profile(profile)
    run = CodingRun(workspace_path=str(tmp_path), goal_profile_id=profile.profile_id)
    controller = CodingController(store, DemoCodingAdapter("ok"))
    controller.create_run(run)
    lease = controller.acquire_lease(run.run_id, "human", mode="human")
    current = store.get_coding_run(run.run_id)
    controller.control(run.run_id, "pause", lease_id=lease.lease_id,
                       expected_revision=current.revision, idempotency_key="same")
    paused_revision = store.get_coding_run(run.run_id).revision
    controller.control(run.run_id, "pause", lease_id=lease.lease_id,
                       expected_revision=0, idempotency_key="same")
    assert store.get_coding_run(run.run_id).revision == paused_revision
    with pytest.raises(ControlConflict):
        controller.control(run.run_id, "pause", lease_id=lease.lease_id,
                           expected_revision=0, idempotency_key="new")


def test_view_navigation_never_calls_adapter(tmp_path: Path) -> None:
    store = TraceStore(tmp_path / "view.db")
    adapter = DemoCodingAdapter()
    controller = CodingController(store, adapter)
    run = CodingRun(workspace_path=str(tmp_path))
    controller.create_run(run)
    store.append_coding_event(CodingEvent(run.run_id, "observable"))
    before = adapter.calls
    assert controller.rewind(run.run_id, 1)
    assert controller.forward(run.run_id, 1)
    assert adapter.calls == before


def test_fail_repair_pass_records_reference_and_feedback(tmp_path: Path) -> None:
    class RepairAdapter:
        def __init__(self) -> None:
            self.calls = 0

        def execute(self, run: CodingRun, attempt: object, emit: object) -> AdapterResult:
            self.calls += 1
            return AdapterResult(output="pass" if self.calls == 2 else "fail")

    store = TraceStore(tmp_path / "repair.db")
    profile = GoalProfile(checks=(CheckSpec("required_text", "pass"),))
    store.upsert_goal_profile(profile)
    run = CodingRun(workspace_path=str(tmp_path), goal_profile_id=profile.profile_id)
    adapter = RepairAdapter()
    controller = CodingController(store, adapter)  # type: ignore[arg-type]
    controller.start(run)
    lease = controller.acquire_lease(run.run_id, "human", mode="human")
    current = store.get_coding_run(run.run_id)
    controller.control(run.run_id, "repair", lease_id=lease.lease_id,
                       expected_revision=current.revision, idempotency_key="repair-1")
    attempts = store.list_coding_attempts(run.run_id)
    assert attempts[1].repair_of_attempt_id == attempts[0].attempt_id
    assert attempts[1].metadata["repair_feedback"]["failed_attempt_id"] == attempts[0].attempt_id
    assert store.get_coding_run(run.run_id).status is RunStatus.COMPLETED


def test_runtime_fail_closed_and_worktree_safety(tmp_path: Path) -> None:
    runtime = DockerContainerRuntime(RuntimeConfig(), executor=None)
    runtime._executor = None
    if runtime.available():
        pytest.skip("Docker is installed in this environment")
    with pytest.raises(RuntimeUnavailableError):
        runtime.run(["python", "-c", "print(1)"], workspace=tmp_path)
    provider = GitWorktreeWorkspaceProvider([tmp_path])
    assert provider.validate(tmp_path) == tmp_path.resolve()
    with pytest.raises(WorkspaceSafetyError):
        provider.validate(tmp_path.parent)


def test_api_shapes_and_sse_replay(tmp_path: Path) -> None:
    store = TraceStore(tmp_path / "api.db")
    client = TestClient(create_app(store))
    profile = client.post("/api/v1/coding/goal-profiles", json={"checks": []}).json()
    response = client.post(
        "/api/v1/coding/runs",
        json={"workspace_path": str(tmp_path), "goal_profile_id": profile["profile_id"]},
    )
    assert response.status_code == 201
    run_id = response.json()["run_id"]
    events = client.get(f"/api/v1/coding/runs/{run_id}/events")
    assert events.status_code == 200
    assert "run_created" in events.text
    last = events.text.split("id: ")[1].splitlines()[0]
    replay = client.get(f"/api/v1/coding/runs/{run_id}/events", headers={"Last-Event-ID": last})
    assert "run_created" not in replay.text
