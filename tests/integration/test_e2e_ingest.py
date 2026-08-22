"""End-to-end integration tests for Phase 1 ingestion.

Unlike the unit tests under ``tests/``, these exercises the **whole stack**:
the CLI ``serve`` command, a real Uvicorn/FastAPI process bound to a random
loopback port, OTLP/HTTP requests over a real socket, and SQLite persistence
on disk via :class:`TraceStore`.

The contract under test is the Phase 1 exit criterion (plan §6):

> A real OpenInference-instrumented agent produces a queryable trace in
> TimeTravel with full prompt/response/tool-call fidelity. Hash of
> ``span.attributes['gen_ai.prompt']`` matches the source byte-for-byte.
> Span linking (parent → child) round-trips correctly for a multi-step agent.

We mark these with ``@pytest.mark.integration`` so they default-skip on
machines that lack network/port access (CI sandboxes, containers). Run with:

    pytest tests/integration -m integration

We do **not** speak to any real LLM here — the integration boundary is
"OTLP bytes in → on-disk SQLite out", which is what Phase 1 ships. The
replay-engine contract (Phase 3 exit criterion) is what requires an LLM.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from uuid import uuid4

import pytest
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2 as ts
from opentelemetry.proto.common.v1 import common_pb2 as c

from agent_timetravel.models import hash_payload
from agent_timetravel.storage import TraceStore

# Module-level marker: skip the whole file unless ``-m integration`` is passed.
pytestmark = pytest.mark.integration

_PROTO_CT = "application/x-protobuf"
_TRACE_HEX = "fedcba0987654321fedcba0987654321"
_AGENT_HEX = "1111222233334444"
_LLM_HEX = "5555666677778888"
_TOOL_HEX = "99990000aaaabbbb"


def _free_port() -> int:
    """Bind to port 0 to let the OS pick a free port, then release it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sk:
        sk.bind(("127.0.0.1", 0))
        return sk.getsockname()[1]


def _wait_for_health(port: int, timeout: float = 10.0) -> None:
    """Poll /healthz until the server responds or the timeout elapses."""
    deadline = time.monotonic() + timeout
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1) as r:
                if r.status == 200:
                    return
        except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
            last_err = exc
            time.sleep(0.2)
    msg = f"server on port {port} did not become healthy in {timeout}s"
    raise RuntimeError(msg) from last_err


def _raw_kv(key: str, value: object) -> c.KeyValue:
    """Build a KeyValue with primitive (str/int/bool) oneof."""
    kv = c.KeyValue(key=key)
    if isinstance(value, bool):
        kv.value.bool_value = value
    elif isinstance(value, int):
        kv.value.int_value = value
    else:
        kv.value.string_value = str(value)
    return kv


def _three_step_agent_request(source_messages_str: str) -> ts.ExportTraceServiceRequest:
    """Build the reference 3-span agent trace (root+llm+tool).

    Mirrors what an instrumented customer-care agent would emit: one AGENT
    root span, one LLM child carrying the prompt, one TOOL child carrying the
    tool output. Returns the request so the integration test can compare the
    ``messages_hash`` of the persisted LLM span against this source payload.
    """
    trace_id = bytes.fromhex(_TRACE_HEX)
    agent_id = bytes.fromhex(_AGENT_HEX)
    llm_id = bytes.fromhex(_LLM_HEX)
    tool_id = bytes.fromhex(_TOOL_HEX)

    req = ts.ExportTraceServiceRequest()
    rs = req.resource_spans.add()
    rs.resource.attributes.extend(
        [_raw_kv("service.name", "customer-care-agent"), _raw_kv("gen_ai.system", "openai")]
    )
    ss = rs.scope_spans.add()

    agent = ss.spans.add()
    agent.trace_id = trace_id
    agent.span_id = agent_id
    agent.name = "CustomerCareAgent.run"
    agent.start_time_unix_nano = 1_700_000_000_000_000_000
    agent.end_time_unix_nano = 1_700_000_005_000_000_000
    agent.status.code = 1
    agent.attributes.extend([_raw_kv("openinference.span.kind", "AGENT")])

    llm = ss.spans.add()
    llm.trace_id = trace_id
    llm.span_id = llm_id
    llm.parent_span_id = agent_id
    llm.name = "chat.completions.openai"
    llm.start_time_unix_nano = 1_700_000_001_000_000_000
    llm.end_time_unix_nano = 1_700_000_002_000_000_000
    llm.status.code = 1
    # The source-of-truth prompt payload — used for byte-for-byte hash compare.
    llm.attributes.extend(
        [
            _raw_kv("gen_ai.request.model", "gpt-4o"),
            _raw_kv("gen_ai.response.model", "gpt-4o"),
            _raw_kv("gen_ai.usage.prompt_tokens", 18),
            _raw_kv("gen_ai.usage.completion_tokens", 9),
            _raw_kv("gen_ai.usage.total_tokens", 27),
            _raw_kv("gen_ai.prompt", source_messages_str),
            _raw_kv("gen_ai.completion", "ok"),
        ]
    )

    tool = ss.spans.add()
    tool.trace_id = trace_id
    tool.span_id = tool_id
    tool.parent_span_id = agent_id
    tool.name = "tool.kb_lookup"
    tool.start_time_unix_nano = 1_700_000_003_000_000_000
    tool.end_time_unix_nano = 1_700_000_004_000_000_000
    tool.status.code = 1
    tool.attributes.extend(
        [_raw_kv("tool.name", "kb_lookup"), _raw_kv("tool.output", "answer=canned")]
    )

    return req


@pytest.fixture
def running_server(tmp_path: Path):
    """Spawn ``agent-timetravel serve`` on a random port, yield (port, db_path), tear down.

    Marks as integration. Skipped automatically if ``-m "not integration"`` is
    passed (CI convention for fast test runs).
    """
    port = _free_port()
    db_path = tmp_path / f"timetravel_it_{uuid4().hex[:8]}.db"
    # S603: we spawn our own ``agent-timetravel serve`` binary, never untrusted input.
    proc = subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            "-m",
            "agent_timetravel",
            "serve",
            "--port",
            str(port),
            "--db",
            str(db_path),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for_health(port)
        yield port, db_path
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


class TestEndToEndIngest:
    def test_three_span_agent_trace_round_trips_with_fidelity(
        self, running_server: tuple[int, Path]
    ) -> None:
        port, db_path = running_server
        source_messages_str = repr(
            [
                {"role": "system", "content": "You help customers."},
                {"role": "user", "content": "Where is my order?"},
            ]
        )
        req = _three_step_agent_request(source_messages_str)
        blob = req.SerializeToString()

        http_req = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/traces",
            data=blob,
            headers={"content-type": _PROTO_CT},
        )
        # S310: URL is a loopback we just spawned; no file:// / custom scheme.
        with urllib.request.urlopen(http_req, timeout=5) as resp:  # noqa: S310
            assert resp.status == 200
            assert resp.headers.get("x-timetravel-spans-accepted") == "3"

        # Read back through a new store handle pointing at the same DB file.
        # WAL mode lets a reader open without locking out the writer.
        store = TraceStore(str(db_path))
        trace = store.get_trace(_TRACE_HEX)
        assert trace is not None
        assert len(trace.spans) == 3

        by_span_id = {s.span_id: s for s in trace.spans}

        # Phase 1 exit criterion: parent → child linking round-trips.
        assert by_span_id[_LLM_HEX].parent_span_id == _AGENT_HEX
        assert by_span_id[_TOOL_HEX].parent_span_id == _AGENT_HEX
        assert by_span_id[_AGENT_HEX].parent_span_id is None

        # Phase 1 exit criterion: prompt hash matches source payload byte-for-byte.
        llm = by_span_id[_LLM_HEX]
        source_prompt = llm.raw_attributes["gen_ai.prompt"]
        assert hash_payload(source_prompt) == hash_payload(source_messages_str)

        # Resource attributes merge into every span's raw_attributes.
        for sp in trace.spans:
            assert sp.raw_attributes["service.name"] == "customer-care-agent"
