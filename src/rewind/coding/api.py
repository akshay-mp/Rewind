"""FastAPI routes for the coding control plane."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from rewind.coding.controller import CodingController, ControlConflict, LeaseRequired
from rewind.coding.domain import CheckSpec, CodingRun, GoalProfile
from rewind.coding.workspace import GitWorktreeWorkspaceProvider, WorkspaceSafetyError


def _public(value: object) -> object:
    if hasattr(value, "__dataclass_fields__"):
        return {key: _public(getattr(value, key)) for key in value.__dataclass_fields__}
    if isinstance(value, dict):
        return {str(key): _public(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_public(item) for item in value]
    if hasattr(value, "value"):
        return value.value
    return value


def mount_coding(app: FastAPI) -> None:
    """Mount coding routes using ``app.state.store``."""
    if not hasattr(app.state, "coding_controller"):
        app.state.coding_controller = CodingController(app.state.store)

    def controller() -> CodingController:
        return app.state.coding_controller

    @app.post("/api/v1/coding/runs", status_code=status.HTTP_201_CREATED)
    async def create_coding_run(body: dict[str, Any]) -> dict[str, Any]:
        run = CodingRun(
            run_id=str(body.get("run_id") or CodingRun().run_id),
            workspace_path=str(body.get("workspace_path", "")),
            goal_profile_id=str(body.get("goal_profile_id", "")),
            attempt_limit=int(body.get("attempt_limit", 3)),
            time_budget_seconds=int(body.get("time_budget_seconds", 1800)),
            token_budget=int(body.get("token_budget", 200_000)),
            metadata=dict(body.get("metadata", {})),
        )
        created = controller().create_run(run)
        if body.get("start"):
            created = controller().start_run(created.run_id)
        return _public(created)

    @app.get("/api/v1/coding/runs")
    async def list_coding_runs() -> dict[str, Any]:
        runs = controller().store.list_coding_runs()
        return {"runs": [_public(item) for item in runs]}

    @app.get("/api/v1/coding/runs/{run_id}")
    async def get_coding_run(run_id: str) -> dict[str, Any]:
        run = controller().store.get_coding_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="coding run not found")
        return _public(run)

    @app.get("/api/v1/coding/runs/{run_id}/events")
    async def coding_events(run_id: str, request: Request) -> StreamingResponse:
        if controller().store.get_coding_run(run_id) is None:
            raise HTTPException(status_code=404, detail="coding run not found")
        try:
            after = int(request.headers.get("last-event-id", "0"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Last-Event-ID must be an integer") from exc
        events = controller().store.list_coding_events(run_id, after)

        def stream() -> Iterator[str]:
            for event in events:
                data = json.dumps(_public(event))
                yield f"id: {event.sequence}\nevent: {event.kind}\ndata: {data}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.post("/api/v1/coding/runs/{run_id}/leases/acquire")
    @app.post("/api/v1/coding/runs/{run_id}/lease/acquire")
    async def acquire_lease(run_id: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            lease = controller().acquire_lease(
                run_id,
                str(body.get("owner", "anonymous")),
                mode=str(body.get("mode", "automation")),
                ttl_seconds=int(body.get("ttl_seconds", 60)),
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _public(lease)

    @app.post("/api/v1/coding/runs/{run_id}/leases/heartbeat")
    @app.post("/api/v1/coding/runs/{run_id}/lease/heartbeat")
    async def heartbeat_lease(run_id: str, body: dict[str, Any]) -> dict[str, Any]:
        del run_id
        try:
            return _public(
                controller().heartbeat_lease(
                    str(body["lease_id"]), ttl_seconds=int(body.get("ttl_seconds", 60))
                )
            )
        except LeaseRequired as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/v1/coding/runs/{run_id}/leases/release")
    @app.post("/api/v1/coding/runs/{run_id}/lease/release")
    async def release_lease(run_id: str, body: dict[str, Any]) -> dict[str, bool]:
        del run_id
        controller().release_lease(str(body["lease_id"]))
        return {"released": True}

    @app.post("/api/v1/coding/runs/{run_id}/control")
    async def control(run_id: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            run = controller().control(
                run_id,
                str(body["action"]),
                lease_id=str(body["lease_id"]),
                expected_revision=int(body["expected_revision"]),
                idempotency_key=str(body["idempotency_key"]),
            )
        except LeaseRequired as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ControlConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _public(run)

    @app.get("/api/v1/coding/runs/{run_id}/attempts")
    async def attempts(run_id: str) -> dict[str, Any]:
        return {
            "attempts": [_public(item) for item in controller().store.list_coding_attempts(run_id)]
        }

    @app.get("/api/v1/coding/runs/{run_id}/attempts/{attempt_id}/evaluation")
    async def attempt_evaluation(run_id: str, attempt_id: str) -> dict[str, Any]:
        values = controller().store.list_coding_evaluations(run_id, attempt_id)
        if not values:
            raise HTTPException(status_code=404, detail="evaluation not found")
        return _public(values[-1])

    @app.get("/api/v1/coding/runs/{run_id}/attempts/{attempt_id}/diff")
    async def attempt_diff(run_id: str, attempt_id: str) -> dict[str, Any]:
        changes = controller().store.list_coding_events(run_id)
        return {
            "run_id": run_id,
            "attempt_id": attempt_id,
            "changes": [
                event.payload
                for event in changes
                if event.attempt_id == attempt_id and event.kind == "workspace_change"
            ],
        }

    @app.post("/api/v1/coding/goal-profiles", status_code=status.HTTP_201_CREATED)
    async def create_goal_profile(body: dict[str, Any]) -> dict[str, Any]:
        profile = GoalProfile(
            profile_id=str(body.get("profile_id") or GoalProfile().profile_id),
            name=str(body.get("name", "Coding goal")),
            version=int(body.get("version", 1)),
            checks=tuple(CheckSpec(**item) for item in body.get("checks", [])),
        )
        controller().store.upsert_goal_profile(profile)
        return _public(profile)

    @app.post("/api/v1/coding/runs/{run_id}/promote")
    async def promote(run_id: str) -> dict[str, Any]:
        run = controller().store.get_coding_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="coding run not found")
        try:
            artifact = GitWorktreeWorkspaceProvider([run.workspace_path]).promote_artifact(
                run.workspace_path
            )
        except (ValueError, WorkspaceSafetyError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"run_id": run_id, "patch_artifact": artifact, "applied": False}


__all__ = ["mount_coding"]
