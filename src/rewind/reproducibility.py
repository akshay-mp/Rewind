"""Phase 5.3 — reproducibility manifest capture.

Captures the environment + dependency fingerprint at run time so a trace can
be re-executed deterministically on another machine (or after an upgrade).
The manifest is persisted in the ``run_environment`` table and surfaced in
the UI's "reproducibility" panel.

The manifest includes:

* Python version + platform.
* Installed package versions (the rewind deps + any adapter extras).
* The rewind version itself.
* A content hash over the above (so two manifests can be compared quickly).
"""

from __future__ import annotations

import hashlib
import json
import platform
import sys
from dataclasses import asdict, dataclass, field
from typing import Any

__all__ = ["RunManifest", "capture_manifest"]


@dataclass(frozen=True)
class RunManifest:
    """A reproducibility fingerprint for one run environment.

    Stored as JSON in the ``run_environment`` table. The ``content_hash``
    lets the UI flag "this trace was recorded under a different environment"
    without diffing the full manifest.
    """

    rewind_version: str
    python_version: str
    platform: str
    packages: dict[str, str] = field(default_factory=dict)
    content_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-storable dict."""
        return asdict(self)


def capture_manifest(*, rewind_version: str = "") -> RunManifest:
    """Capture the current environment as a :class:`RunManifest`.

    ``rewind_version`` defaults to :data:`rewind.__version__` when empty.
    Package versions are read from :mod:`importlib.metadata` for the rewind
    install + its direct dependencies; missing packages are skipped silently.
    """
    if not rewind_version:
        # pylint: disable=import-outside-toplevel
        try:
            from rewind import __version__ as _v
        except ImportError:
            _v = "unknown"
        rewind_version = _v

    packages = _collect_package_versions()
    manifest = RunManifest(
        rewind_version=rewind_version,
        python_version=sys.version.split()[0],
        platform=platform.platform(),
        packages=packages,
    )
    return _with_content_hash(manifest)


def _collect_package_versions() -> dict[str, str]:
    """Read versions for rewind + its known dependencies from importlib.metadata."""
    # pylint: disable=import-outside-toplevel
    try:
        from importlib.metadata import PackageNotFoundError, version
    except ImportError:  # pragma: no cover - py<3.8 only
        return {}

    interesting = [
        "rewind-ai",
        "fastapi",
        "uvicorn",
        "openai",
        "pydantic",
        "sqlmodel",
        "click",
        "opentelemetry-api",
        "opentelemetry-sdk",
    ]
    out: dict[str, str] = {}
    for name in interesting:
        try:
            out[name] = version(name)
        except PackageNotFoundError:
            continue
    return out


def _with_content_hash(manifest: RunManifest) -> RunManifest:
    """Compute a stable SHA-256 over the manifest's meaningful fields."""
    payload = json.dumps(
        {
            "rewind_version": manifest.rewind_version,
            "python_version": manifest.python_version,
            "platform": manifest.platform,
            "packages": manifest.packages,
        },
        sort_keys=True,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return RunManifest(
        rewind_version=manifest.rewind_version,
        python_version=manifest.python_version,
        platform=manifest.platform,
        packages=manifest.packages,
        content_hash=digest,
    )
