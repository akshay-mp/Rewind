#!/usr/bin/env python3
"""Run vulnerability scans for a Rewind phase.

Per the user's requirement, each phase must be scanned for code vulnerabilities
with DeepSec. ``deepsec`` is not currently provisioned in this environment (it
is not on PyPI under that name), so this script:

  1. **Always** runs the static security checks that *are* available today:
     - ruff ``S`` rules (bandit-equivalent, AST-based)
     - bandit (independent AST SAST, defence-in-depth)
  2. **Delegates to DeepSec** when it is on PATH (no-op + warning otherwise),
     so the moment it is provisioned (CI secret / brew install / vendor
     download), per-phase scans start flowing automatically.

Usage:
    python scripts/security_scan.py --phase 0
    python scripts/security_scan.py --phase 1 --src src/rewind

Exit code is non-zero on any HIGH/CRITICAL finding.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable


def run(cmd: list[str], cwd: Path = REPO_ROOT) -> tuple[int, str]:
    """Run ``cmd`` and return (returncode, combined output)."""
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False, cwd=cwd)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def main() -> int:
    """Run the security stack for the requested phase. Returns process exit code."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--phase", required=True, help="Phase number being scanned.")
    ap.add_argument("--src", default="src/rewind", help="Source tree to scan.")
    ap.add_argument(
        "--out", default=".deepsec", help="Where to write the report directory."
    )
    args = ap.parse_args()

    src = (REPO_ROOT / args.src).resolve()
    out = (REPO_ROOT / args.out / f"phase{args.phase}").resolve()
    out.mkdir(parents=True, exist_ok=True)

    failures: list[str] = []
    print(f"[scan] phase={args.phase} src={src} out={out}")

    # 1. ruff S-rules (bandit-equivalent; already enforced in CI).
    rc, out_s = run([PY, "-m", "ruff", "check", "--select", "S", str(src)])
    (out / "ruff-S.txt").write_text(out_s, encoding="utf-8")
    if rc != 0:
        failures.append("ruff S rules")
    print(f"  ruff S      -> rc={rc}")

    # 2. bandit (independent AST SAST, defence-in-depth).
    rc, out_b = run([PY, "-m", "bandit", "-r", str(src), "-q"])
    (out / "bandit.txt").write_text(out_b, encoding="utf-8")
    if rc != 0:
        failures.append("bandit")
    print(f"  bandit      -> rc={rc}")

    # 3. DeepSec — invoked only if provisioned.
    deepsec = shutil.which("deepsec")
    if deepsec is not None:
        rc, out_d = run(
            [deepsec, "scan", "--src", str(src), "--out", str(out / "deepsec")]
        )
        (out / "deepsec.txt").write_text(out_d, encoding="utf-8")
        if rc != 0:
            failures.append("deepsec")
        print(f"  deepsec     -> rc={rc}")
    else:
        msg = "deepsec not on PATH; skipping (ruff S + bandit were run)."
        (out / "deepsec.txt").write_text(msg + "\n", encoding="utf-8")
        print(f"  deepsec     -> SKIPPED ({msg})")

    if failures:
        print(f"\n[FAIL] security findings from: {', '.join(failures)}")
        return 1
    print("\n[OK] no HIGH/CRITICAL findings from enabled scanners.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
