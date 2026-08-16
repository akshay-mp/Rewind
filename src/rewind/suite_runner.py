"""Phase 4.2 — regression suite orchestration with SSE progress.

The :class:`SuiteRunner` runs a collection of regression cases concurrently
and streams progress events so the UI (or CLI) can render a live
pass/fail tally as each case resolves.

Design
------
* Each case runs via :func:`rewind.evaluate.run_frozen_verification` — the
  deterministic frozen-replay core. Concurrency is bounded by an
  :class:`asyncio.Semaphore`.
* Progress is published as an async iterator of :class:`SuiteProgressEvent`
  dicts, consumable by an SSE endpoint. The runner does NOT couple to
  FastAPI — it yields plain dicts the caller serialises.
* Failures don't abort the suite; every case gets a verdict.

Why a separate module (not folded into evaluate.py)?
----------------------------------------------------
``evaluate.py`` is already ~1500 lines (the eval harness). The regression
suite is a thin orchestration layer on top of the frozen-verification
primitive; keeping it separate keeps each file's responsibility single.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from rewind.evaluate import RegressionResult, run_frozen_verification

if TYPE_CHECKING:
    from rewind.evaluate import ReplaySessionFactory

__all__ = [
    "SuiteProgressEvent",
    "SuiteRunner",
    "SuiteSummary",
]

_LOGGER = logging.getLogger(__name__)
_GENERIC_ERROR_DETAIL = "error: regression case could not be executed"


#: One progress event yielded by :meth:`SuiteRunner.run`. Plain dict so the
#: SSE layer can ``json.dumps`` it directly.
SuiteProgressEvent = dict[str, Any]


class SuiteSummary(dict[str, Any]):
    """Final tally — a dict subclass for ergonomics (``summary["passed"]``)."""


class SuiteRunner:
    """Run a set of regression cases concurrently with live progress.

    Example::

        runner = SuiteRunner(store, case_ids=[...], concurrency=4)
        async for event in runner.run():
            print(event)  # {"type": "case_done", "case_id": ..., "passed": ...}
        print(runner.summary)  # {"total": 5, "passed": 4, "failed": 1}
    """

    def __init__(
        self,
        store: Any,  # noqa: ANN401
        *,
        case_ids: list[str],
        concurrency: int = 4,
        factory: ReplaySessionFactory | None = None,
    ) -> None:
        self.store = store
        self.case_ids = list(case_ids)
        self.concurrency = max(1, concurrency)
        self.factory = factory
        self.summary: SuiteSummary = SuiteSummary(
            total=len(self.case_ids),
            passed=0,
            failed=0,
            errored=0,
        )

    async def run(self) -> AsyncIterator[SuiteProgressEvent]:
        """Yield progress events as each case completes.

        Emits a ``suite_started`` event, one ``case_done`` per case (in
        completion order), and a final ``suite_finished`` with the summary.
        """
        yield {"type": "suite_started", "total": len(self.case_ids)}
        semaphore = asyncio.Semaphore(self.concurrency)

        async def _run_one(case_id: str) -> tuple[str, RegressionResult]:
            async with semaphore:
                result = await run_frozen_verification(
                    case_id, store=self.store, factory=self.factory
                )
                return case_id, result

        # Drive all cases concurrently but yield events as they land.
        pending = {
            asyncio.create_task(_run_one(cid), name=f"regression-{cid}"): cid
            for cid in self.case_ids
        }
        try:
            while pending:
                done, _ = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    case_id = pending.pop(task)
                    try:
                        cid, result = task.result()
                    except Exception:  # pylint: disable=broad-except
                        _LOGGER.exception(
                            "Regression case %s failed during suite execution",
                            case_id,
                        )
                        self.summary["errored"] += 1
                        yield {
                            "type": "case_done",
                            "case_id": case_id,
                            "passed": False,
                            "detail": _GENERIC_ERROR_DETAIL,
                        }
                        continue
                    if result.passed:
                        self.summary["passed"] += 1
                    else:
                        self.summary["failed"] += 1
                    yield {
                        "type": "case_done",
                        "case_id": cid,
                        "passed": result.passed,
                        "detail": result.detail,
                        "branch_id": str(result.branch_id)
                        if result.branch_id is not None
                        else None,
                    }
        finally:
            for task in pending:
                task.cancel()

        overall_pass = (
            self.summary["failed"] == 0 and self.summary["errored"] == 0
        )
        yield {
            "type": "suite_finished",
            "summary": dict(self.summary),
            "passed": overall_pass,
        }
