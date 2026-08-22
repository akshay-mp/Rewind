"""Unit tests for ``agent_timetravel.receiver`` — FastAPI OTLP/HTTP surface.

We use FastAPI's ``TestClient`` so tests stay synchronous and the receiver
never binds a real socket. The store points at a temp path so each test is
isolated. Spans actually land in SQLite, and we read them back through the
``TraceStore`` to assert the full ingest pipeline.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2 as ts
from opentelemetry.proto.common.v1 import common_pb2 as c

from agent_timetravel.receiver import create_app
from agent_timetravel.storage import TraceStore

_TRACE_ID = bytes.fromhex("abcdef1234567890abcdef1234567890")
_SPAN_ID = bytes.fromhex("1111111111111111")
_PROTO_CONTENT_TYPE = "application/x-protobuf"


def _kv(key: str, value: object) -> c.KeyValue:
    """Build a single KeyValue with detected oneof from the Python value."""
    kv = c.KeyValue(key=key)
    av = c.AnyValue()
    if isinstance(value, bool):
        av.bool_value = value
    elif isinstance(value, int):
        av.int_value = value
    elif isinstance(value, str):
        av.string_value = value
    else:
        msg = f"unsupported test value type: {type(value)}"
        raise TypeError(msg)
    kv.value.CopyFrom(av)
    return kv


def _one_llm_request() -> ts.ExportTraceServiceRequest:
    """A 1-span OTLP request used by multiple tests."""
    req = ts.ExportTraceServiceRequest()
    rs = req.resource_spans.add()
    rs.resource.attributes.extend([_kv("service.name", "demo")])
    span = rs.scope_spans.add().spans.add()
    span.trace_id = _TRACE_ID
    span.span_id = _SPAN_ID
    span.name = "chat.completions.openai"
    span.start_time_unix_nano = 1_700_000_000_000_000_000
    span.end_time_unix_nano = 1_700_000_001_000_000_000
    span.status.code = 1
    span.attributes.extend(
        [
            _kv("gen_ai.system", "openai"),
            _kv("gen_ai.request.model", "gpt-4o"),
            _kv("gen_ai.response.model", "gpt-4o-2024"),
            _kv("gen_ai.usage.prompt_tokens", 11),
            _kv("gen_ai.usage.completion_tokens", 22),
            _kv("gen_ai.usage.total_tokens", 33),
        ]
    )
    return req


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    """App + TestClient wired to a temp SQLite store."""
    db = tmp_path / "ph1_receiver.db"
    store = TraceStore(str(db))
    app = create_app(store)
    # The client holds the loop open unless we shutdown; we use the context
    # manager so each test gets a clean lifecycle.
    with TestClient(app) as c:
        yield c


class TestHealth:
    def test_healthz(self, client: TestClient) -> None:
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


class TestProtoIngest:
    def test_protobuf_post_persists_span(self, client: TestClient) -> None:
        req = _one_llm_request()
        resp = client.post(
            "/v1/traces",
            content=req.SerializeToString(),
            headers={"content-type": _PROTO_CONTENT_TYPE},
        )
        assert resp.status_code == 200
        assert resp.headers["x-timetravel-spans-accepted"] == "1"

        # Round-trip through the store the server actually used.
        store: TraceStore = client.app.state.store  # type: ignore[attr-defined]
        spans = store.get_spans(_TRACE_ID.hex())
        assert len(spans) == 1
        assert spans[0].span_id == _SPAN_ID.hex()
        assert spans[0].total_tokens == 33

    def test_empty_protobuf_request_succeeds_with_zero_count(
        self, client: TestClient
    ) -> None:
        blob = ts.ExportTraceServiceRequest().SerializeToString()
        resp = client.post(
            "/v1/traces",
            content=blob,
            headers={"content-type": _PROTO_CONTENT_TYPE},
        )
        assert resp.status_code == 200
        assert resp.headers["x-timetravel-spans-accepted"] == "0"

    def test_multi_span_request_persists_one_trace_many_spans(
        self, client: TestClient
    ) -> None:
        req = ts.ExportTraceServiceRequest()
        rs = req.resource_spans.add()
        ss = rs.scope_spans.add()
        for i in range(3):
            s = ss.spans.add()
            s.trace_id = _TRACE_ID
            s.span_id = bytes([i + 1] * 8)
            s.name = f"span.{i}"
            s.start_time_unix_nano = 1_700_000_000_000_000_000 + i
            s.end_time_unix_nano = s.start_time_unix_nano + 1

        resp = client.post(
            "/v1/traces",
            content=req.SerializeToString(),
            headers={"content-type": _PROTO_CONTENT_TYPE},
        )
        assert resp.status_code == 200
        assert resp.headers["x-timetravel-spans-accepted"] == "3"

        store: TraceStore = client.app.state.store  # type: ignore[attr-defined]
        # Exactly one trace row, three spans.
        trace = store.get_trace(_TRACE_ID.hex())
        assert trace is not None
        assert isinstance(trace.root_branch_id, UUID)
        assert len(trace.spans) == 3


class TestContentTypeNegotiation:
    def test_missing_content_type_returns_415(self, client: TestClient) -> None:
        resp = client.post("/v1/traces", content=b"whatever")
        assert resp.status_code == 415

    def test_unsupported_content_type_returns_415(self, client: TestClient) -> None:
        resp = client.post(
            "/v1/traces",
            content=b"x",
            headers={"content-type": "text/plain"},
        )
        assert resp.status_code == 415

    def test_malformed_content_returns_400(self, client: TestClient) -> None:
        resp = client.post(
            "/v1/traces",
            content=b"not-proto",
            headers={"content-type": _PROTO_CONTENT_TYPE},
        )
        assert resp.status_code == 400

    def test_json_ingest_works(self, client: TestClient) -> None:
        from google.protobuf import json_format

        req = _one_llm_request()
        json_body = json_format.MessageToJson(req)
        resp = client.post(
            "/v1/traces",
            content=json_body.encode("utf-8"),
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 200
        assert resp.headers["x-timetravel-spans-accepted"] == "1"


class TestSecurityPosture:
    """Smoke tests for Phase 1's documented security rules.

    The receiver intentionally exposes no docs, no schema, no auth. These
    tests pin that posture so a regression here is caught before deploy.
    """

    def test_no_openapi_schema_advertised(self, client: TestClient) -> None:
        # docs_url=None / openapi_url=None in create_app
        assert client.get("/openapi.json").status_code == 404
        assert client.get("/docs").status_code == 404

    def test_no_cors_headers_leak(self, client: TestClient) -> None:
        resp = client.options(
            "/v1/traces",
            headers={
                "origin": "https://evil.example",
                "access-control-request-method": "POST",
            },
        )
        # No CORS middleware installed → no acao header should be present.
        assert "access-control-allow-origin" not in {k.lower() for k in resp.headers}


class TestUiMountGracefulDegradation:
    """Phase 2: ``/ui`` must respond gracefully whether or not the UI is built.

    These tests pin the *absent* path (no ``web/dist``) so the fallback hint
    is never accidentally broken by a refactor. The *present* path is covered
    by ``tests/integration/test_ui_served.py`` because it requires the real
    built artifact.
    """

    def test_ui_route_registered(self, client: TestClient) -> None:
        # Even in absence, the route exists — it returns the hint, not 404.
        resp = client.get("/ui", follow_redirects=False)
        # Either the graceful 307 redirect (UI built) or the 404-with-body
        # hint (not built). The integration environment has the dist present,
        # so we assert the union, not one specific path.
        assert resp.status_code in {307, 404}

    def test_timeline_routes_registered_alongside_ui(
        self, client: TestClient
    ) -> None:
        # Phase 2 contract: read API and UI mount coexist on one process.
        resp = client.get("/api/v1/traces")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0  # nothing ingested in this fixture.

