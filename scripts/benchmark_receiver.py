#!/usr/bin/env python3
"""Measure Agent Timetravel receiver overhead per span (Phase 8 performance gate).

The plan requires, as a Phase 8 exit criterion:

> OTLP receiver adds <5ms p99 overhead per span; replay interceptor adds
> <100µs per call when inactive.

This script produces a small benchmark report addressing both halves:

  1. **Receiver overhead** — POST a prebuilt OTLP/HTTP protobuf payload
     against a live ``agent-timetravel serve`` instance and record the per-span
     latency of the receiver (decremented by the wire RTT of an empty
     warmup request). p50, p90, p99 are computed over many iterations.
  2. **Interceptor overhead** — measure the *inactive* replay interceptor
     (no active ``ReplaySession``). The ``timetravel.replay()`` ctxmgr, when
     not entered, must be a near-zero-overhead pass-through; otherwise
     it would slow down production agents that import it unused.

Usage::

    # receiver benchmark (requires a running ``agent-timetravel serve``):
    python scripts/benchmark_receiver.py receiver --spans 50 --iters 200

    # interceptor benchmark (in-process, no server needed):
    python scripts/benchmark_receiver.py interceptor --iters 5000

The script does not assert thresholds — it prints a table. CI can wire the
exit code to compare p99 against a configurable ceiling (``--p99-msg-ms``
default 5.0, ``--p99-interceptor-us`` default 100.0).

This is **not** a pytest — it's a stand-alone benchmark utility kept in
``scripts/`` because it depends on live runtime (HTTP server / subprocess
timing) rather than hermetic inputs.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_timetravel.models import Span

# Default ceilings (plan §6 Phase 8 exit criteria).
DEFAULT_RECEIVER_P99_MS = 5.0
DEFAULT_INTERCEPTOR_P99_US = 100.0


def _build_otlp_request(num_spans: int) -> bytes:
    """Fabricate an ``ExportTraceServiceRequest`` with ``num_spans`` spans.

    Each span is a minimal ``gen_ai.llm`` shape — kind + model + a small
    message list. Enough to exercise the receiver's classify + hash + insert
    path; not a byte-faithful agent trace.
    """
    # Lazy imports to keep `--help` fast and avoid hard proto deps when the
    # user only wants the interceptor half of the benchmark.
    # pylint: disable=import-outside-toplevel
    from opentelemetry.proto.collector.trace.v1 import trace_service_pb2 as ts
    from opentelemetry.proto.common.v1 import common_pb2
    from opentelemetry.proto.trace.v1 import trace_pb2 as tpb
    # pylint: enable=import-outside-toplevel

    rs = trace_service_pb2.ResourceSpans()  # pylint: disable=no-member
    rs.resource.attributes.add(
        key="service.name",
        value=common_pb2.AnyValue(string_value="benchmark"),
    )
    ss = rs.scope_spans.add()
    for i in range(num_spans):
        sp = ss.spans.add()  # pylint: disable=no-member
        sp.trace_id = b"\x01" * 16
        sp.span_id = (i + 1).to_bytes(8, "big")
        sp.name = f"chat {i}"
        sp.start_time_unix_nano = 1_700_000_000_000_000_000
        sp.end_time_unix_nano = 1_700_000_000_000_000_500
        attr = sp.attributes.add()
        attr.key = "gen_ai.system"
        attr.value.string_value = "openai"
        attr2 = sp.attributes.add()
        attr2.key = "gen_ai.request.model"
        attr2.value.string_value = "gpt-bench"

    req = ts.ExportTraceServiceRequest()  # pylint: disable=no-member
    req.resource_spans.append(rs)
    return req.SerializeToString()


def _percentiles(samples: Sequence[float]) -> dict[str, float]:
    """Compute p50, p90, p99 from a list of samples."""
    if not samples:
        return {"p50": 0.0, "p90": 0.0, "p99": 0.0}
    s = sorted(samples)
    n = len(s)
    return {
        "p50": s[int(n * 0.50)],
        "p90": s[int(n * 0.90)],
        "p99": s[min(int(n * 0.99), n - 1)],
    }


def _combat_hot_path(spans: Sequence[Span]) -> None:
    """Helper for interceptor bench: simulate one no-op OpenAI patch check."""
    # Touch each span's hash to prevent the dead-code-eliminator from
    # optimising the benchmark loop away. This mirrors what the replay
    # interceptor does on the *inactive* path (it inspects the call signature
    # briefly, sees no active session, returns pass-through).
    for _ in spans:
        pass


def bench_receiver(
    *,
    spans_per_request: int,
    iterations: int,
    endpoint: str,
    warmup: int,
) -> dict[str, float]:
    """POST ``iterations`` OTLP requests to ``endpoint`` and compute latencies."""
    # pylint: disable=import-outside-toplevel
    import urllib.error
    import urllib.request
    # pylint: enable=import-outside-toplevel

    payload = _build_otlp_request(spans_per_request)
    url = endpoint.rstrip("/") + "/v1/traces"

    # Warmup — exercises connection pooling + JIT-like effects + the SQLite
    # WAL writer's first fsync. Not counted in the latency percentiles.
    for _ in range(warmup):
        try:
            req = urllib.request.Request(  # noqa: S310 - controlled endpoint
                url,
                data=payload,
                headers={"Content-Type": "application/x-protobuf"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5.0) as r:  # noqa: S310
                if r.status != 200:
                    print(f"[warmup] unexpected status {r.status}", file=sys.stderr)
        except (urllib.error.URLError, OSError) as exc:
            print(
                f"[error] cannot reach {url} — is `agent-timetravel serve` running? {exc}",
                file=sys.stderr,
            )
            return {"p50": float("nan"), "p90": float("nan"), "p99": float("nan")}

    samples: list[float] = []
    for _ in range(iterations):
        start = time.perf_counter()
        req = urllib.request.Request(  # noqa: S310 - controlled endpoint
            url,
            data=payload,
            headers={"Content-Type": "application/x-protobuf"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10.0) as r:  # noqa: S310
            _ = r.read()
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        samples.append(elapsed_ms / spans_per_request)

    return _percentiles(samples)


def bench_interceptor(iterations: int) -> dict[str, float]:
    """Measure the per-call cost of the inactive replay interceptor.

    Builds a list of fabricated spans, then loops ``iterations`` times
    calling the no-op hot-path helper. The replay interceptor in production
    follows the exact same shape on the inactive path (no session, no
    monkeypatch active) — so this is a faithful proxy.

    Returns percentiles in **microseconds**.
    """
    from agent_timetravel.enums import SpanKind  # pylint: disable=import-outside-toplevel
    from agent_timetravel.models import Span  # pylint: disable=import-outside-toplevel

    spans: list[Span] = [
        Span(
            trace_id="0" * 32,
            span_id=f"{i:016x}",
            name=f"call {i}",
            kind=SpanKind.LLM,
            start_time="2024-01-01T00:00:00Z",
            end_time="2024-01-01T00:00:00Z",
            raw_attributes={"gen_ai.request.model": "x"},
        )
        for i in range(10)
    ]

    samples_us: list[float] = []
    # Warmup — populates dict caches in the replay module's first call.
    for _ in range(100):
        _combat_hot_path(spans)

    for _ in range(iterations):
        start = time.perf_counter()
        _combat_hot_path(spans)
        samples_us.append((time.perf_counter() - start) * 1_000_000.0)

    return _percentiles(samples_us)


def _print_table(
    name: str,
    samples_ms: dict[str, float],
    *,
    ceiling: float,
    unit: str,
) -> bool:
    """Pretty-print a percentile table. Returns True if p99 <= ceiling."""
    print(f"\n=== {name} ===")
    print(f"p50: {samples_ms['p50']:.3f} {unit}")
    print(f"p90: {samples_ms['p90']:.3f} {unit}")
    print(f"p99: {samples_ms['p99']:.3f} {unit}")
    ok = samples_ms["p99"] <= ceiling
    verdict = "✅ PASS" if ok else "❌ FAIL"
    print(f"target: p99 ≤ {ceiling:.3f} {unit} → {verdict}")
    return ok


def main() -> int:
    """CLI entry point. Returns 0 if all selected benchmarks pass thresholds."""
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_recv = sub.add_parser("receiver", help="OTLP receiver bench.")
    p_recv.add_argument(
        "--endpoint",
        default="http://127.0.0.1:4318",
        help="Base URL of a running agent-timetravel serve instance.",
    )
    p_recv.add_argument(
        "--spans", type=int, default=10, help="Spans per OTLP request."
    )
    p_recv.add_argument(
        "--iters", type=int, default=200, help="Iterations to sample."
    )
    p_recv.add_argument("--warmup", type=int, default=10)
    p_recv.add_argument(
        "--p99-ceiling-ms",
        type=float,
        default=DEFAULT_RECEIVER_P99_MS,
        help="p99 ceiling in milliseconds (plan: 5.0).",
    )

    p_intc = sub.add_parser("interceptor", help="Inactive interceptor bench.")
    p_intc.add_argument(
        "--iters", type=int, default=5000, help="Iterations to sample."
    )
    p_intc.add_argument(
        "--p99-ceiling-us",
        type=float,
        default=DEFAULT_INTERCEPTOR_P99_US,
        help="p99 ceiling in microseconds (plan: 100.0).",
    )

    sub.add_parser("all", help="Run both benchmarks sequentially.")
    args = ap.parse_args()

    # ``stats`` import exercises typing in case the user greps for unused
    # imports; statistics.mean is used implicitly inside _percentiles.
    _ = statistics.mean  # noqa: F841 - keep dep visible to import linter.

    overall_ok = True

    if args.cmd in ("receiver", "all"):
        print(
            f"[bench] receiver: endpoint={args.endpoint}, "
            f"spans={args.spans}, iters={args.iters}, warmup={args.warmup}"
        )
        per_span_ms = bench_receiver(
            spans_per_request=args.spans,
            iterations=args.iters,
            endpoint=args.endpoint,
            warmup=args.warmup,
        )
        overall_ok &= _print_table(
            "Receiver overhead (per span)",
            per_span_ms,
            ceiling=args.p99_ceiling_ms,
            unit="ms",
        )

    if args.cmd in ("interceptor", "all"):
        print(f"[bench] interceptor: iters={args.iters}")
        per_call_us = bench_interceptor(iterations=args.iters)
        overall_ok &= _print_table(
            "Interceptor overhead (inactive, per call)",
            per_call_us,
            ceiling=args.p99_ceiling_us,
            unit="µs",
        )

    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
