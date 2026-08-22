"""FastAPI OTLP/HTTP receiver — the ingest surface.

Phase 1 deliverable: a working local OTLP receiver that any instrumented
agent can ship to. Phase 2 mounts the read-only timeline API and the static
UI artifact onto the same app. Exposes:

- ``POST /v1/traces`` — accepts OTLP/HTTP in either protobuf
  (``Content-Type: application/x-protobuf``) or JSON
  (``Content-Type: application/json``) form. The body is decoded, mapped to
  TimeTravel ``Span``s, and persisted via :class:`timetravel.storage.TraceStore`.
- ``GET /healthz`` — liveness probe used by the wiring docs' smoke test.
- ``GET /api/v1/...`` — read-only timeline API (mounted in Phase 2).
- ``GET /ui/...`` — static timeline UI artifact (Phase 2; graceful 404).

This is a *local, debug-only* surface: it binds to 127.0.0.1 by default and
performs no authentication. The threat model is documented in
``docs/phases/phase-1.md`` and ``docs/phases/phase-2.md``. Network exposure
is opt-in via the ``--host`` CLI flag; production deployment is explicitly
out of scope per plan §1.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from agent_timetravel import __version__
from agent_timetravel.agents import TimeTravel
from agent_timetravel.coding.api import mount_coding
from agent_timetravel.eval_api import mount_eval
from agent_timetravel.ingest import (
    IngestError,
    decode_export_request,
    decode_export_request_json,
    spans_from_request,
)
from agent_timetravel.models import Span, Trace
from agent_timetravel.stepping_api import mount_stepping
from agent_timetravel.storage import TraceStore
from agent_timetravel.timeline import mount_timeline
from agent_timetravel.ui_assets import ui_dist_path

_PROTO_CONTENT_TYPE = "application/x-protobuf"
_JSON_CONTENT_TYPES = ("application/json", "application/json; charset=utf-8")
_LIVENESS_OK = "ok"
_UI_MOUNT_PATH = "/ui"

#: Page returned when the UI build artifact is absent. Tells the operator
#: how to build it without leaking internal paths or stack traces.
_UI_MISSING_HTML = """<!doctype html>
<html lang="en">
  <head><meta charset="utf-8"><title>TimeTravel UI not built</title></head>
  <body>
    <h1>TimeTravel UI not built</h1>
    <p>The OTLP receiver and read API are running, but the timeline UI has
       not been built yet.</p>
    <p>To build it:</p>
    <pre><code>cd web &amp;&amp; pnpm install &amp;&amp; pnpm build</code></pre>
    <p>Then reload this page.</p>
  </body>
</html>
"""


def create_app(store: TraceStore, registry: TimeTravel | None = None) -> FastAPI:
    """Build a FastAPI app bound to the given store.

    The store is injected (not constructed inside the handler) so tests can
    pass a temp-path store, and so the app can be re-used across replays.
    Mounts the read-only timeline API (:func:`timetravel.timeline.mount_timeline`)
    and serves the static UI artifact if it has been built.
    """
    app = FastAPI(
        title="TimeTravel OTLP/HTTP receiver",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        # Hide the schema endpoint — local debug surface, not advertised.
        openapi_url=None,
    )
    app.state.store = store

    _register_routes(app)

    # Phase 2: read-only timeline API lives at /api/v1/* and shares the store.
    mount_timeline(app)

    # Phase 5.5: eval harness API lives at /api/v1/evals* and shares the store.
    mount_eval(app)

    # Phase 9: interactive stepping server lives at /api/v1/sessions* and
    # shares the store. Mounted after eval so all read APIs are available
    # before the (potentially long-running) stepping surface.
    mount_stepping(app, registry=registry)

    # Coding-agent control plane shares the same durable SQLite store.
    mount_coding(app)

    # Phase 2: static UI artifact at /ui. Mounted last so /api and /v1 win.
    _mount_ui(app)
    return app


def _mount_ui(app: FastAPI) -> None:
    """Serve the built UI artifact at ``/ui``, or a helpful 404 if absent.

    If the UI dist exists we mount :class:`StaticFiles` and rewrite the bare
    ``/ui`` (no trailing slash) to ``/ui/`` so the SPA entrypoint loads. If
    absent we register a single route that returns the "not built" hint.
    """
    dist = ui_dist_path()
    if dist is not None:
        app.mount(
            _UI_MOUNT_PATH,
            StaticFiles(directory=str(dist), html=True),
            name="timetravel-ui",
        )

        @app.get(_UI_MOUNT_PATH, include_in_schema=False, response_class=HTMLResponse)
        def ui_redirect() -> Response:  # pragma: no cover
            # Trivial redirect — covered by integration test rather than unit.
            return Response(
                status_code=status.HTTP_307_TEMPORARY_REDIRECT,
                headers={"Location": f"{_UI_MOUNT_PATH}/"},
            )

        return

    @app.get(
        _UI_MOUNT_PATH,
        include_in_schema=False,
        response_class=HTMLResponse,
    )
    def ui_missing() -> HTMLResponse:
        # ``status=404`` and not 200 so the integration test can distinguish
        # "not built" from "served". The body is operator-facing only.
        return HTMLResponse(
            content=_UI_MISSING_HTML,
            status_code=status.HTTP_404_NOT_FOUND,
        )


def _register_routes(app: FastAPI) -> None:
    """Wire all Phase 1 routes onto ``app``."""

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        """Liveness probe — no store touch, instant 200."""
        return {"status": _LIVENESS_OK}

    @app.post("/v1/traces")
    async def ingest_traces(request: Request) -> Response:
        """Accept an OTLP ``ExportTraceServiceRequest`` and persist its spans.

        Content-type selects the codec:

        - ``application/x-protobuf`` → binary protobuf
        - ``application/json``       → OTLP JSON

        Returns the OTLP/HTTP response shape (`ExportTraceServiceResponse` is
        empty by spec) with status 200 on success, 400 on malformed payloads,
        415 on unsupported content type.
        """
        ctype = (request.headers.get("content-type") or "").lower().split(";")[0].strip()
        body = await request.body()

        try:
            if ctype == _PROTO_CONTENT_TYPE:
                req = decode_export_request(body)
            elif ctype in _JSON_CONTENT_TYPES:
                req = decode_export_request_json(body)
            else:
                raise HTTPException(
                    status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                    detail=(
                        f"unsupported content type '{ctype}'; expected "
                        f"{_PROTO_CONTENT_TYPE} or {_JSON_CONTENT_TYPES[0]}"
                    ),
                )
        except IngestError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc

        spans = spans_from_request(req)
        count = _persist(app.state.store, spans)

        # Return raw empty protobuf — exactly what the OTLP/HTTP spec wants.
        return Response(
            content=b"",
            media_type=_PROTO_CONTENT_TYPE,
            headers={"x-timetravel-spans-accepted": str(count)},
        )


def _persist(store: TraceStore, spans: list[Span]) -> int:
    """Persist a batch of spans, creating trace rows as needed.

    Groups by trace_id, upserts a trace row per group, then inserts all spans
    on that trace's root branch. Sparse multi-span batches from a single
    exporter flush are handled correctly (one trace row, many spans).
    """
    if not spans:
        return 0

    seen: set[str] = set()
    for span in spans:
        if span.trace_id in seen:
            continue
        seen.add(span.trace_id)
        store.upsert_trace(Trace(trace_id=span.trace_id))

    for span in spans:
        store.insert_span(span)
    return len(spans)


__all__ = ["create_app"]
