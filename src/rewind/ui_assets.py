"""Static UI artifact location + bundling helpers.

Phase 2 ships a React/Vite/TypeScript app under ``web/``. The built
artifact lands in ``web/dist/`` and is served by FastAPI at ``/ui``. This
module isolates the "where does the built UI live?" question so the rest of
the codebase never hardcodes a path.

Build workflow
--------------
::

    cd web && pnpm install && pnpm build      # → web/dist/index.html + assets/

If the build output is absent we degrade gracefully: ``rewind serve`` still
runs the OTLP receiver and read API, but ``GET /ui`` returns 404 with a
short HTML message telling the operator how to build the UI. This means
fresh clones can ``rewind serve`` immediately against an OTLP source, see
spans land in SQLite, and decide later whether to build the UI.
"""

from __future__ import annotations

from pathlib import Path

#: Absolute path to the shipped package's ``web/dist`` directory. Resolved at
#: import time so the value is stable across FastAPI requests.
#: When installed via pip the ``web/dist`` is bundled inside the wheel (see
#: ``pyproject.toml``'s ``[tool.hatch.build.targets.wheel]``). When running
#: from a checkout, it lives alongside ``src/rewind``.
_WEB_DIST: Path = Path(__file__).resolve().parent.parent.parent / "web" / "dist"


def ui_dist_path() -> Path | None:
    """Return the static UI directory if built, else ``None``.

    The caller is expected to handle the ``None`` case by serving a 404 + an
    HTML hint about running ``cd web && pnpm build``.
    """
    if _WEB_DIST.is_dir() and (_WEB_DIST / "index.html").is_file():
        return _WEB_DIST
    return None


__all__ = ["ui_dist_path"]
