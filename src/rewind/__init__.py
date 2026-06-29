"""Rewind — time-travel debugging for AI agents.

OTel-in / replay-out: consume standard OpenTelemetry/OpenInference traces,
then rewind an agent to any span, branch it live, and diff the timelines.
"""

from __future__ import annotations

from rewind.tool_intercept import tool

__version__ = "0.1.0"
__all__ = ["__version__", "tool"]
