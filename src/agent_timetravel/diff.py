"""Span-level and message-level diffing for branches.

Phase 5's payoff: compare two timelines and surface the precise point of
divergence, then drill down to token-level LLM-response diffs.

This module is intentionally **pure** — no SQLite, no FastAPI, no SDK. It
takes :class:`~agent_timetravel.models.Span` lists in and produces render-friendly
:class:`SpanDiff` / :class:`MessageDiff` payloads out. Storage and API
layers compose on top.

Three diff flavours support the three debugging tasks:

1. :func:`span_diff` — side-by-side of two ordered span lists. Returns the
   first index where the lists diverge (the plan's exit criterion:
   *"Diffing two branches marks exactly which span first diverged"*).
2. :func:`message_diff` — token-level diff of two assistant responses. Splits
   on whitespace, aligns via difflib.SequenceMatcher, classifies each
   fragment as ``equal`` / ``added`` / ``removed`` / ``changed``.
3. :func:`branch_tree` — collapses a flat ``list[Branch]`` (storage's view)
   into a render-friendly tree rooted at the trace's root branch.

Algorithm choices
-----------------
* Span matching is by **index**, not by ``span_id`` — branching re-issues
  span ids under a new branch, so span_id equality is meaningless across
  branches. The spans are already ordered by ``start_time`` at the storage
  layer. Index + kind + messages_hash comparison catches divergence.
* Message diff uses Python's :mod:`difflib` rather than pulling in a
  third-party diffing library. ``SequenceMatcher`` with ``autojunk=False``
  is O(N*M) worst case but is more than fast enough for the small
  completion sizes agents typically emit (≤ a few hundred tokens).
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import UUID

from agent_timetravel.enums import SpanKind
from agent_timetravel.models import Span

if TYPE_CHECKING:
    from agent_timetravel.models import Branch


# ----------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------

#: Span kinds whose ``gen_ai.response`` carries tokens worth diffing.
_DIFFABLE_KINDS: frozenset[SpanKind] = frozenset(
    {SpanKind.LLM, SpanKind.TOOL, SpanKind.MCP}
)

#: Fragment classifications surfaced to the UI. ``(added, removed, equal)``
#: triples from ``difflib.SequenceMatcher.get_opcodes`` map onto these.
_EQUAL = "equal"
_ADDED = "added"
_REMOVED = "removed"
#: Synthesised "changed" classification for adjacent removal+addition pairs
#: (so the UI can render one strike-through+insert instead of two fragments).
_CHANGED = "changed"


# ----------------------------------------------------------------------
# Span-level diff
# ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SpanPair:
    """One row of a side-by-side span comparison.

    Exactly one of ``left`` / ``right`` is non-None at divergent indices;
    both are populated at shared prefix indices; at the common suffix (if
    any) both populate as well.
    """

    index: int
    left: Span | None
    right: Span | None
    status: str  # "equal" | "diverged" | "left_only" | "right_only"

    @property
    def is_first_divergence(self) -> bool:
        """``True`` only for the first index where the lists diverge.

        Sentinel used by the UI to scroll the user to the relevant line.
        Computed once by :func:`span_diff` and stamped on the row.
        """
        return self._is_first_divergence

    _is_first_divergence: bool = field(default=False, repr=False)


@dataclass(frozen=True, slots=True)
class SpanDiff:
    """Side-by-side comparison of two span lists.

    Attributes:
        pairs: One per index up to ``max(len(left), len(right))``.
        first_divergence_index: First index where the two lists differ, or
            ``None`` when they are byte-for-byte identical. Pinned by the
            Phase 5 exit criterion.
        left_count: Span count on the left side.
        right_count: Span count on the right side.
        quant_diverges: Phase 7 auto-flag — True when the two sides were
            replayed against different quant levels of the *same* base model
            (e.g. ``qwen3:32b-q4_K_M`` vs ``qwen3:32b-q8_0``). Surfaces the
            "did the quant cause this?" hypothesis without manual digging.
    """

    pairs: list[SpanPair]
    first_divergence_index: int | None
    left_count: int
    right_count: int
    quant_diverges: bool = False

    @property
    def identical(self) -> bool:
        """Fast path for no-divergence traces."""
        return self.first_divergence_index is None


def _spans_equal(left: Span, right: Span) -> bool:
    """Compare two spans by their *semantic* identity, not row-id.

    Spans with the same ``(kind, messages_hash, tools_hash)`` are the same
    captured observation — even if ``span_id`` differs (it will, across
    branches). For non-LLM kinds (AGENT/UNKNOWN) we fall back to the
    ``raw_attributes`` JSON hash as a coarse proxy: an agent span doesn't
    have a ``messages_hash``, so its divergence is structural.
    """
    if left.kind != right.kind:
        return False
    if left.kind in _DIFFABLE_KINDS:
        return (
            left.messages_hash == right.messages_hash
            and left.tools_hash == right.tools_hash
        )
    # AGENT / UNKNOWN — compare the deterministic payload signature.
    return str(left.raw_attributes) == str(right.raw_attributes)


def span_diff(left: list[Span], right: list[Span]) -> SpanDiff:
    """Build a side-by-side diff of two span lists.

    The walk is O(N) where N = ``max(len(left), len(right))``. The first
    divergence is flagged on exactly one :class:`SpanPair`.

    Also computes the Phase 7 ``quant_diverges`` auto-flag: True when the
    two sides replayed against different quant levels of the *same* base
    model. Surfaces the "did the quant cause this?" hypothesis in the UI
    without the operator having to grep model tags by hand.
    """
    pairs: list[SpanPair] = []
    first_divergence: int | None = None
    quant_diverges = _detect_quant_divergence(left, right)
    n = max(len(left), len(right))
    for i in range(n):
        ls = left[i] if i < len(left) else None
        rs = right[i] if i < len(right) else None
        if ls is not None and rs is not None:
            if _spans_equal(ls, rs):
                status = _EQUAL
            else:
                status = "diverged"
                if first_divergence is None:
                    first_divergence = i
        elif ls is not None:
            status = "left_only"
            if first_divergence is None:
                first_divergence = i
        else:
            status = "right_only"
            if first_divergence is None:
                first_divergence = i
        pair = SpanPair(
            index=i,
            left=ls,
            right=rs,
            status=status,
            _is_first_divergence=(i == first_divergence),
        )
        pairs.append(pair)
    return SpanDiff(
        pairs=pairs,
        first_divergence_index=first_divergence,
        left_count=len(left),
        right_count=len(right),
        quant_diverges=quant_diverges,
    )


def _detect_quant_divergence(left: list[Span], right: list[Span]) -> bool:
    """Phase 7 "did the quant cause this?" auto-flag.

    Returns ``True`` when the two sides replayed against **different quant
    levels of the same base model** (e.g. ``qwen3:32b-q4_K_M`` vs
    ``qwen3:32b-q8_0``). The comparison is per-side LLM-span aggregation:
    we collect the set of distinct ``(base, quant)`` pairs from each side's
    LLM spans and flag if every base is shared but at least one quant
    differs. Returns ``False`` when:

    * either side has no LLM spans (nothing to compare),
    * the base models are entirely different (a model swap, not a quant
      comparison),
    * neither side records a quant (no enrichment pass ran).
    """
    left_pairs = _base_quant_pairs(left)
    right_pairs = _base_quant_pairs(right)
    if not left_pairs or not right_pairs:
        return False
    left_bases = {base for base, _ in left_pairs}
    right_bases = {base for base, _ in right_pairs}
    # Different base model families → not a quant comparison.
    if not left_bases or left_bases != right_bases:
        return False
    left_quants = {quant for _, quant in left_pairs if quant is not None}
    right_quants = {quant for _, quant in right_pairs if quant is not None}
    # No quant attribute anywhere → can't flag.
    if not left_quants and not right_quants:
        return False
    return left_quants != right_quants


def _base_quant_pairs(spans: list[Span]) -> set[tuple[str, str | None]]:
    """Collapse a span list to ``{(base_model, quant)}`` pairs for LLM spans."""
    # Lazy import: enrichment imports nothing heavy at module load (only
    # ``re`` and ``shutil``), so this stays cheap. Keeping the import local
    # avoids a top-level cycle (enrichment imports models; diff imports models).
    # pylint: disable=import-outside-toplevel
    from agent_timetravel.enrichment import quant_from_span
    # pylint: enable=import-outside-toplevel

    out: set[tuple[str, str | None]] = set()
    for span in spans:
        if span.kind != SpanKind.LLM or not span.model_name:
            continue
        quant = quant_from_span(span).label
        base = _strip_quant_suffix(span.model_name, quant)
        out.add((base, quant))
    return out


def _strip_quant_suffix(model_name: str, quant: str | None) -> str:
    """Remove a detected quant tag from ``model_name`` to get the base name."""
    if quant is None or not quant:
        return model_name.lower()
    # Quant tags appear after one of -, _, : in the model name.
    for sep in ("-", "_", ":"):
        suffix = sep + quant
        if model_name.lower().endswith(suffix):
            return model_name[: -len(suffix)].lower()
    return model_name.lower()


# ----------------------------------------------------------------------
# Message / token diff
# ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MessageFragment:
    """One segment of a token-aligned message diff.

    ``text`` is verbatim (preserving the whitespace between tokens). The UI
    wraps ``added`` / ``removed`` fragments in ``<ins>`` / ``<del>`` tags
    or renders them as row-by-row strikes and inserts. Replacements use the
    structured ``removed`` / ``added`` fields so either side can contain any
    character, including the arrow used in the UI legend.
    """

    text: str
    kind: str  # "equal" | "added" | "removed" | "changed"
    removed: str | None = None
    added: str | None = None


@dataclass(frozen=True, slots=True)
class MessageDiff:
    """Token-level diff of two assistant messages.

    ``fragments`` is a flat list — the UI decides line wrapping / rendering.
    Classifying adjacent removal+addition as a single ``changed`` row is
    done in :func:`_coalesce_changed` so the wire shape matches what the
    UI will render (otherwise the caller would have to re-walk the list).
    """

    left: str
    right: str
    fragments: list[MessageFragment]
    added_tokens: int
    removed_tokens: int

    @property
    def identical(self) -> bool:
        """``True`` when the two messages were byte-identical."""
        return self.left == self.right


def _tokenise(text: str) -> list[str]:
    """Split ``text`` into word and whitespace tokens for diffing.

    Strategy: each maximal run of non-whitespace *or* whitespace becomes
    its own token. This makes the same word produce identical tokens
    regardless of where it appears in the string ("beta" mid-sentence
    matches "beta" at the end), which keeps diffs noise-free.
    ``"".join(tokens) == text`` still holds for round-tripping.
    """
    if not text:
        return []
    tokens: list[str] = []
    buf = ""
    buf_is_space = None  # type: bool | None
    for ch in text:
        ch_is_space = ch.isspace()
        if buf_is_space is None:
            buf = ch
            buf_is_space = ch_is_space
            continue
        if ch_is_space == buf_is_space:
            buf += ch
        else:
            tokens.append(buf)
            buf = ch
            buf_is_space = ch_is_space
    if buf:
        tokens.append(buf)
    return tokens


def _is_word_token(token: str) -> bool:
    """``True`` if ``token`` carries content (i.e. is not pure whitespace).

    Used for the ``added_tokens`` / ``removed_tokens`` counters — those
    reflect *content* deltas, since users reason in words, not whitespace.
    """
    return not token.isspace()


def _coalesce_changed(
    raw: list[tuple[str, str]],
) -> list[MessageFragment]:
    """Collapse adjacent (remove, add) opcode pairs into ``changed`` rows.

    ``SequenceMatcher`` emits sequences like
    ``[("delete", "foo"), ("insert", "bar")]`` for every replacement.
    Rendering two fragments per substitution bloats the UI; collapsing
    them produces a cleaner strike-through-plus-insert.
    """
    fragments: list[MessageFragment] = []
    i = 0
    while i < len(raw):
        op, text = raw[i]
        if op == _EQUAL:
            fragments.append(MessageFragment(text=text, kind=_EQUAL))
            i += 1
            continue
        if op == _REMOVED and i + 1 < len(raw) and raw[i + 1][0] == _ADDED:
            # Pair them: render as one "changed" fragment carrying both texts.
            fragments.append(
                MessageFragment(
                    text="",
                    kind=_CHANGED,
                    removed=text,
                    added=raw[i + 1][1],
                )
            )
            i += 2
            continue
        if op == _REMOVED:
            fragments.append(MessageFragment(text=text, kind=_REMOVED))
        elif op == _ADDED:
            fragments.append(MessageFragment(text=text, kind=_ADDED))
        i += 1
    return fragments


def message_diff(left: str, right: str) -> MessageDiff:
    """Compute a token-aligned diff of two assistant message strings.

    Returns a :class:`MessageDiff` with fragments classified as
    ``equal`` / ``added`` / ``removed`` / ``changed``. The Phase 5 exit
    criterion *"token-level message diff renders add/remove/change
    correctly"* is pinned here.

    Identical inputs short-circuit to a single ``equal`` fragment for
    cheap no-op reads on common-prefix spans.
    """
    if left == right:
        return MessageDiff(
            left=left,
            right=right,
            fragments=[MessageFragment(text=left, kind=_EQUAL)] if left else [],
            added_tokens=0,
            removed_tokens=0,
        )
    left_tokens = _tokenise(left)
    right_tokens = _tokenise(right)
    matcher = difflib.SequenceMatcher(
        a=left_tokens, b=right_tokens, autojunk=False
    )
    raw: list[tuple[str, str]] = []
    added = 0
    removed = 0
    for op, l1, l2, r1, r2 in matcher.get_opcodes():
        # difflib opcode strings — pinned here so this file is self-documenting.
        if op == "equal":
            raw.append((_EQUAL, "".join(left_tokens[l1:l2])))
            continue
        if op in ("delete", "replace"):
            txt = "".join(left_tokens[l1:l2])
            if txt:
                raw.append((_REMOVED, txt))
                removed += sum(
                    1 for tok in left_tokens[l1:l2] if _is_word_token(tok)
                )
        if op in ("insert", "replace"):
            txt = "".join(right_tokens[r1:r2])
            if txt:
                raw.append((_ADDED, txt))
                added += sum(
                    1 for tok in right_tokens[r1:r2] if _is_word_token(tok)
                )
    fragments = _coalesce_changed(raw)
    return MessageDiff(
        left=left,
        right=right,
        fragments=fragments,
        added_tokens=added,
        removed_tokens=removed,
    )


# ----------------------------------------------------------------------
# Branch tree
# ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BranchNode:
    """One node in a trace's branch tree.

    ``parent_branch_id`` is ``None`` for the root. ``children`` carries
    the recursive sub-branches; the UI renders it as a collapsible tree
    (Phase 5 exit criterion *"branch tree view"*).
    """

    # pylint: disable=too-many-instance-attributes, duplicate-code
    # The seven fixed fields mirror ``agent_timetravel.models.Branch`` verbatim;
    # ``children`` is structurally required for the tree shape. Collapsing
    # any field into a sub-struct would obscure the 1:1 mapping with the
    # storage row and the wire shape. The duplicate-code detector flags
    # the field list against ``agent_timetravel.timeline.BranchNodeView`` — the
    # duplication is the cost of a clean layer split (pure dataclass for
    # the diff engine vs Pydantic BaseModel for the HTTP wire shape).
    branch_id: UUID
    trace_id: str
    parent_branch_id: UUID | None
    branch_at_index: int | None
    mode: str
    label: str
    created_at: str
    children: list[BranchNode]


def branch_tree(branches: list[Branch]) -> BranchNode | None:
    """Collapse a flat ``list[Branch]`` into a render-friendly tree.

    Root = the branch with ``parent_branch_id is None``. Children are
    attached recursively in insertion order (storage's ``created_at`` sort
    is preserved by the caller — :func:`TraceStore.list_branches` already
    returns root-first, then chronological).

    Returns ``None`` if the list is empty (defensive — every trace has at
    least a root branch, but callers may pass partial data).
    """
    if not branches:
        return None
    by_parent: dict[UUID | None, list[Branch]] = {}
    for branch in branches:
        by_parent.setdefault(
            branch.parent_branch_id, []
        ).append(branch)
    root_branches = by_parent.get(None, [])
    if not root_branches:
        # No root in the supplied list — can't assemble a tree.
        return None
    root_branch = root_branches[0]

    def _build(branch: Branch) -> BranchNode:
        children = [
            _build(child)
            for child in by_parent.get(branch.branch_id, [])
        ]
        return BranchNode(
            branch_id=branch.branch_id,
            trace_id=branch.trace_id,
            parent_branch_id=branch.parent_branch_id,
            branch_at_index=branch.branch_at_index,
            mode=branch.mode,
            label=branch.label,
            created_at=branch.created_at,
            children=children,
        )

    return _build(root_branch)


__all__ = [
    "BranchNode",
    "MessageDiff",
    "MessageFragment",
    "SpanDiff",
    "SpanPair",
    "branch_tree",
    "message_diff",
    "span_diff",
]
