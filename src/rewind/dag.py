"""Phase 5.1 — execution DAG builder.

Converts a flat list of spans into a typed parent → children tree so the UI
can render the causal/structural shape of an agent run (which LLM call
spawned which tool call, etc.) rather than just a flat timeline.

The DAG is built from ``parent_span_id`` pointers — every span carries one
(or ``None`` for roots). This module is pure logic: it takes spans, returns
:class:`DAGNode` trees. The timeline API exposes it via
``GET /api/v1/traces/{trace_id}/dag``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rewind.models import Span

__all__ = ["DAGNode", "build_dag"]


@dataclass
class DAGNode:
    """One node in the execution DAG.

    ``children`` are ordered by ``start_time`` so the UI renders calls in
    execution order. Carries the minimal fields the DAG renderer needs;
    the full span detail is fetched separately via the span API.
    """

    span_id: str
    name: str
    kind: str
    status: str
    parent_span_id: str | None
    start_time: str
    children: list[DAGNode] = field(default_factory=list)


def build_dag(spans: list[Span]) -> list[DAGNode]:
    """Build a forest of :class:`DAGNode` trees from a flat span list.

    Returns a list of root nodes (spans with ``parent_span_id is None``).
    Non-root spans whose parent isn't in the set are treated as roots too
    (defensive against partial traces / filtered exports).

    Children within each node are sorted by ``start_time`` ascending so the
    UI renders sub-calls in execution order.
    """
    by_id: dict[str, DAGNode] = {}
    for span in spans:
        node = DAGNode(
            span_id=span.span_id,
            name=span.name,
            kind=span.kind.value,
            status=span.status.value,
            parent_span_id=span.parent_span_id,
            start_time=span.start_time,
        )
        by_id[span.span_id] = node

    roots: list[DAGNode] = []
    for span in spans:
        node = by_id[span.span_id]
        parent_id = span.parent_span_id
        if parent_id is None or parent_id not in by_id:
            roots.append(node)
        else:
            by_id[parent_id].children.append(node)

    # Sort children by start_time for stable rendering.
    for node in by_id.values():
        node.children.sort(key=lambda n: n.start_time)
    roots.sort(key=lambda n: n.start_time)
    return roots
