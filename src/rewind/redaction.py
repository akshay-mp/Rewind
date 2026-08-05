"""Phase 5.4 — configurable redaction for trace export.

Provides a :class:`RedactionPolicy` the export path accepts so sensitive
fields can be scrubbed before a trace leaves the machine. The policy is
declarative: a list of field names to drop and a list of regex patterns to
mask in any string value.

Design
------
* Field-name redaction is exact-match against top-level keys in
  ``raw_attributes`` (e.g. ``"gen_ai.response"``).
* Pattern redaction runs against every string value in the span's
  ``raw_attributes`` recursively, replacing matches with ``[REDACTED]``.
* ``preview`` mode returns a count of what *would* be redacted without
  mutating the spans, so the CLI can show "3 fields, 12 pattern matches"
  before committing to an export.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from rewind.models import Span

__all__ = ["RedactionPolicy", "apply_redaction", "preview_redaction"]


@dataclass
class RedactionPolicy:
    """Declarative redaction rules for trace export.

    * ``fields`` — top-level ``raw_attributes`` keys to drop entirely.
    * ``patterns`` — regex patterns; matches in any string value are replaced
      with ``[REDACTED]``.
    * ``replacement`` — the string substituted for redacted content.
    """

    fields: set[str] = field(default_factory=set)
    patterns: list[re.Pattern[str]] = field(default_factory=list)
    replacement: str = "[REDACTED]"

    @classmethod
    def from_cli(
        cls,
        *,
        redact_fields: list[str] | None = None,
        redact_patterns: list[str] | None = None,
    ) -> RedactionPolicy:
        """Build a policy from CLI ``--redact-field`` / ``--redact-pattern`` lists."""
        return cls(
            fields=set(redact_fields or []),
            patterns=[re.compile(p) for p in (redact_patterns or [])],
        )


def apply_redaction(spans: list[Span], policy: RedactionPolicy) -> list[Span]:
    """Return new spans with ``policy`` applied (originals untouched)."""
    if not policy.fields and not policy.patterns:
        return list(spans)
    return [_redact_span(s, policy) for s in spans]


def preview_redaction(spans: list[Span], policy: RedactionPolicy) -> dict[str, int]:
    """Count what *would* be redacted without mutating the spans.

    Returns ``{"fields_dropped": N, "pattern_matches": M}``.
    """
    fields_dropped = 0
    pattern_matches = 0
    for span in spans:
        attrs = span.raw_attributes or {}
        for key in policy.fields:
            if key in attrs:
                fields_dropped += 1
        if policy.patterns:
            for value in attrs.values():
                pattern_matches += _count_pattern_matches(value, policy.patterns)
    return {"fields_dropped": fields_dropped, "pattern_matches": pattern_matches}


def _redact_span(span: Span, policy: RedactionPolicy) -> Span:
    """Apply redaction to one span's raw_attributes, returning a new Span."""
    attrs = dict(span.raw_attributes or {})
    for key in list(attrs.keys()):
        if key in policy.fields:
            attrs.pop(key, None)
        else:
            attrs[key] = _redact_value(attrs[key], policy)
    return span.model_copy(update={"raw_attributes": attrs})


def _redact_value(value: Any, policy: RedactionPolicy) -> Any:  # noqa: ANN401
    """Recursively redact a value (string / dict / list)."""
    if isinstance(value, str):
        result = value
        for pattern in policy.patterns:
            result = pattern.sub(policy.replacement, result)
        return result
    if isinstance(value, dict):
        return {k: _redact_value(v, policy) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(v, policy) for v in value]
    return value


def _count_pattern_matches(value: Any, patterns: list[re.Pattern[str]]) -> int:  # noqa: ANN401
    """Count total pattern matches in a value (recursive)."""
    if isinstance(value, str):
        return sum(len(p.findall(value)) for p in patterns)
    if isinstance(value, dict):
        return sum(_count_pattern_matches(v, patterns) for v in value.values())
    if isinstance(value, list):
        return sum(_count_pattern_matches(v, patterns) for v in value)
    return 0
