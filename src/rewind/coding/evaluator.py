"""Deterministic trusted evaluation for coding attempts."""

from __future__ import annotations

import json
from typing import Any

from rewind.coding.domain import CheckSpec, EvaluationResult, GoalProfile, Verdict


def _check(
    check: CheckSpec, output: str, context: dict[str, Any]
) -> tuple[Verdict, dict[str, Any]]:
    """Evaluate one supported check without executing arbitrary user code."""
    kind = check.kind
    if kind == "required_text":
        ok = str(check.value) in output
        return Verdict.PASS if ok else Verdict.FAIL, {
            "check": kind,
            "value": check.value,
            "matched": ok,
        }
    if kind == "forbidden_text":
        ok = str(check.value) not in output
        return Verdict.PASS if ok else Verdict.FAIL, {
            "check": kind,
            "value": check.value,
            "matched": not ok,
        }
    if kind == "json_valid":
        try:
            json.loads(output)
        except (TypeError, ValueError) as exc:
            return Verdict.FAIL, {"check": kind, "valid": False, "error": str(exc)}
        return Verdict.PASS, {"check": kind, "valid": True}
    if kind in {"required_changed_path", "forbidden_changed_path"}:
        paths = set(context.get("changed_paths", []))
        path = str(check.path or check.value)
        present = path in paths
        ok = present if kind == "required_changed_path" else not present
        return Verdict.PASS if ok else Verdict.FAIL, {
            "check": kind,
            "path": path,
            "present": present,
        }
    if kind == "command_exit_status":
        actual = context.get("command_exit_status")
        expected = check.value if check.value is not None else 0
        if actual is None:
            return Verdict.UNKNOWN, {"check": kind, "expected": expected, "actual": None}
        ok = actual == expected
        return Verdict.PASS if ok else Verdict.FAIL, {
            "check": kind,
            "expected": expected,
            "actual": actual,
        }
    if kind in {"token_budget", "cost_budget", "duration_budget"}:
        names = {
            "token_budget": "tokens",
            "cost_budget": "cost_usd",
            "duration_budget": "duration_seconds",
        }
        actual = context.get(names[kind])
        maximum = check.maximum if check.maximum is not None else check.value
        if actual is None:
            return Verdict.UNKNOWN, {"check": kind, "maximum": maximum, "actual": None}
        ok = float(actual) <= float(maximum)
        return Verdict.PASS if ok else Verdict.FAIL, {
            "check": kind,
            "maximum": maximum,
            "actual": actual,
        }
    return Verdict.UNKNOWN, {"check": kind, "error": "unsupported check"}


def evaluate_goal(
    profile: GoalProfile,
    output: str,
    *,
    context: dict[str, Any] | None = None,
    run_id: str = "",
    attempt_id: str | None = None,
) -> EvaluationResult:
    """Evaluate a profile with precedence ERROR > FAIL > UNKNOWN > PASS."""
    facts = context or {}
    evidence: list[dict[str, Any]] = []
    verdicts: list[Verdict] = []
    try:
        for spec in profile.checks:
            verdict, item = _check(spec, output, facts)
            verdicts.append(verdict)
            evidence.append(item)
    except Exception as exc:  # defensive boundary for malformed persisted profiles
        return EvaluationResult(
            run_id=run_id,
            attempt_id=attempt_id,
            profile_id=profile.profile_id,
            verdict=Verdict.ERROR,
            evidence={"checks": evidence},
            error=str(exc),
        )
    verdict = next(
        (
            item
            for item in (Verdict.ERROR, Verdict.FAIL, Verdict.UNKNOWN, Verdict.PASS)
            if item in verdicts
        ),
        Verdict.PASS,
    )
    return EvaluationResult(
        run_id=run_id,
        attempt_id=attempt_id,
        profile_id=profile.profile_id,
        verdict=verdict,
        evidence={"checks": evidence},
        metrics={"check_count": len(verdicts)},
    )


__all__ = ["evaluate_goal"]
