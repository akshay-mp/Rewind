"""Unit tests for the Phase 5 :mod:`timetravel.diff` engine.

Three families of tests pin the three Phase 5 exit criteria:

1. :func:`timetravel.diff.span_diff` — *"Diffing two branches marks exactly which
   span first diverged"*.
2. :func:`timetravel.diff.message_diff` — *"Token-level message diff renders
   add/remove/change correctly"*.
3. :func:`timetravel.diff.branch_tree` — *"branch tree view"* (storage-level flat
   list → renderable tree).

The tests in this file are **pure** — they construct :class:`~timetravel.models.Span`
and :class:`~timetravel.models.Branch` directly, no SQLite. Cross-layer behaviour
lives in ``tests/test_diff_api.py`` (HTTP) and the integration suite.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from agent_timetravel.diff import (
    BranchNode,
    SpanPair,
    branch_tree,
    message_diff,
    span_diff,
)
from agent_timetravel.enums import SpanKind, SpanStatus
from agent_timetravel.models import Branch, Span

# ----------------------------------------------------------------------
# Span factory helpers — keep test setup terse without hiding the contract
# (fields must be ``raw_attributes=`` not ``attributes=``; Pydantic forbids
# extra fields).
# ----------------------------------------------------------------------


def _llm(idx: int, *, messages_hash: str = "abc", tools_hash: str | None = None) -> Span:
    return Span(
        trace_id="a" * 24 + "00000001",
        span_id=f"{idx:016x}",
        parent_span_id=None,
        name=f"llm-{idx}",
        kind=SpanKind.LLM,
        model_name="qwen3:32b",
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        messages_hash=messages_hash,
        tools_hash=tools_hash,
        start_time=f"2026-01-01T00:00:{idx:02d}Z",
        end_time=f"2026-01-01T00:00:{idx + 1:02d}Z",
        status=SpanStatus.OK,
        status_message=None,
        raw_attributes={},
    )


def _agent(idx: int, *, attr: str = "agent-a") -> Span:
    return Span(
        trace_id="a" * 24 + "00000001",
        span_id=f"{idx:016x}",
        parent_span_id=None,
        name=f"agent-{idx}",
        kind=SpanKind.AGENT,
        start_time=f"2026-01-01T00:00:{idx:02d}Z",
        end_time=f"2026-01-01T00:00:{idx + 1:02d}Z",
        status=SpanStatus.OK,
        status_message=None,
        raw_attributes={"openinference.span.kind": attr},
    )


# ----------------------------------------------------------------------
# span_diff
# ----------------------------------------------------------------------


def test_span_diff_identical_lists_have_no_divergence() -> None:
    """Two identical spans produce zero divergence rows."""
    left = [_llm(0), _llm(1), _llm(2)]
    diff = span_diff(left, list(left))

    assert diff.identical is True
    assert diff.first_divergence_index is None
    assert diff.left_count == 3
    assert diff.right_count == 3
    assert all(p.status == "equal" for p in diff.pairs)
    assert all(p.left is not None and p.right is not None for p in diff.pairs)
    # No row should carry the first-divergence sentinel.
    assert all(p.is_first_divergence is False for p in diff.pairs)


def test_span_diff_empty_lists_are_identical() -> None:
    """Edge case: two empty span lists are trivially identical."""
    diff = span_diff([], [])
    assert diff.identical is True
    assert diff.pairs == []


def test_span_diff_flags_first_divergence_index() -> None:
    """The first divergent row carries ``is_first_divergence``."""
    left = [_llm(0), _llm(1, messages_hash="aaa"), _llm(2)]
    right = [_llm(0), _llm(1, messages_hash="bbb"), _llm(2)]

    diff = span_diff(left, right)

    assert diff.identical is False
    # Pinned Phase 5 exit criterion: exactly one first-divergence row.
    first_rows = [p for p in diff.pairs if p.is_first_divergence]
    assert len(first_rows) == 1
    assert first_rows[0].index == 1
    assert first_rows[0].status == "diverged"
    assert diff.first_divergence_index == 1


def test_span_diff_compares_by_messages_hash_not_span_id() -> None:
    """Spans with same semantic identity (same hash) match despite span_id."""
    left = [_llm(0)]
    right = [_llm(0)]  # New Span with same messages_hash but different span_id.
    assert left[0].span_id == right[0].span_id  # same string by construction
    # Force different span_id on the right while keeping the hash:
    right_mirrored = Span(
        **{**_llm(0).__dict__, "span_id": "ffffffffffffffff"}
    )

    diff = span_diff(left, [right_mirrored])

    assert diff.identical is True
    assert diff.pairs[0].left.messages_hash == diff.pairs[0].right.messages_hash


def test_span_diff_handles_left_only_and_right_only() -> None:
    """Asymmetric lengths produce left_only / right_only statuses."""
    left = [_llm(0), _llm(1)]
    right = [_llm(0), _llm(1), _llm(2), _llm(3)]

    diff = span_diff(left, right)

    assert diff.left_count == 2
    assert diff.right_count == 4
    assert [p.status for p in diff.pairs] == [
        "equal",
        "equal",
        "right_only",
        "right_only",
    ]
    assert diff.pairs[2].is_first_divergence is True


def test_span_diff_agent_kind_falls_back_to_raw_attributes() -> None:
    """AGENT spans have no messages_hash — divergence is decided by raw attrs."""
    left = [_agent(0, attr="x"), _agent(1, attr="y")]
    right = [_agent(0, attr="x"), _agent(1, attr="z")]

    diff = span_diff(left, right)

    assert diff.first_divergence_index == 1
    assert diff.pairs[0].status == "equal"
    assert diff.pairs[1].status == "diverged"


def test_span_diff_different_kinds_always_diverge() -> None:
    """A different ``SpanKind`` is always a divergence."""
    left = [_llm(0)]
    right = [_agent(0)]

    diff = span_diff(left, right)

    assert diff.first_divergence_index == 0


def test_span_diff_first_pair_is_divergence_when_index_zero_diverges() -> None:
    """Divergence at index 0 still produces exactly one sentinel row."""
    left = [_llm(0, messages_hash="x")]
    right = [_llm(0, messages_hash="y")]

    diff = span_diff(left, right)

    assert diff.first_divergence_index == 0
    assert diff.pairs[0].is_first_divergence is True
    # No false positives on subsequent rows.
    assert sum(p.is_first_divergence for p in diff.pairs) == 1


# ----------------------------------------------------------------------
# Phase 7 — quant-divergence auto-flag
# ----------------------------------------------------------------------


def _llm_quant(
    idx: int,
    model: str,
    *,
    quant: str | None = None,
    messages_hash: str = "shared",
) -> Span:
    """LLM span whose divergence is driven by model/quant, not messages_hash.

    Both sides share ``messages_hash`` so the divergence is structural-via-quant
    (the Phase 7 "did the quant cause this?" hypothesis).
    """
    raw: dict[str, object] = {}
    if quant is not None:
        raw["agent_timetravel.local.quant"] = quant
    return Span(
        trace_id="a" * 24 + "00000001",
        span_id=f"{idx:016x}",
        parent_span_id=None,
        name=f"llm-{idx}",
        kind=SpanKind.LLM,
        model_name=model,
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        messages_hash=messages_hash,
        tools_hash=None,
        start_time=f"2026-01-01T00:00:{idx:02d}Z",
        end_time=f"2026-01-01T00:00:{idx + 1:02d}Z",
        status=SpanStatus.OK,
        status_message=None,
        raw_attributes=raw,
    )


def test_span_diff_quant_diverges_when_same_base_different_quant_attr() -> None:
    """Quant-divergence flag fires when same base model has different quant attr."""
    # model_name has NO quant suffix → the divergence signal comes purely
    # from the recorded timetravel.local.quant attribute.
    left = [_llm_quant(0, "qwen3:32b", quant="q4_k_m")]
    right = [_llm_quant(0, "qwen3:32b", quant="q8_0")]

    diff = span_diff(left, right)

    assert diff.quant_diverges is True


def test_span_diff_quant_diverges_when_models_have_different_quant_suffix() -> None:
    """Flag fires from on-the-fly model_name parsing when no quant attr recorded."""
    left = [_llm_quant(0, "qwen3:32b-q4_K_M")]
    right = [_llm_quant(0, "qwen3:32b-q8_0")]

    diff = span_diff(left, right)

    assert diff.quant_diverges is True


def test_span_diff_quant_does_not_diverge_when_same_quant() -> None:
    """Identical quant on both sides → flag is False even on divergent spans."""
    left = [_llm_quant(0, "qwen3:32b-q4_K_M", messages_hash="a")]
    right = [_llm_quant(0, "qwen3:32b-q4_K_M", messages_hash="b")]

    diff = span_diff(left, right)

    assert diff.quant_diverges is False


def test_span_diff_quant_does_not_diverge_for_different_base_models() -> None:
    """Different base models → a model swap, not a quant comparison."""
    left = [_llm_quant(0, "qwen3:32b-q4_K_M")]
    right = [_llm_quant(0, "llama3.1:8b-q4_K_M")]

    diff = span_diff(left, right)

    assert diff.quant_diverges is False


def test_span_diff_quant_does_not_diverge_for_cloud_models() -> None:
    """Cloud models with no quant metadata → can't flag, returns False."""
    left = [_llm_quant(0, "gpt-4o", quant=None)]
    right = [_llm_quant(0, "gpt-4o", quant=None)]

    diff = span_diff(left, right)

    assert diff.quant_diverges is False


def test_span_diff_quant_does_not_flag_empty_lists() -> None:
    """No LLM spans on either side → nothing to compare → no false flag."""
    diff = span_diff([], [])
    assert diff.quant_diverges is False


def test_span_diff_quant_aggregates_across_multiple_llm_spans() -> None:
    """Per-side quant set is collected across ALL LLM spans, not per-pair.

    Two-LLM trace where both spans switched from Q4 to Q8:
    """
    left = [
        _llm_quant(0, "qwen3:32b", quant="q4_k_m"),
        _llm_quant(1, "qwen3:32b", quant="q4_k_m"),
    ]
    right = [
        _llm_quant(0, "qwen3:32b", quant="q8_0"),
        _llm_quant(1, "qwen3:32b", quant="q8_0"),
    ]

    diff = span_diff(left, right)

    assert diff.quant_diverges is True


# ----------------------------------------------------------------------
# message_diff
# ----------------------------------------------------------------------


def test_message_diff_identical_inputs_short_circuit() -> None:
    """Identical inputs collapse to a single ``equal`` fragment."""
    txt = "The quick brown fox."
    diff = message_diff(txt, txt)

    assert diff.identical is True
    assert diff.added_tokens == 0
    assert diff.removed_tokens == 0
    assert len(diff.fragments) == 1
    assert diff.fragments[0].kind == "equal"
    assert diff.fragments[0].text == txt


def test_message_diff_empty_inputs_short_circuit() -> None:
    """Two empty strings produce zero fragments."""
    diff = message_diff("", "")

    assert diff.identical is True
    assert diff.fragments == []
    assert diff.added_tokens == 0
    assert diff.removed_tokens == 0


def test_message_diff_classifies_pure_addition() -> None:
    """Right appends tokens to left: every appended token is ``added``."""
    left = "alpha beta"
    right = "alpha beta gamma delta"
    diff = message_diff(left, right)

    assert diff.identical is False
    kinds = [f.kind for f in diff.fragments]
    assert "added" in kinds
    assert "removed" not in kinds
    assert "changed" not in kinds
    assert diff.added_tokens == 2
    assert diff.removed_tokens == 0


def test_message_diff_classifies_pure_removal() -> None:
    """Left has tokens the right dropped: every dropped token ``removed``."""
    left = "alpha beta gamma"
    right = "alpha"
    diff = message_diff(left, right)

    kinds = [f.kind for f in diff.fragments]
    assert "removed" in kinds
    assert "added" not in kinds
    assert diff.removed_tokens == 2
    assert diff.added_tokens == 0


def test_message_diff_coalesces_replace_into_changed() -> None:
    """Adjacent remove+add pairs collapse into a single ``changed`` fragment.

    Pinned by the Phase 5 exit criterion: *"token-level message diff renders
    add/remove/change correctly"*.
    """
    # Single-token replacement: "foo" → "bar"
    diff = message_diff("foo bar baz", "qux bar baz")

    kinds = [f.kind for f in diff.fragments]
    assert "changed" in kinds
    # No raw removed/added fragments should be emitted for the replaced token.
    assert "removed" not in kinds
    assert "added" not in kinds
    assert diff.added_tokens == 1
    assert diff.removed_tokens == 1


def test_message_diff_preserves_whitespace_between_tokens() -> None:
    """Whitespace between tokens survives in the fragment text.

    Tokenization splits words and whitespace runs into separate tokens,
    but the emitted fragment text is the verbatim concatenation of the
    matched-range tokens — so ``"".join(fragment.text) == original slice``.
    """
    diff = message_diff("a b", "a c")

    # First fragment is the common prefix "a " (word 'a' + whitespace ' ').
    assert diff.fragments[0].text == "a "
    # Replacements carry their two sides as separate structured fields.
    changed_frags = [f for f in diff.fragments if f.kind == "changed"]
    assert len(changed_frags) == 1
    assert changed_frags[0].removed == "b"
    assert changed_frags[0].added == "c"


def test_message_diff_preserves_arrows_in_changed_sides() -> None:
    """An arrow inside either replacement side is not treated as a delimiter."""
    diff = message_diff("prefix old→left suffix", "prefix new→right suffix")

    changed = [fragment for fragment in diff.fragments if fragment.kind == "changed"]
    assert len(changed) == 1
    assert changed[0].removed == "old→left"
    assert changed[0].added == "new→right"


def test_message_diff_handles_completely_disjoint_inputs() -> None:
    """No common prefix → first fragment is a remove+add (coalesced to changed)."""
    diff = message_diff("xxx", "yyy")

    kinds = [f.kind for f in diff.fragments]
    assert "changed" in kinds
    assert diff.removed_tokens == 1
    assert diff.added_tokens == 1


def test_message_diff_empty_left_is_all_addition() -> None:
    """Empty left, non-empty right → all tokens classified ``added``."""
    diff = message_diff("", "hello world")

    assert all(f.kind == "added" for f in diff.fragments)
    assert diff.added_tokens == 2


def test_message_diff_empty_right_is_all_removal() -> None:
    """Non-empty left, empty right → all tokens classified ``removed``."""
    diff = message_diff("hello world", "")

    assert all(f.kind == "removed" for f in diff.fragments)
    assert diff.removed_tokens == 2


# ----------------------------------------------------------------------
# branch_tree
# ----------------------------------------------------------------------


def _branch(
    *,
    branch_id: UUID | None = None,
    parent_branch_id: UUID | None = None,
    branch_at_index: int | None = None,
    label: str = "root",
) -> Branch:
    return Branch(
        trace_id="t" * 24 + "1",
        parent_branch_id=parent_branch_id,
        branch_at_index=branch_at_index,
        mode="frozen",
        label=label,
        branch_id=branch_id or uuid4(),
    )


def test_branch_tree_empty_input_returns_none() -> None:
    """Defensive: an empty branch list produces no tree."""
    assert branch_tree([]) is None


def test_branch_tree_single_root_has_no_children() -> None:
    """A single-branch (root-only) trace yields a leaf node."""
    root = _branch(parent_branch_id=None, branch_at_index=None, label="ingest")
    tree = branch_tree([root])

    assert tree is not None
    assert tree.branch_id == root.branch_id
    assert tree.label == "ingest"
    assert tree.children == []
    assert tree.parent_branch_id is None


def test_branch_tree_attaches_children_recursively() -> None:
    """Two-level nesting yields the expected nested structure."""
    root = _branch(label="root")
    child_a = _branch(parent_branch_id=root.branch_id, branch_at_index=2, label="A")
    child_b = _branch(parent_branch_id=root.branch_id, branch_at_index=1, label="B")
    grandchild = _branch(parent_branch_id=child_a.branch_id, branch_at_index=5, label="grand")

    tree = branch_tree([root, child_a, child_b, grandchild])

    assert tree is not None
    assert tree.branch_id == root.branch_id
    assert len(tree.children) == 2
    # Children preserve storage's input order.
    assert [c.label for c in tree.children] == ["A", "B"]
    # Grand-child recurses under child A.
    assert tree.children[0].children[0].label == "grand"
    assert tree.children[0].children[0].branch_at_index == 5


def test_branch_tree_missing_root_in_partial_list_returns_none() -> None:
    """If the supplied list contains only children (no root), no tree is built."""
    parent = uuid4()
    orphan = _branch(parent_branch_id=parent, label="orphan")
    # Parent isn't in the list — defensive: no tree.
    assert branch_tree([orphan]) is None


def test_branch_node_is_frozen() -> None:
    """BranchNode is a frozen dataclass — immutability pincushion."""
    node = BranchNode(
        branch_id=uuid4(),
        trace_id="t",
        parent_branch_id=None,
        branch_at_index=None,
        mode="frozen",
        label="x",
        created_at="2026-01-01T00:00:00Z",
        children=[],
    )
    with pytest.raises((AttributeError, Exception)):
        node.label = "mutated"  # type: ignore[misc]


def test_span_pair_is_frozen() -> None:
    """SpanPair is frozen — no mid-walk mutation from callers."""
    pair = SpanPair(
        index=0,
        left=None,
        right=None,
        status="equal",
    )
    with pytest.raises((AttributeError, Exception)):
        pair.status = "diverged"  # type: ignore[misc]
