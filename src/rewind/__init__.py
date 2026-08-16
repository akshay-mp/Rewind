"""Rewind — time-travel debugging for AI agents.

OTel-in / replay-out: consume standard OpenTelemetry/OpenInference traces,
then rewind an agent to any span, branch it live, and diff the timelines.
"""

from __future__ import annotations

from rewind.agents import AgentDefinition, Rewind, RewindContext
from rewind.checkpoint import checkpoint
from rewind.tool_intercept import tool

__version__ = "0.1.0"
rewind = Rewind()
__all__ = [
    "AgentDefinition",
    "Rewind",
    "RewindContext",
    "__version__",
    "checkpoint",
    "rewind",
    "tool",
]


# Lazy re-export of the public Phase 5.5 eval surface. We use __getattr__
# rather than a top-level import so ``import rewind`` doesn't eagerly pull
# in asyncio / dataclasses machinery for callers only using Phase 1-4.
def __getattr__(name: str) -> object:
    """Lazy re-export for the Phase 5.5 eval harness public surface."""
    if name in {
        "evaluate",
        "EvalScenario",
        "EvalSuite",
        "EvalSuiteResult",
        "ScenarioResult",
        "EvaluatorOutcome",
        "validate_suite",
    }:
        # pylint: disable=import-outside-toplevel
        import rewind.evaluate as _eval
        # pylint: enable=import-outside-toplevel

        return getattr(_eval, name)
    raise AttributeError(f"module 'rewind' has no attribute {name!r}")
