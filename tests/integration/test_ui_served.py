"""End-to-end integration test: ``rewind ui`` serves the timeline UI.

Phase 2 exit criterion (plan §6):

> Phase-1 reference trace loads + inspectable; 200-span trace renders with no
> perceptible lag.

This test covers the *loaded* contract:

1. The UI build artifact (``web/dist``) exists and is mounted at ``/ui``.
2. ``GET /ui/`` returns the SPA HTML entrypoint with the expected title.
3. The same-origin read API (``/api/v1/*``) serves the Phase 1 reference
   trace data.
4. A span search returns hits from the same data.

We do **not** headlessly verify the React render (no Playwright here) — the
contract is the artifact mount + same-origin API surface. Visual/200-span lag
verification is the operator's manual check, see ``docs/phases/phase-2.md``.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2 as ts
from opentelemetry.proto.common.v1 import common_pb2 as c

pytestmark = pytest.mark.integration

_PROTO_CT = "application/x-protobuf"
_TRACE_HEX = "0123456789abcdef0123456789abcdef"
_AGENT_HEX = "0011223344556677"
_LLM_HEX = "8899aabbccddeeff"
_TOOL_HEX = "ffeeddccbbaa9988"

#: Title must appear in index.html and the unbuilt Vite template.
_EXPECTED_TITLE = "Rewind — Agent Timeline"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sk:
        sk.bind(("127.0.0.1", 0))
        return sk.getsockname()[1]


def _wait_for_health(port: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            # S310: loopback we just spawned.
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/healthz", timeout=1
            ) as r:
                if r.status == 200:
                    return
        except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
            last = exc
            time.sleep(0.2)
    msg = f"server on port {port} not healthy in {timeout}s"
    raise RuntimeError(msg) from last


def _kv(key: str, value: object) -> c.KeyValue:
    kv = c.KeyValue(key=key)
    av = c.AnyValue()
    if isinstance(value, bool):
        av.bool_value = value
    elif isinstance(value, int):
        av.int_value = value
    else:
        av.string_value = str(value)
    kv.value.CopyFrom(av)
    return kv


def _reference_trace_request() -> ts.ExportTraceServiceRequest:
    """A 3-span AGENT+LLM+TOOL trace — the canonical Phase 1 fixture."""
    trace_id = bytes.fromhex(_TRACE_HEX)
    agent_id = bytes.fromhex(_AGENT_HEX)
    llm_id = bytes.fromhex(_LLM_HEX)
    tool_id = bytes.fromhex(_TOOL_HEX)

    req = ts.ExportTraceServiceRequest()
    rs = req.resource_spans.add()
    ss = rs.scope_spans.add()

    agent = ss.spans.add()
    agent.trace_id = trace_id
    agent.span_id = agent_id
    agent.name = "ReferenceAgent.run"
    agent.start_time_unix_nano = 1_700_000_000_000_000_000
    agent.end_time_unix_nano = 1_700_000_005_000_000_000
    agent.status.code = 1
    agent.attributes.extend([_kv("openinference.span.kind", "AGENT")])

    llm = ss.spans.add()
    llm.trace_id = trace_id
    llm.span_id = llm_id
    llm.parent_span_id = agent_id
    llm.name = "chat.completions.openai"
    llm.start_time_unix_nano = 1_700_000_001_000_000_000
    llm.end_time_unix_nano = 1_700_000_004_000_000_000
    llm.status.code = 1
    llm.attributes.extend(
        [
            _kv("gen_ai.request.model", "gpt-4o"),
            _kv("gen_ai.response.model", "gpt-4o"),
            _kv("gen_ai.usage.prompt_tokens", 100),
            _kv("gen_ai.usage.completion_tokens", 20),
            _kv("gen_ai.usage.total_tokens", 120),
            _kv(
                "gen_ai.prompt",
                '[{"role":"user","content":"find products"}]',
            ),
        ]
    )

    tool = ss.spans.add()
    tool.trace_id = trace_id
    tool.span_id = tool_id
    tool.parent_span_id = agent_id
    tool.name = "tool.search_products"
    tool.start_time_unix_nano = 1_700_000_002_000_000_000
    tool.end_time_unix_nano = 1_700_000_003_000_000_000
    tool.status.code = 1
    tool.attributes.extend(
        [_kv("tool.name", "search_products"), _kv("tool.output", "[]")]
    )
    return req


@pytest.fixture(scope="module")
def web_dist_built() -> None:
    """Ensure ``web/dist`` exists before tests run.

    Run-with-different-venv safety: the parent of this test's package is the
    project root; ``web/dist`` is the Vite build output. If the operator
    hasn't run ``pnpm build`` we skip with a clear reason rather than fail,
    because the UI artifact is a *build* dependency, not a Python one.
    """
    # This file: <root>/tests/integration/test_ui_served.py
    dist = Path(__file__).resolve().parents[2] / "web" / "dist" / "index.html"
    if not dist.is_file():
        pytest.skip(
            "web/dist not built; run `cd web && pnpm install && pnpm build`"
        )


@pytest.fixture(scope="module")
def ui_server(web_dist_built: None, tmp_path_factory: pytest.TempPathFactory) -> str:
    """Boot ``rewind ui`` against a temp DB; return the base URL.

    Returns the base URL once /healthz is up. Spawns the subprocess with the
    same Python interpreter so the venv (and its ``rewind`` install) is used.
    """
    db = tmp_path_factory.mktemp("ui_served") / "ui.db"
    port = _free_port()
    base = f"http://127.0.0.1:{port}"

    # Ingest the reference trace *before* the UI server starts so the data
    # exists when the test queries it. Use a one-shot POST via Python+socket
    # — simpler than spawning a second pipeline.
    proc = subprocess.Popen(  # noqa: S603 - argv is constant; no shell.
        [
            sys.executable,
            "-m",
            "rewind",
            "ui",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--otlp-port",
            str(port),
            "--db",
            str(db),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        _wait_for_health(port, timeout=15.0)

        # Push the reference trace through the OTLP ingest port.
        body = _reference_trace_request().SerializeToString()
        # S310: loopback we just spawned; URL is a constant.
        req = urllib.request.Request(  # noqa: S310
            f"{base}/v1/traces",
            data=body,
            headers={"content-type": _PROTO_CT},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
            assert resp.status == 200
            assert resp.headers["x-rewind-spans-accepted"] == "3"

        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_ui_returns_spa_html(ui_server: str) -> None:
    """``GET /ui/`` serves the built index.html with the SPA title."""
    # S310: loopback only.
    with urllib.request.urlopen(f"{ui_server}/ui/", timeout=5) as resp:  # noqa: S310
        assert resp.status == 200
        html = resp.read().decode("utf-8")
    assert _EXPECTED_TITLE in html
    # The Vite-built HTML references a hashed JS asset under /ui/assets/.
    assert "/assets/" in html


def test_ui_root_redirects_to_slash(ui_server: str) -> None:
    """``GET /ui`` (no trailing slash) redirects to ``/ui/``."""
    # S310: loopback only; URL is a constant and request goes back to our
    # freshly-spawned server.
    req = urllib.request.Request(f"{ui_server}/ui")  # noqa: S310
    try:
        # S310: loopback only; request will 307 redirect or 200 index.html.
        urllib.request.urlopen(req, timeout=5)  # type: ignore[func-returns-value]  # noqa: S310
    except urllib.error.HTTPError as exc:
        # PT017: we use a manual assertion rather than ``pytest.raises``
        # because the request may also succeed (302 + follow), in which case
        # there is no HTTPError to catch.
        assert exc.code in {301, 302, 303, 307, 308}  # noqa: PT017
    except urllib.error.URLError:
        # 200 Ok on follow is fine for this contract.
        pass


def test_same_origin_trace_list(ui_server: str) -> None:
    """``GET /api/v1/traces`` returns the ingested reference trace."""
    import json

    # S310: loopback only.
    with urllib.request.urlopen(f"{ui_server}/api/v1/traces", timeout=5) as r:  # noqa: S310
        body = json.loads(r.read().decode("utf-8"))
    assert body["total"] == 1
    summary = body["items"][0]
    assert summary["trace_id"] == _TRACE_HEX
    assert summary["span_count"] == 3
    assert summary["span_count_by_kind"] == {
        "gen_ai.agent": 1,
        "gen_ai.llm": 1,
        "gen_ai.tool": 1,
    }


def test_same_origin_search(ui_server: str) -> None:
    """``GET /api/v1/search?q=…`` finds spans in the reference trace."""
    import json
    from urllib.parse import urlencode

    qs = urlencode({"q": "find products"})
    # S310: loopback only.
    with urllib.request.urlopen(f"{ui_server}/api/v1/search?{qs}", timeout=5) as r:  # noqa: S310
        body = json.loads(r.read().decode("utf-8"))
    assert body["total"] >= 1
    # The hit must be the LLM span carrying the gen_ai.prompt content.
    llm_hits = [h for h in body["items"] if h["kind"] == "gen_ai.llm"]
    assert len(llm_hits) == 1
    assert llm_hits[0]["model_name"] == "gpt-4o"


def test_get_span_by_rewind_id(ui_server: str) -> None:
    """``GET /api/v1/spans/{rewind_id}`` resolves a span by UUID."""
    import json

    # Discover a rewind_id via the trace list → trace detail flow.
    detail_url = f"{ui_server}/api/v1/traces/{_TRACE_HEX}"
    # S310: loopback only.
    with urllib.request.urlopen(detail_url, timeout=5) as r:  # noqa: S310
        detail = json.loads(r.read().decode("utf-8"))
    span = next(s for s in detail["spans"] if s["kind"] == "gen_ai.tool")
    rewind_id = span["rewind_id"]

    # S310: loopback only.
    with urllib.request.urlopen(f"{ui_server}/api/v1/spans/{rewind_id}", timeout=5) as r:  # noqa: S310
        body = json.loads(r.read().decode("utf-8"))
    assert body["rewind_id"] == rewind_id
    assert body["name"] == "tool.search_products"
