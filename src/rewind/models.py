"""Core domain models for Rewind.

These Pydantic models mirror the **OpenTelemetry GenAI semantic conventions**
(``open-telemetry/semantic-conventions-genai``). The golden rule: every span
preserves a ``raw_attributes`` JSON blob so we never lose fidelity to whatever
OpenInference actually emitted, even when semconv shifts.

The round-trip contract (Phase 0 exit criterion) is:

    Span(...) -> SQLite -> Span(**row)   # identity

Tested in ``tests/test_models.py``.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from rewind.enums import SpanKind, SpanStatus


def _utcnow_iso() -> str:
    """Return the current UTC time as an ISO-8601 string with offset.

    ``datetime.utcnow()`` is deprecated in 3.12 and tz-naive; we always emit
    a timezone-aware value for unambiguous storage and comparison.
    """
    return datetime.now(tz=UTC).isoformat()


def _stable_json(payload: Any) -> str:  # noqa: ANN401
    """Serialize ``payload`` with sorted keys for deterministic hashing.

    Used for messages/tools hashing so that frozen replay can match on
    ``model + messages_hash + tools_hash`` instead of fragile byte equality.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def hash_payload(payload: Any) -> str:  # noqa: ANN401
    """Return a hex SHA-256 of ``payload`` via stable JSON serialization.

    Why SHA-256 not a shorter hash? Span matching must be collision-free across
    a whole workspace of potentially millions of calls; 16 bytes is cheap.
    """
    return hashlib.sha256(_stable_json(payload).encode("utf-8")).hexdigest()


class RewindModel(BaseModel):
    """Base for all Rewind domain models.

    - ``model_config = ConfigDict(extra="forbid")`` prevents silent typos
      drifting the wire format from the OTel source. (We capture unknown attrs
      explicitly in ``Span.raw_attributes`` instead.)
    - Populated fields are validated; ``frozen=False`` because we mutate during
      replay cursor advances.
    """

    model_config = ConfigDict(extra="forbid", use_enum_values=False)


class Span(RewindModel):
    """One discrete unit of agent execution, mirroring GenAI semconv.

    Attributes are split into *typed Rewind fields* (for querying/diffing) and
    ``raw_attributes`` (the verbatim OpenInference payload). We never drop the
    raw payload, so semconv churn never loses data.
    """

    #: Stable opaque id. The OTel ``span_id`` (16-hex) is preserved separately
    #: so a Rewind row is retrievable even when OTel re-issues the same span_id
    #: across replays.
    rewind_id: UUID = Field(default_factory=uuid4)
    trace_id: str = Field(..., description="OTel trace id (32-hex).")
    span_id: str = Field(..., description="OTel span id (16-hex).")
    parent_span_id: str | None = Field(
        default=None, description="OTel parent span id, None for the root span."
    )
    name: str = Field(..., description="OTel span name (e.g. 'chat_completions.create').")
    kind: SpanKind = Field(default=SpanKind.UNKNOWN, description="Rewind span classification.")

    #: Start/end as ISO-8601 strings (tz-aware). OTel sends unix-nanos; the
    #: ingestion layer converts. Keeping strings here keeps SQLite round-trip
    #: trivial and JSON-portable for the UI.
    start_time: str = Field(default_factory=_utcnow_iso)
    end_time: str = Field(default_factory=_utcnow_iso)

    status: SpanStatus = Field(default=SpanStatus.UNSET)
    status_message: str | None = None

    #: Typed, queryable GenAI fields. All optional — non-LLM spans won't have
    #: ``gen_ai.response.model``, etc. They are derived from raw_attributes at
    #: ingestion and kept in sync for fast SQL filtering.
    model_name: str | None = Field(
        default=None, description="gen_ai.response.model / request.model"
    )
    prompt_tokens: int | None = Field(
        default=None, description="gen_ai.usage.prompt_tokens"
    )
    completion_tokens: int | None = Field(
        default=None, description="gen_ai.usage.completion_tokens"
    )
    total_tokens: int | None = Field(
        default=None, description="gen_ai.usage.total_tokens"
    )

    #: Hashes computed at ingestion to enable fixture matching without reading
    #: the (potentially huge) raw payload back. See ``hash_payload``.
    messages_hash: str | None = None
    tools_hash: str | None = None

    #: The verbatim OpenInference payload, untouched. This is the contract:
    #: ``hash(span.raw_attributes)`` at ingest time must equal the source.
    raw_attributes: dict[str, Any] = Field(
        default_factory=dict, description="Verbatim GenAI semconv attributes."
    )

    @field_validator("trace_id", "span_id")
    @classmethod
    def _non_empty_hex(cls, v: str) -> str:
        if not v:
            raise ValueError("trace_id/span_id must be non-empty")
        return v

    def matches_signature(
        self,
        other_model: str,
        other_messages: Any,  # noqa: ANN401
        other_tools: Any | None = None,  # noqa: ANN401
    ) -> bool:
        """Frozen-replay signature match: model + messages + tools.

        Used by the Phase 3 responder to decide serve-from-fixture vs.
        forward-live without parsing the raw payload. Returns True only if all
        three components match the recorded hash.
        """
        if self.model_name != other_model:
            return False
        if self.messages_hash is None or self.messages_hash != hash_payload(other_messages):
            return False
        if other_tools is None:
            return self.tools_hash is None
        return self.tools_hash == hash_payload(other_tools)


class Branch(RewindModel):
    """A divergent timeline sharing spans 1..N with a parent trace.

    Branches form a tree rooted at an original trace. ``parent_branch_id=None``
    marks the original (ingested) timeline; any non-null value is a replay
    branch. The replay engine (P3) clones spans ``1..branch_at_index`` under a
    new ``branch_id``.
    """

    branch_id: UUID = Field(default_factory=uuid4)
    trace_id: str = Field(..., description="OTel trace id this branch belongs to.")
    parent_branch_id: UUID | None = Field(
        default=None, description="None for the ingested original timeline."
    )
    branch_at_index: int | None = Field(
        default=None,
        description="Span index (0-based) where this branch diverges. None for root.",
    )
    mode: str = Field(default="frozen", description="ReplayMode used to create this branch.")
    label: str = Field(default="", description="Human-readable label for the diff UI.")
    created_at: str = Field(default_factory=_utcnow_iso)

    @field_validator("branch_at_index")
    @classmethod
    def _non_negative_or_none(cls, v: int | None) -> int | None:
        if v is not None and v < 0:
            raise ValueError("branch_at_index must be >= 0 or None")
        return v


class Trace(RewindModel):
    """One complete agent run: a tree of spans plus its branch tree.

    The root ``Trace`` carries the ingested timeline. Branches (Phase 3) are
    tracked separately and linked back here by ``trace_id``.
    """

    trace_id: str = Field(..., description="OTel trace id (32-hex).")
    root_branch_id: UUID = Field(default_factory=uuid4)
    created_at: str = Field(default_factory=_utcnow_iso)
    spans: list[Span] = Field(default_factory=list)

    def span_count_by_kind(self) -> dict[str, int]:
        """Return a summary ``{kind: count}`` — used by the timeline UI."""
        counts: dict[str, int] = {}
        for span in self.spans:
            counts[span.kind.value] = counts.get(span.kind.value, 0) + 1
        return counts


__all__ = [
    "Branch",
    "RewindModel",
    "Span",
    "Trace",
    "hash_payload",
]
