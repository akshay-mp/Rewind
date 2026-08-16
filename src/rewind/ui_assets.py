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

#: The wheel carries the built UI inside the Python package. The checkout
#: fallback keeps local development working without copying generated files
#: into ``src/rewind``.
_PACKAGED_UI: Path = Path(__file__).resolve().parent / "_ui"
_CHECKOUT_UI: Path = Path(__file__).resolve().parents[2] / "web" / "dist"


def ui_dist_path() -> Path | None:
    """Return the static UI directory if built, else ``None``.

    The caller is expected to handle the ``None`` case by serving a 404 + an
    HTML hint about running ``cd web && pnpm build``.
    """
    for candidate in (_PACKAGED_UI, _CHECKOUT_UI):
        if candidate.is_dir() and (candidate / "index.html").is_file():
            return candidate
    return None


__all__ = ["ui_dist_path"]
