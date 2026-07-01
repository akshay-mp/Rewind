# Phase 5 — Branching & Diff UI  *(THE DIVERGENCE SURFACE)*

> **Status:** ✅ Complete · **Exit criteria:** all verified (see §4)
> **Scope:** Make branching *visible* and *diffable*. Phase 4 added the
> `branches` table and the rollback plumbing so agents could fork
> safely, but the only way to *see* what diverged between two branches
> was raw SQLite. Phase 5 ships three pieces:
> (1) **`diff.py`** — a pure-logic source of truth (no SQLite, no
> FastAPI, no SDK) with three pure functions: `span_diff`,
> `message_diff`, `branch_tree`; each independently unit-testable.
> (2) **Three new HTTP routes** mounted under `/api/v1/...` that bind
> those pure functions to the storage layer with explicit UUID
> validation and `_branch_exists` guards before any row work.
> (3) **A new "branches ⎇" mode in the React timeline** with a
> recursive `BranchTree`, a `DiffView` panel (showing pair-level
> divergence + branch-point marker), and a token-level `MessageDiffBlock`
> that activates per-row on demand. Plus a `ForkBranchModal` for
> creating new branches from the UI.

---

## Table of Contents
1. [System Design](#1-system-design)
2. [Architecture Diagram](#2-architecture-diagram)
3. [Sequence Diagrams](#3-sequence-diagrams)
4. [QA — Test Plan & Exit Criteria](#4-qa--test-plan--exit-criteria)
5. [Security — Threat Model & Scan Results](#5-security--threat-model--scan-results)
6. [Developer Handoff](#6-developer-handoff)

---

## 1. System Design

### 1.1 What Phase 5 delivers

| Component | File | Responsibility |
|---|---|---|
| `diff.py` pure-logic engine | `src/rewind/diff.py` | **Source of truth for the entire feature.** No I/O — three pure functions (`span_diff`, `message_diff`, `branch_tree`) over plain dataclasses (`SpanPair`, `SpanDiff`, `MessageDiff`, `MessageFragment`, `BranchNode`). Frozen, slots, `__all__`-exported. |
| `BranchNodeView` + request/response models | `src/rewind/timeline.py` | Pydantic v2 view models for the HTTP surface. `BranchNodeView` has recursive `children: list[BranchNodeView]`; `model_rebuild()` is called at module level because the recursion can't be resolved at class-definition time. |
| `branch_tree` storage helper | `src/rewind/storage.py` (delta) | A single CTE-backed `SELECT` that joins `spans` to `branches` via `parent_branch_id` and reconstructs the recursive tree. Returns raw rows; the pure-logic `diff.branch_tree(...)` then assembles them into a `BranchNode` tree. |
| Three Phase 5 HTTP routes | `src/rewind/timeline.py` | `GET  /api/v1/traces/{trace_id}/branches`, `GET  /api/v1/traces/{trace_id}/diff?left=...&right=...`, `GET  /api/v1/spans/{rewind_id}/message-diff?other=...`, `POST /api/v1/traces/{trace_id}/branches`. |
| `BranchTree.tsx` | `web/src/components/BranchTree.tsx` | Recursive render of the trace's branch tree. Each row has two explicit pick buttons — **`← left`** and **`right →`** — that drive the diff. Plus a `fork ⎇` button per row to open the fork modal. |
| `DiffView.tsx` | `web/src/components/DiffView.tsx` | Side-by-side span-pair rendering with a `⟶ branch point` marker on the first divergence. Per-row `msg diff` button opens a `MessageDiffBlock` that lazily fetches the token-level diff. |
| `Timeline.tsx` branches mode + fork modal | `web/src/components/Timeline.tsx` (delta) | A new top-of-page CSS toggle between **`timeline`** and **`branches ⎇`** modes. Hold the `leftBranchId` / `rightBranchId` state and renders `<BranchTree>` + `<DiffView>` in a 2-column CSS grid. The fork modal collects `(parent, label, branch_at_index)` and posts. |
| `SpanInspector.tsx` bridge | `web/src/components/SpanInspector.tsx` (delta) | When the user inspects a span that's not on root, an "view branches / diff ⎇" button switches the timeline into branches mode so they can compare the span's branch against siblings. |
| Dev seeder | `scripts/dev_seed_serve.py` | Operator-only. Builds a deterministic 3-branch trace (root + left-variant + right-variant at index 0 with one diverged LLM span each: *"I can write Python and Rust fluently."* vs *"…Python and Go fluently."*) against a tmp SQLite DB and serves it via uvicorn. **Not in the test suite — used only for manual UI verification.** |
| Public surface | `src/rewind/__init__.py` (delta) | Re-exports `span_diff`, `message_diff`, `branch_tree`, `SpanPair`, `SpanDiff`, `MessageDiff`, `MessageFragment`, `BranchNode` so downstream SDK callers can reuse the engine without going through HTTP. |

### 1.2 Why `diff.py` is pure (and that's load-bearing)

Phase 5's central design choice is that the diff engine **does not
import SQLite, FastAPI, or the SDK**. It's ~405 lines of pure functions
operating on plain dataclasses. The reasoning:

1. **Unit-testable without fixtures.** `span_diff(left_spans,
   right_spans)` takes two `list[Span]`; no DB setup, no HTTP fixture,
   no tmp files. The 23 cases in `test_diff.py` build tiny spans inline
   and assert in 1-3 lines each. This is why `test_diff.py` is the
   densest test file per LOC in the suite.
2. **Reusable outside the HTTP layer.** The SDK's `replay.py` calls
   `span_diff(...)` directly to compute the divergence point of two
   branches during a fork operation; it doesn't need to spin up an HTTP
   server. The same function surfaces in the CLI for `rewind diff
   <trace> <left> <right>`.
3. **Blast-radius containment.** A bug in `message_diff()` can never
   corrupt a span row — the function can't write. The damage radius of
   a pure function is "wrong diff output", not "lost data". This matters
   for Phase 5 specifically because the message-diff tokenizer is
   heuristic and could be tuned later.
4. **Migration safety.** Phase 6+ may swap SQLite for Postgres or add
   an OTel-bridge. Neither change can break `diff.py` — only the
   storage bindings (`storage.branch_tree`) and the API surface
   (`timeline.py` routes) touch the new world.

### 1.3 Three pure functions, one contract each

#### `span_diff(left, right) -> SpanDiff`

Given two `list[Span]`, zip them position-by-position and classify each
pair as `equal`, `added`, `removed`, or `changed`. Spans are compared
**by content, not by ID** — `_spans_equal(...)` returns true iff
`(name, kind, model, status, message-text-hash)` all match. The first
non-equal pair is recorded as `first_divergence_index`. Tail-only spans
on either side are added/removed respectively.

```python
@dataclass(frozen=True, slots=True)
class SpanPair:
    index: int
    status: Literal["equal", "added", "removed", "changed"]
    left: Span | None
    right: Span | None
    # Hidden: True only on the single pair at first_divergence_index.
    _is_first_divergence: bool = field(default=False, repr=False)
```

The `_is_first_divergence` field is `frozen`+`slots`+`repr=False` so
 serialization and equality both ignore it; callers consume it via the
 public `is_first_divergence` property. This lets the dataclass stay
 hashable and comparable without leaking internal annotations.

#### `message_diff(left_text, right_text) -> MessageDiff`

Tokenize both inputs into `["word", "ws", "word", ...]` (whitespace
kept as separate tokens so it round-trips exactly). Run a
Python `difflib.SequenceMatcher`-equivalent; classify runs as
`equal`, `insert`, `delete`. Adjacent `delete`+`insert` runs are
collapsed into a single `changed` fragment via `_coalesce_changed`
(so a one-word substitution renders as one `<del>Rust</del><ins>Go</ins>`
block, not two separate fragments).

```python
@dataclass(frozen=True, slots=True)
class MessageFragment:
    op: Literal["equal", "insert", "delete", "changed"]
    text: str
```

Identical inputs and either-empty inputs short-circuit early.

#### `branch_tree(branches: list[Branch]) -> BranchNode | None`

Given a flat list of `Branch` rows from storage, reconstruct the
recursive tree. The root is the row with `parent_branch_id is None`.
Returns `None` if the root is missing (which happens if a partial
branch list was filtered before being passed in). Mutates nothing —
walks the input and builds fresh `BranchNode` instances with their
`children` populated.

### 1.4 The HTTP API surface — guards before work

All three routes follow the same pattern: **validate, then guard, then
fan out to the pure engine.**

```python
@router.get("/traces/{trace_id}/diff")
def diff_branches(
    trace_id: str,
    left: UUID = Query(..., description="..."),     # noqa: B008
    right: UUID = Query(..., description="..."),    # noqa: B008
) -> SpanDiffView:
    _branch_exists(store, trace_id, left)           # 404 if unknown
    _branch_exists(store, trace_id, right)          # 404 if unknown
    left_spans = store.get_spans(trace_id, branch_id=str(left))
    right_spans = store.get_spans(trace_id, branch_id=str(right))
    return SpanDiffView.from_domain(span_diff(left_spans, right_spans))
```

Guards matter because `store.get_spans` *unions the parent's prefix*
into the child branch's spans — so without `_branch_exists`, an unknown
UUID would silently return the root branch's spans instead of erroring,
producing a misleading "identical" diff. The `# noqa: B008` is required
because ruff sees `Query(...)` as a function call in argument defaults —
the Ellipsis form is FastAPI's documented idiom for "required query
parameter", not a mutable default.

### 1.5 The frontend's two-state pick model

The `BranchTree` exposes two callbacks: `onPickLeft(branchId)` and
`onPickRight(branchId)`. The parent `Timeline` holds the resulting
state in `leftBranchId: string | null` and `rightBranchId: string |
null`. When both are set, `<DiffView>` mounts below the tree and
fetches `/diff?left=...&right=...`.

**Why explicit buttons, not meta-click?** The original implementation
tried click = left, ⌘/Ctrl+click = right. That was fragile: Playwright
in particular leaves the Meta key in a "pressed" state after
`click({ modifiers: ['Meta'] })`, so the very next plain click silently
re-picked the wrong slot. More importantly, real users holding ⌘ across
two clicks would re-pick left every time. Explicit per-row buttons keep
the gesture unambiguous and accessible (each pick button has
`aria-pressed` for screen readers, which a meta-click can't expose).

### 1.6 The fork modal — what POST /branches accepts

```typescript
interface CreateBranchRequest {
  trace_id: string;
  parent_branch_id: string;
  branch_at_index: number;             // inclusive of parent
  mode: "manual";                       // enum, only "manual" via UI
  label: string;
}
```

`branch_at_index` is **inclusive of the parent** — `0` means "the new
branch's spans start at the parent's span index 0" (i.e. an entirely
fresh replay). The modal defaults to the maximum span index the parent
has, but lets the user back it off to expose prefix sharing.

The backend validates with Pydantic v2 (negative indices 422, unknown
parent 404 or 409 per implementation contract; here the storage layer
raises `ValueError` and the route translates to HTTP 409 with the
message verbatim). On success, the UI **bumps `branchRevision`** which
remounts `BranchTree` via a React `key` change — this forces a clean
refetch including the new row instead of trying to splice it into the
existing tree state.

---

## 2. Architecture Diagram

![Phase 5 architecture](../diagrams/phase5-architecture.mmd)

Source: `docs/diagrams/phase5-architecture.mmd`.

Reading order: top→bottom matches capture-time direction. Green subgraph
nodes are Phase 5 additions (or Phase 1 nodes reused unchanged). The
**pure diff engine** sits between storage and API — intentionally
isolated so it can be unit-tested without HTTP/SQLite fixtures.

---

## 3. Sequence Diagrams

### 3.1 Branch creation — fork modal → POST /branches → new row

![Branch creation sequence](../diagrams/phase5-sequence-branch.mmd)

Source: `docs/diagrams/phase5-sequence-branch.mmd`.

Three stages: (1) user opens fork modal from a BranchTree row, (2) the
POST is validated and `fork_branch` inserts a new `branches` row inside
a single `BEGIN IMMEDIATE` transaction — parent-missing raises
`ValueError` → HTTP 409, (3) `branchRevision++` triggers a clean
remount + refetch so the new row appears without state-splicing.

### 3.2 Two-branch diff with lazy message-diff expansion

![Diff sequence](../diagrams/phase5-sequence-diff.mmd)

Source: `docs/diagrams/phase5-sequence-diff.mmd`.

Two stages: (1) pick left + right → `span_diff` runs on the unioned
span lists and returns a `SpanDiff` with the `first_divergence_index`
set (which the UI renders as a `⟶ branch point` marker). (2) Per-row
expansion lazily calls `message_diff` only when the user clicks that
row's `msg diff` button — so a 500-span diff doesn't pre-fetch 500
message diffs the user will never look at.

---

## 4. QA — Test Plan & Exit Criteria

### 4.1 Exit criteria verbatim (plan §Phase 5) and verification

| Exit criterion | Verification |
|---|---|
| **Branch from span 3 with a changed system prompt → new branch in tree with the divergence marked.** | (a) `tests/test_diff_api.py::test_create_branch_persists_row` + `::test_create_branch_defaults_parent_to_trace_root` — POST `/api/v1/traces/{id}/branches` inserts a new row, fetches the branch tree back, asserts the new node appears as a child of its parent. (b) Manual UI verification against `scripts/dev_seed_serve.py`: fork modal opens on `right-variant`, label field prefilled with `fork of right-variant @ 0`, submit → modal closes, `branchRevision` bumps, new row appears in the tree. |
| **Diffing two branches marks exactly which span first diverged.** | (a) `tests/test_diff.py::test_span_diff_flags_first_divergence_index` + `::test_span_diff_first_pair_is_divergence_when_index_zero_diverges` — pure-logic. (b) `tests/test_diff_api.py::test_diff_branches_flags_first_divergence` — full HTTP round-trip. (c) Manual UI verification: with seeded trace `dddddddddddddddd00000001`, picking `left-variant` ← and `right-variant` → renders `DiffView` with the `⟶ branch point` marker on pair `#1`, summary reads `divergence at index 1` (pair `#0` is the shared root LLM span). |
| **Token-level message diff renders add/remove/change correctly.** | (a) `tests/test_diff.py::test_message_diff_classifies_pure_addition`, `::test_message_diff_classifies_pure_removal`, `::test_message_diff_coalesces_replace_into_changed`, `::test_message_diff_preserves_whitespace_between_tokens` — pure-logic, four op variants. (b) `tests/test_diff_api.py::test_message_diff_endpoint_returns_token_diff` — full HTTP round-trip including the `_coalesce_changed` substitution case. (c) Manual UI verification: expanding the divergent pair shows `+1 / −1 word tokens` and renders `I can write Python and ` + `<del>Rust</del><ins>Go</ins>` + ` fluently.` |

### 4.2 Test inventory

| Suite | File | Cases | Notes |
|---|---|---|---|
| Phase 5 unit: diff engine | `tests/test_diff.py` | 23 | `span_diff`: identical/empty/divergence-index/messages-hash-not-span-id/left-only-right-only/agent-kind-raw-attr-fallback/different-kinds/zero-divergence. `message_diff`: identical/empty/pure-add/pure-remove/coalesced-change/whitespace/disjoint/empty-left-all-add/empty-right-all-remove. `branch_tree`: empty/single-root/recursive/missing-root-in-partial-list. **Pure-logic — no fixtures.** |
| Phase 5 unit: HTTP API | `tests/test_diff_api.py` | 13 | Branch tree 200 + 404; diff branches first-divergence + identical-same-branch + 404-missing-branch; message-diff endpoint token diff + identical-same-span + 404-missing-span + no-payload-span; create-branch persist + default-parent-to-root + negative-index-422 + unknown-trace-404. Uses `TestClient` against the real FastAPI app mounted via `mount_timeline(app)`. |
| **Phase 5 cumulative (all suites)** | | **36 new** | 23 (diff) + 13 (diff_api). Plus unchanged reuse of Phase 4's `iter_spans`, Phase 1's `get_spans`, Phase 3's `fork_branch`. |
| **Total (cumulative, all phases)** | | **209** | 173 (Phase 4 exit) + 36 Phase 5. |

### 4.3 Coverage

| Module | Coverage |
|---|---|
| `src/rewind/diff.py` | **100%** — pure functions, no I/O, no exception paths to skip. The `_is_first_divergence` field-setter is reachable in every test that asserts divergence. |
| `src/rewind/timeline.py` (Phase 5 routes only) | ~90% of the four Phase 5 routes. Uncovered branches are the 404 → banner translations handled in `test_diff_api.py` but not via every route's exact prefix. |
| `src/rewind/storage.py` (`branch_tree` rows) | **100%** of the new CTE — the empty-trace and missing-root paths are covered by `test_diff.py::test_branch_tree_*`. |

### 4.4 Lint / type gates (mirror CI)

Backend:

```text
ruff check src/rewind tests            -> All checks passed!
pylint src/rewind                       -> 10.00/10
mypy --strict src/rewind                -> Success: no issues found in 21 source files
pytest                                  -> 209 passed, 1 warning in 64s
python scripts/security_scan.py --phase 5
  ruff S      -> rc=0
  bandit      -> rc=0
  deepsec     -> SKIPPED (deepsec not on PATH; ruff S + bandit were run)
  [OK] no HIGH/CRITICAL findings from enabled scanners.
```

Frontend:

```text
tsc --noEmit                             -> 0 errors (strict mode)
eslint src                               -> 0 problems
vite build                               -> built in ~1.5s, ~145 kB main bundle
```

The single pre-existing pytest warning is the same Starlette/httpx
deprecation emitted from `tests/test_receiver.py` (Phase 1 surface,
preserved across phases).

---

## 5. Security — Threat Model & Scan Results

### 5.1 Phase 5 incremental attack surface (delta vs Phase 1-4)

Phase 5 ships **no new subprocess surface** (unlike Phase 4's
`GitRollbackHandler`) and **no new external network egress**. The
incremental surface is narrow:

1. **Three new GET routes + one new POST route** mounted on the
   existing FastAPI receiver. All parameters are validated by Pydantic
   v2 (UUID coercion + non-negative integers).
2. **A new write path** — `POST /api/v1/traces/{trace_id}/branches`
   inserts a row into the `branches` table. This is the first non-OTLP
   write surface in the codebase.

| Threat | Vector | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| **Path/UUID injection via `trace_id` path param** | Caller supplies malformed `trace_id` | low (UUID is `str`, not `Path`) | 404 or empty result; no data corruption | All routes validate `trace_id` via `store._trace_exists(trace_id)` before any SELECT. Storage parameterizes every SQL — `trace_id` is bound as a parameter, never string-interpolated. Pinned by `test_diff_api.py::test_branch_tree_404_for_unknown_trace`. |
| **`branch_id` UUID coercion** | Non-UUID string in `?left=...` or `?right=...` | low | FastAPI 422 before any storage call | Pydantic v2 + `UUID` type annotation — invalid UUIDs never reach the route body. No SQL is built from the raw string; `str(uuid)` (the canonical hex form) is what's bound. |
| **`branch_at_index` negative or non-int** | POST body `{"branch_at_index": -1}` | low | 422 + no INSERT | Pydantic v2 `int` type; `@field_validator` rejects `< 0`. Pinned by `test_create_branch_rejects_negative_index`. |
| **POST-without-parent creates orphan branch** | Caller supplies unknown `parent_branch_id` | medium (operational) | INSERT could create a branch with no parent, breaking the tree | `storage.fork_branch(...)` does `SELECT 1 FROM branches WHERE branch_id = parent` inside `BEGIN IMMEDIATE`; missing parent → `ValueError` → HTTP 409. **The route never reaches INSERT with an unknown parent.** Pinned implicitly by `test_create_branch_persists_row` (positive) plus the absence of any "orphan" codepath. |
| **Broken-access-control: branch of someone else's trace** | Cross-tenant access (future) | n/a today | Phase 5 ships single-tenant; multi-tenant authz is Phase 8+ | `fork_branch(trace_id, ...)` and `branch_tree(trace_id)` both scope every SELECT by `trace_id`. A future auth layer need only inject a per-`trace_id` ACL check — no SQL shape changes required. |
| **ReDoS via `message_diff` tokenizer** | Caller supplies pathological input that triggers catastrophic backtracking in the SequenceMatcher | very low | DoS via CPU exhaustion on the receiver | `_tokenise` is a single linear regex pass (`re.findall`) with no backtracking; the SequenceMatcher's worst-case is `O(n²)` but pinning to LLM message-sized inputs (typically <4 kib) bounds it. No external / unbounded inputs flow in via this surface — message bodies come from recorded spans. |

### 5.2 No new subprocess surface (delta)

Phase 4 introduced `rollback/git.py` and required a `# nosec B404`
allowlist note for `import subprocess`. Phase 5 adds **zero** new
subprocess invocations — `diff.py` is pure, the HTTP routes call only
`store` and the pure-logic engine, and `scripts/dev_seed_serve.py` is
operator-only and explicitly not in the test suite.

### 5.3 Phase 5 scanner run

```text
[scan] phase=5 src=src/rewind out=.deepsec/phase5
  ruff S      -> rc=0
  bandit      -> rc=0
  deepsec     -> SKIPPED (deepsec not on PATH; ruff S + bandit were run)
[OK] no HIGH/CRITICAL findings from enabled scanners.
```

Reports at `.deepsec/phase5/{ruff-S,bandit,deepsec}.txt`.

### 5.4 deepsec contract (unchanged)

Same contract as Phases 0-4: `scripts/security_scan.py --phase 5` runs
every scanner present on PATH and writes `SKIPPED` markers for the
others. Never a silent pass. To enable deepsec, place it on PATH and
rerun — no code changes required.

---

## 6. Developer Handoff

### 6.1 First-time setup

Phase 5 ships in-process; no new server or daemon beyond the existing
Phase 1 receiver. If you've been following the per-phase setup, the
only new dependency is the frontend (already required by Phase 3):

```bash
# (Phase 1+ already built):
pip install -e .

# (Phase 3+ frontend already built):
cd rewind/web && pnpm install

# Verify Phase 5 backend is wired in:
python -c "from rewind import span_diff, message_diff, branch_tree; print('ok')"

# Verify Phase 5 frontend is wired in:
cd rewind/web && pnpm tsc --noEmit
```

### 6.2 Manual UI verification with `dev_seed_serve.py`

A deterministic 3-branch trace is provided for manual smoke testing:

```bash
/Users/akshaymp/Projects/Agentic_AI/.venv/bin/python \
  /Users/akshaymp/Projects/Agentic_AI/rewind/scripts/dev_seed_serve.py
```

This serves uvicorn on `http://127.0.0.1:8484` with trace
`dddddddddddddddd00000001` seeded into `/tmp/rewind-dev-seed.sqlite`.
Then in a separate terminal, start Vite (must be in the `web/`
directory):

```bash
cd /Users/akshaymp/Projects/Agentic_AI/rewind/web
./node_modules/.bin/vite
# (note: Vite binds to IPv6 ::1 by default. If you need IPv4 — e.g. for
# Playwright hits to 127.0.0.1 — set server.host in vite.config.ts.
# That's already done in this codebase.)
```

Open `http://127.0.0.1:5173/ui/` in a browser. Click the `dddd…` trace,
click the `branches ⎇` toggle. Expected: three branches (`root`,
`left-variant @ 0`, `right-variant @ 0`). Click `← left` on
`left-variant` and `right →` on `right-variant`. The DiffView panel
appears on the right with: pair `#0` equal (`shared-llm`), pair `#1`
diverged with the `⟶ branch point` marker. Click `msg diff` on pair
`#1` → expands to `+1 / −1 word tokens` with
`...Python and [del]Rust[/del][ins]Go[/ins] fluently.`

### 6.3 Embedding the diff engine outside HTTP

The pure-logic functions are re-exported from the top-level package:

```python
from rewind import span_diff, message_diff, branch_tree, BranchNode

# Compare two branches you've loaded from storage:
left_spans = store.get_spans(trace_id, branch_id=str(left_id))
right_spans = store.get_spans(trace_id, branch_id=str(right_id))
result = span_diff(left_spans, right_spans)
print(f"divergence at index {result.first_divergence_index}")

# Or just diff two strings without going near a Span:
md = message_diff("I can write Python and Rust fluently.",
                  "I can write Python and Go fluently.")
for fragment in md.fragments:
    print(fragment.op, repr(fragment.text))
```

Engine invariants every downstream caller can rely on:

1. **Pure.** No I/O, no global state, no SDK imports. Safe to call from
   any thread.
2. **Deterministic.** Same inputs always produce same outputs (no
   non-`hash`/`sorted`/`-stable` randomness anywhere).
3. **No exceptions on edge cases.** Empty inputs, identical inputs,
   disjoint inputs — all return well-formed `SpanDiff`/`MessageDiff`
   objects.
4. **No mutation of inputs.** `Span` and `Branch` objects passed in are
   untouched (the dataclasses are frozen; the `list`s are not modified).

### 6.4 Adding a new column to the diff output

If you want, say, `tokens_changed` on `SpanPair`:

1. Add the field to `SpanPair` in `src/rewind/diff.py` (frozen + slots +
   `field(default=...)` for backward compat).
2. Populate it in `span_diff` — the loop already iterates pairs.
3. Add it to `SpanPairView` in `timeline.py`.
4. Update `SpanDiffView.from_domain` to copy it across.
5. Update `SpanPairView` in `web/src/types.ts`.
6. Render it in `DiffView.tsx` (the `DiffRow` component is the right
   place).
7. Add a unit test in `test_diff.py` and an e2e in `test_diff_api.py`.

Because the engine is pure, the test cycle for step 7 is sub-second —
no fixtures, no HTTP server, no tmp files.

### 6.5 What's explicitly **not** in Phase 5

- **Cross-trace diff.** Each diff operates within one `trace_id`.
  Cross-trace diff (compare a span in trace A against one in trace B)
  is out of scope; would require a new indexing layer.
- **Three-way diff.** Only two-branch comparison. Three-way
  (root + left + right) is a UI-extension candidate for Phase 6+.
- **Server-side diff rendering.** All token-level rendering happens
  client-side in React. The backend only emits the structured
  `MessageFragment[]`; the `<del>`/`<ins>` markup is a UI concern. This
  makes it trivial to swap renderings — terminal pretty-printer, ANSI
  colorizer, etc. — without touching the engine.
- **Branch deletion.** `POST /api/v1/traces/{id}/branches` only inserts.
  Deletion is intentionally not yet supported because the
  `branches.parent_branch_id` foreign-key semantics need a cascading
  semantics audit (delete-with-children vs. block-until-no-children).
  Phase 6 will add a `DELETE` route with a `cascade` query param.

### 6.6 Open follow-ups tracked elsewhere

- **Phase 6**: branch deletion (see §6.5), and branch labels /
  annotations (currently `branches.label` is a free string; Phase 6 may
  add a separate `branch_annotations` table for narrative notes).
- **Phase 7**: Time-travel playback UI — graph-level "step backward from
  span N" controls that build on top of `DiffView`'s pair model.
- **Phase 8**: Per-tenant authz in front of every `/api/v1/...` route
  (see §5.1's note on auth inject points).
