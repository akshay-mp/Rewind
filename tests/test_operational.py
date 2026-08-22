"""Unit tests for Phase 5 — operational hardening.

Covers:

* **5.1** — :mod:`timetravel.dag` (build_dag) + ``GET /traces/{id}/dag``.
* **5.2** — :class:`timetravel.models.LatencyBreakdown`.
* **5.3** — :mod:`timetravel.reproducibility` (capture_manifest) + storage.
* **5.4** — :mod:`timetravel.redaction` (RedactionPolicy + apply/preview) +
  the ``timetravel export`` CLI.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_timetravel.cli import cli
from agent_timetravel.dag import build_dag
from agent_timetravel.enums import SpanKind, SpanStatus
from agent_timetravel.models import LatencyBreakdown, Span, Trace
from agent_timetravel.redaction import RedactionPolicy, apply_redaction, preview_redaction
from agent_timetravel.reproducibility import capture_manifest
from agent_timetravel.storage import TraceStore
from agent_timetravel.timeline import mount_timeline

_TRACE_ID = "a" * 32


# --- helpers ---------------------------------------------------------------


def _span(
    *,
    span_id: str,
    parent: str | None,
    name: str,
    kind: SpanKind = SpanKind.LLM,
    start: str = "2026-08-03T00:00:00+00:00",
) -> Span:
    return Span(
        trace_id=_TRACE_ID,
        span_id=span_id,
        parent_span_id=parent,
        name=name,
        kind=kind,
        status=SpanStatus.UNSET,
        status_message=None,
        model_name=None,
        prompt_tokens=None,
        completion_tokens=None,
        total_tokens=None,
        raw_attributes={},
        start_time=start,
        end_time=start,
    )


def _seed_tree(store: TraceStore) -> None:
    """Seed a 3-span tree: root → child1, child2."""
    spans = [
        _span(span_id="1" * 16, parent=None, name="root", kind=SpanKind.AGENT),
        _span(
            span_id="2" * 16,
            parent="1" * 16,
            name="llm-1",
            start="2026-08-03T00:00:01+00:00",
        ),
        _span(
            span_id="3" * 16,
            parent="1" * 16,
            name="tool-1",
            kind=SpanKind.TOOL,
            start="2026-08-03T00:00:02+00:00",
        ),
    ]
    store.upsert_trace(Trace(trace_id=_TRACE_ID, spans=spans))
    for s in spans:
        store.insert_span(s)


@pytest.fixture
def store(tmp_path: Path) -> TraceStore:
    s = TraceStore(str(tmp_path / "op_harden.db"))
    _seed_tree(s)
    return s


# ===========================================================================
# 5.1 — execution DAG
# ===========================================================================


class TestBuildDag:
    def test_builds_parent_child_tree(self, store: TraceStore) -> None:
        spans = store.get_spans(_TRACE_ID)
        roots = build_dag(spans)
        assert len(roots) == 1
        root = roots[0]
        assert root.span_id == "1" * 16
        assert root.name == "root"
        assert len(root.children) == 2
        # Children sorted by start_time.
        assert root.children[0].span_id == "2" * 16
        assert root.children[1].span_id == "3" * 16

    def test_orphan_becomes_root(self) -> None:
        """A span whose parent isn't in the set is treated as a root."""
        orphan = _span(span_id="9" * 16, parent="missing", name="orphan")
        roots = build_dag([orphan])
        assert len(roots) == 1
        assert roots[0].span_id == "9" * 16

    def test_empty_spans(self) -> None:
        assert build_dag([]) == []


class TestDagEndpoint:
    @pytest.fixture
    def client(self, store: TraceStore) -> TestClient:
        app = FastAPI()
        app.state.store = store
        mount_timeline(app)
        return TestClient(app)

    def test_get_dag(self, client: TestClient) -> None:
        resp = client.get(f"/api/v1/traces/{_TRACE_ID}/dag")
        assert resp.status_code == 200
        roots = resp.json()
        assert len(roots) == 1
        assert roots[0]["span_id"] == "1" * 16
        assert len(roots[0]["children"]) == 2

    def test_missing_trace_404(self, client: TestClient) -> None:
        resp = client.get(f"/api/v1/traces/{'z' * 32}/dag")
        assert resp.status_code == 404


# ===========================================================================
# 5.2 — latency breakdown
# ===========================================================================


class TestLatencyBreakdown:
    def test_defaults_all_none(self) -> None:
        lb = LatencyBreakdown()
        assert lb.queue_ms is None
        assert lb.ttft_ms is None
        assert lb.generation_ms is None
        assert lb.total_ms is None

    def test_frozen_dataclass(self) -> None:
        lb = LatencyBreakdown(ttft_ms=120.5, total_ms=500.0)
        with pytest.raises(AttributeError, match="cannot assign"):
            lb.ttft_ms = 999  # type: ignore[misc]


# ===========================================================================
# 5.3 — reproducibility manifest
# ===========================================================================


class TestReproducibility:
    def test_capture_manifest_has_fields(self) -> None:
        m = capture_manifest(timetravel_version="0.1.0")
        assert m.timetravel_version == "0.1.0"
        assert m.python_version
        assert m.platform
        assert m.content_hash
        assert len(m.content_hash) == 16

    def test_content_hash_stable(self) -> None:
        m1 = capture_manifest(timetravel_version="0.1.0")
        m2 = capture_manifest(timetravel_version="0.1.0")
        assert m1.content_hash == m2.content_hash

    def test_different_version_changes_hash(self) -> None:
        m1 = capture_manifest(timetravel_version="0.1.0")
        m2 = capture_manifest(timetravel_version="0.2.0")
        assert m1.content_hash != m2.content_hash

    def test_storage_round_trip(self, store: TraceStore) -> None:
        m = capture_manifest(timetravel_version="0.1.0")
        manifest_dict = {**m.to_dict(), "captured_at": "2026-08-03T00:00:00+00:00"}
        store.upsert_run_environment(manifest_dict)
        fetched = store.get_run_environment(m.content_hash)
        assert fetched is not None
        assert fetched["timetravel_version"] == "0.1.0"


# ===========================================================================
# 5.4 — redaction
# ===========================================================================


class TestRedaction:
    def _span_with_pii(self) -> Span:
        return Span(
            trace_id=_TRACE_ID,
            span_id="1" * 16,
            parent_span_id=None,
            name="llm",
            kind=SpanKind.LLM,
            status=SpanStatus.UNSET,
            status_message=None,
            model_name=None,
            prompt_tokens=None,
            completion_tokens=None,
            total_tokens=None,
            raw_attributes={
                "gen_ai.response": {"content": "SSN is 123-45-6789"},
                "user_input": "email is user@example.com",
            },
        )

    def test_field_redaction_drops_key(self) -> None:
        policy = RedactionPolicy.from_cli(redact_fields=["gen_ai.response"])
        spans = apply_redaction([self._span_with_pii()], policy)
        assert "gen_ai.response" not in spans[0].raw_attributes
        assert "user_input" in spans[0].raw_attributes  # untouched

    def test_pattern_redaction_masks_values(self) -> None:
        policy = RedactionPolicy.from_cli(redact_patterns=[r"\d{3}-\d{2}-\d{4}"])
        spans = apply_redaction([self._span_with_pii()], policy)
        ssn_blob = json.dumps(spans[0].raw_attributes)
        assert "123-45-6789" not in ssn_blob
        assert "[REDACTED]" in ssn_blob

    def test_no_policy_returns_originals(self) -> None:
        original = self._span_with_pii()
        spans = apply_redaction([original], RedactionPolicy())
        assert spans[0].raw_attributes == original.raw_attributes

    def test_preview_counts_without_mutating(self) -> None:
        policy = RedactionPolicy.from_cli(
            redact_fields=["gen_ai.response"],
            redact_patterns=[r"\d{3}-\d{2}-\d{4}"],
        )
        spans = [self._span_with_pii()]
        before = dict(spans[0].raw_attributes)
        counts = preview_redaction(spans, policy)
        assert counts["fields_dropped"] == 1
        assert counts["pattern_matches"] >= 1
        # Unmutated.
        assert spans[0].raw_attributes == before

    def test_default_cli_policy_preserves_usage_metrics_and_redacts_nested_secrets(self) -> None:
        span = self._span_with_pii().model_copy(update={
            "raw_attributes": {
                "usage": {"input_tokens": 12, "total_tokens": 15},
                "nested": {
                    "email": "user@example.com",
                    "authorization": "Bearer abc.def.ghi",
                    "api_key": "sk-test-secret-value",
                },
            }
        })
        redacted = apply_redaction([span], RedactionPolicy.from_cli())[0].raw_attributes
        assert redacted["usage"] == {"input_tokens": 12, "total_tokens": 15}
        assert redacted["nested"] == {
            "email": "[REDACTED]",
            "authorization": "[REDACTED]",
            "api_key": "[REDACTED]",
        }


class TestExportCli:
    def test_export_to_stdout(self, tmp_path: Path) -> None:
        s = TraceStore(str(tmp_path / "export.db"))
        _seed_tree(s)
        runner = CliRunner()
        result = runner.invoke(
            cli, ["export", _TRACE_ID, "--db", str(tmp_path / "export.db")]
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["trace_id"] == _TRACE_ID
        assert len(payload["spans"]) == 3


class TestPricingProfiles:
    def test_upsert_and_list_profile(self, store: TraceStore) -> None:
        store.upsert_pricing_profile(
            {
                "profile_id": "local-qwen",
                "name": "Qwen local",
                "provider": "ollama",
                "model": "qwen3",
                "input_per_million": 0,
                "output_per_million": 0,
                "effective_at": "2026-08-05T00:00:00Z",
            }
        )
        profile = store.get_pricing_profile("local-qwen")
        assert profile is not None
        assert profile["model"] == "qwen3"
        assert profile["output_per_million"] == 0
        assert store.list_pricing_profiles()[0]["profile_id"] == "local-qwen"

    def test_http_profile_api(self, store: TraceStore) -> None:
        app = FastAPI()
        app.state.store = store
        mount_timeline(app)
        client = TestClient(app)
        response = client.post(
            "/api/v1/pricing-profiles",
            json={"profile_id": "http-local", "name": "HTTP local"},
        )
        assert response.status_code == 201
        listed = client.get("/api/v1/pricing-profiles")
        assert listed.status_code == 200
        assert any(item["profile_id"] == "http-local" for item in listed.json())

    def test_export_to_file(self, tmp_path: Path) -> None:
        db = tmp_path / "export_file.db"
        s = TraceStore(str(db))
        _seed_tree(s)
        out = tmp_path / "trace.json"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["export", _TRACE_ID, "--db", str(db), "-o", str(out)],
        )
        assert result.exit_code == 0
        assert out.exists()
        payload = json.loads(out.read_text())
        assert len(payload["spans"]) == 3

    def test_export_with_redact_field(self, tmp_path: Path) -> None:
        db = tmp_path / "redact.db"
        s = TraceStore(str(db))
        span = Span(
            trace_id=_TRACE_ID,
            span_id="1" * 16,
            parent_span_id=None,
            name="llm",
            kind=SpanKind.LLM,
            status=SpanStatus.UNSET,
            status_message=None,
            model_name=None,
            prompt_tokens=None,
            completion_tokens=None,
            total_tokens=None,
            raw_attributes={"secret": "shh", "public": "ok"},
        )
        s.upsert_trace(Trace(trace_id=_TRACE_ID, spans=[span]))
        s.insert_span(span)
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["export", _TRACE_ID, "--db", str(db), "--redact-field", "secret"],
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)
        attrs = payload["spans"][0]["raw_attributes"]
        assert "secret" not in attrs
        assert attrs["public"] == "ok"

    def test_export_preview(self, tmp_path: Path) -> None:
        db = tmp_path / "preview.db"
        s = TraceStore(str(db))
        span = Span(
            trace_id=_TRACE_ID,
            span_id="1" * 16,
            parent_span_id=None,
            name="llm",
            kind=SpanKind.LLM,
            status=SpanStatus.UNSET,
            status_message=None,
            model_name=None,
            prompt_tokens=None,
            completion_tokens=None,
            total_tokens=None,
            raw_attributes={"secret": "shh"},
        )
        s.upsert_trace(Trace(trace_id=_TRACE_ID, spans=[span]))
        s.insert_span(span)
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "export", _TRACE_ID, "--db", str(db),
                "--preview", "--redact-field", "secret",
            ],
        )
        assert result.exit_code == 0
        assert "1 fields dropped" in result.output

    def test_export_missing_trace(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            cli, ["export", "n" * 32, "--db", str(tmp_path / "none.db")]
        )
        assert result.exit_code == 1
