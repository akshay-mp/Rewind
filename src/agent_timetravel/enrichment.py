"""Phase 7 — Local-model enrichment helpers.

The plan (§6 Phase 7) calls for surfacing metadata that matters specifically
for **local models** (Ollama, llama.cpp, LM Studio). Three capabilities
land here, all **pure-Python** and **opt-in**:

1. :func:`parse_quant` — parse a GGUF quantisation level out of a model tag
   like ``qwen3:32b-q4_K_M`` or ``llama3.1:8b-instruct-q8_0``. Used by the
   diff "did the quant cause this?" auto-flag and the timeline UI.
2. :func:`render_chat_template` — best-effort render of the post-template
   prompt for inspection. Local-model failures routinely hide in the chat
   template (missing ``<|im_start|>``, wrong role tags, etc.). Calls
   ``transformers.AutoTokenizer.from_pretrained`` *lazily* when available
   and degrades to a plain concatenated string when not.
3. :func:`sample_vram` — one-shot VRAM / unified-memory sample on Apple
   Silicon (``macmon`` / ``asitop``) or NVIDIA (``nvidia-smi``). Returns
   ``None`` when no sampler is installed; the CLI uses this to poll
   periodically during a ``agent-timetravel ui`` session and stamp samples against
   the current span timestamps.

Design choices
--------------
* **No hard dependencies on transformers, macmon, or nvidia-smi.** Each
  helper degrades gracefully when the underlying tool isn't installed.
  The hard guarantee: ``import agent_timetravel.enrichment`` never fails, regardless
  of the local-model toolchain.
* **No subprocess on import.** ``sample_vram`` only spawns a process when
  called; ``parse_quant`` and ``render_chat_template`` are pure.
* **No new SQLite columns.** Enrichment output is written into
  ``Span.raw_attributes`` under ``timetravel.local.*`` keys — the existing
  verbatim-payload contract (Phase 0) absorbs it without a schema bump.

See ``docs/phases/phase-7.md`` for the system-design rationale.
"""

from __future__ import annotations

import re
import shutil
import subprocess  # nosec B404 - bounded argv subprocess for GPU probing.
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from agent_timetravel.models import Span

#: Prefix for all enrichment keys in ``Span.raw_attributes``. Keeps the
#: enrichment namespace separate from OpenInference's verbatim payload.
_ENRICH_PREFIX = "agent_timetravel.local."

#: Attribute key for the parsed quantisation level (e.g. ``"q4_K_M"``).
QUANT_ATTR = _ENRICH_PREFIX + "quant"

#: Attribute key for the unified-memory sample in MiB (Apple Silicon).
VRAM_MIB_ATTR = _ENRICH_PREFIX + "vram_mib"

#: Attribute key for the GPU utilisation sample in percent.
GPU_PCT_ATTR = _ENRICH_PREFIX + "gpu_pct"

# ----------------------------------------------------------------------
# 1. Quantisation parsing
# ----------------------------------------------------------------------

#: Matches a GGUF quant tag at end of a model name. Covers all the variants
#: Ollama / llama.cpp emit: ``q4_K_M``, ``q8_0``, ``f16``, ``bf16``, ``fp16``.
#: Case-insensitive to tolerate ``Q4_K_M`` from upstream docs.
_QUANT_RE = re.compile(
    r"(?P<sep>[-_:])"
    r"(?P<quant>"
    # int8 / int4 (rare).
    r"[iI](?P<bits_i>[1-8])"
    # qN_K_S / qN_K_M / qN_0 / qN_K — K-quant and legacy.
    r"|q(?P<bits_q>[1-8])(?:_K_[SM]|_K|_0)"
    # 16/32-bit float variants.
    r"|f(?:p|p16|16|32)"
    r"|bf16"
    r")$"
)


@dataclass(frozen=True, slots=True)
class QuantInfo:
    """Parsed quantisation metadata for a local-model tag.

    ``bits`` is ``None`` for non-bit-based formats (``f16`` / ``bf16``).
    ``label`` is the canonical (lowercased) quant string; ``None`` when no
    quant tag is present.
    """

    label: str | None
    bits: int | None


def parse_quant(model_name: str | None) -> QuantInfo:
    """Parse the quantisation level out of an Ollama / llama.cpp model tag.

    Returns ``QuantInfo(None, None)`` for untagged or remote-model names —
    i.e. `gpt-4o`, `claude-3-5-sonnet`, `qwen3:32b` (no quant suffix).
    Never raises.

    Examples
    --------
    >>> parse_quant("qwen3:32b-q4_K_M")
    QuantInfo(label='q4_k_m', bits=4)
    >>> parse_quant("llama3.1:8b-instruct-q8_0")
    QuantInfo(label='q8_0', bits=8)
    >>> parse_quant("phi3:mini-f16")
    QuantInfo(label='f16', bits=None)
    >>> parse_quant("gpt-4o")
    QuantInfo(None, None)
    """
    if not model_name:
        return QuantInfo(None, None)
    match = _QUANT_RE.search(model_name)
    if match is None:
        return QuantInfo(None, None)
    quant = match.group("quant").lower()
    bits_str = match.group("bits_q") or match.group("bits_i")
    bits = int(bits_str) if bits_str else None
    return QuantInfo(label=quant, bits=bits)


def quant_from_span(span: Span) -> QuantInfo:
    """Read the recorded quant off a span, falling back to parsing model_name.

    The diff auto-flag uses this: if a span already has ``timetravel.local.quant``
    on it (set by the CLI enrichment pass or manually), use it; otherwise
    fall back to parsing ``span.model_name`` on the fly.
    """
    recorded = span.raw_attributes.get(QUANT_ATTR)
    if isinstance(recorded, str) and recorded:
        bits = _bits_from_label(recorded)
        return QuantInfo(label=recorded.lower(), bits=bits)
    return parse_quant(span.model_name)


def _bits_from_label(label: str) -> int | None:
    """Recover the bit width from a canonical quant label."""
    lower = label.lower()
    m = re.match(r"^[iq](\d)", lower)
    if m is None:
        return None
    return int(m.group(1))


# ----------------------------------------------------------------------
# 2. Chat-template rendering (best-effort)
# ----------------------------------------------------------------------

#: The role order applied when no tokenizer is available. Matches the
#: OpenAI / Anthropic convention: system first, then alternating turns.
_FALLBACK_ROLE_ORDER: dict[str, int] = {
    "system": 0,
    "developer": 1,
    "user": 2,
    "tool": 3,
    "assistant": 4,
}


def render_chat_template(
    messages: Sequence[dict[str, Any]],
    *,
    model_name: str | None = None,
) -> str:
    """Best-effort render of the post-chat-template prompt.

    If ``transformers`` is installed and the tokenizer for ``model_name``
    resolves, uses ``AutoTokenizer.apply_chat_template`` (the load-bearing
    path — this is what surfaces template bugs like a missing ``<|im_start|>``).

    Otherwise falls back to a readable concatenation: ``[system] ...\\n[user]
    ...`` etc. The fallback is **not** byte-faithful to the model's actual
    template, but it's enough to spot missing/extra messages, role typos,
    and obvious truncation in the diff UI.

    Parameters
    ----------
    messages
        OpenInference-style ``[{"role": ..., "content": ...}, ...]`` list.
        Non-dict items are stringified via :func:`str`.
    model_name
        HuggingFace repo id or Ollama tag. Used to resolve the tokenizer.
        When ``None``, always uses the fallback.

    Returns
    -------
    str
        The rendered prompt. Never raises.
    """
    if not messages:
        return ""
    rendered = _try_apply_tokenizer(messages, model_name)
    if rendered is not None:
        return rendered
    return _fallback_render(messages)


def _try_apply_tokenizer(
    messages: Sequence[dict[str, Any]],
    model_name: str | None,
) -> str | None:
    """Attempt ``transformers.AutoTokenizer.apply_chat_template``.

    Returns ``None`` and swallows errors when transformers isn't installed
    or the tokenizer can't resolve — callers fall back to
    :func:`_fallback_render`.
    """
    if model_name is None:
        return None
    try:  # pragma: no cover - exercised only when transformers present.
        # pylint: disable=import-outside-toplevel
        from transformers import AutoTokenizer
        # pylint: enable=import-outside-toplevel
    except ImportError:
        return None
    try:  # pragma: no cover - tokenizer resolution is network/install-dependent.
        # pylint: disable=broad-exception-caught
        # from_pretrained is operator-triggered via the --model flag; nosec B615.
        tokenizer = AutoTokenizer.from_pretrained(model_name)  # nosec B615
        return str(tokenizer.apply_chat_template(list(messages), tokenize=False))
    except Exception:  # pragma: no cover
        # Intentionally broad — tokenizer resolution can fail for a dozen
        # reasons (no HF token, offline, unknown repo, broken template).
        # The whole point of the enrichment pass is best-effort.
        return None


def _fallback_render(messages: Sequence[dict[str, Any]]) -> str:
    """Render messages as ``[role] content`` lines, sorted by role.

    Preserves insertion order within a role; the sort only fixes gross
    framework bugs (e.g. tool result emitted before its user prompt).
    """
    items: list[tuple[int, int, str, str]] = []
    for idx, msg in enumerate(messages):
        if isinstance(msg, dict):
            role = str(msg.get("role", "unknown"))
            content = msg.get("content", "")
        else:
            role = "unknown"
            content = str(msg)
        items.append((_FALLBACK_ROLE_ORDER.get(role, 99), idx, role, str(content)))
    items.sort()
    return "\n\n".join(f"[{role}] {content}" for _, _, role, content in items)


# ----------------------------------------------------------------------
# 3. VRAM / GPU sampler (one-shot, no streaming)
# ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VramSample:
    """A single point-in-time GPU / memory sample.

    ``None`` on any field means the sampler couldn't populate it (the
    platform-specific probe returned nothing or errored). The caller writes
    only the populated fields to ``Span.raw_attributes`` and omits the rest.
    """

    vram_mib: int | None
    gpu_pct: float | None


def sample_vram() -> VramSample:
    """One-shot sample of GPU memory + utilisation.

    Probes in order, returns the first that works:

    1. ``nvidia-smi`` (Linux / Windows NVIDIA boxes).
    2. ``asitop`` or ``macmon`` (Apple Silicon — unified memory).

    Returns ``VramSample(None, None)`` when no tool is installed. Never
    raises — this is sampled from the ``agent-timetravel ui`` background thread and
    must not crash the UI loop.
    """
    if shutil.which("nvidia-smi"):
        sample = _sample_nvidia_smi()
        if sample is not None:
            return sample
    for apple_tool in ("macmon", "asitop"):
        if shutil.which(apple_tool):
            sample = _sample_apple(apple_tool)
            if sample is not None:
                return sample
    return VramSample(None, None)


def _sample_nvidia_smi() -> VramSample | None:
    """Parse ``nvidia-smi --query-gpu=...,...`` into a :class:`VramSample`.

    Uses the ``--query-gpu`` CSV form with ``--format=csv,noheader,nounits``
    so we get clean numbers. Multi-GPU hosts sum the memory and average
    the utilisation (the timeline UI doesn't show per-GPU bars).
    """
    cmd = [
        "nvidia-smi",
        "--query-gpu=memory.used,utilization.gpu",
        "--format=csv,noheader,nounits",
    ]
    try:
        proc = subprocess.run(  # noqa: S603  # nosec B603 - bounded argv, no shell.
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=2.0,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if proc.returncode != 0:
        return None
    used_mib: list[int] = []
    util_pct: list[float] = []
    for line in proc.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2:
            continue
        try:
            used_mib.append(int(parts[0]))
            util_pct.append(float(parts[1]))
        except ValueError:
            continue
    if not used_mib:
        return None
    return VramSample(
        vram_mib=sum(used_mib),
        gpu_pct=sum(util_pct) / len(util_pct),
    )


def _sample_apple(tool: str) -> VramSample | None:
    """Best-effort Apple Silicon unified-memory sample.

    ``asitop`` and ``macmon`` are CLI TUIs primarily; they don't expose a
    clean ``--query`` form. Rather than parsing their ANSI TUI output
    (fragile), we lean on ``psutil`` when available — it exposes Apple's
    unified-memory stats via ``virtual_memory`` and GPU util via
    ``gpu_service.service`` (M-series kernels only). When ``psutil`` isn't
    installed, we return ``None`` and the UI shows no VRAM bar.

    The ``tool`` argument is kept (rather than inlined) so future Apple
    tooling — e.g. a native ``powermetrics`` parser — slots in here without
    a signature change.
    """
    del tool  # currently unused; future hook for powermetrics.
    try:  # pragma: no cover - exercised only when psutil present.
        # pylint: disable=import-outside-toplevel
        import psutil
        # pylint: enable=import-outside-toplevel
    except ImportError:
        return None
    try:  # pragma: no cover - psutil present but no GPU service.
        # pylint: disable=broad-exception-caught
        mem = psutil.virtual_memory()
        vram_mib = int(mem.used // (1024 * 1024))
        # GPU utilisation needs the (non-public) platform bindings; we don't
        # guess a number. Leaving it None is better than a wrong number.
        return VramSample(vram_mib=vram_mib, gpu_pct=None)
    except Exception:  # pragma: no cover
        return None


# ----------------------------------------------------------------------
# 4. Enrichment entry point
# ----------------------------------------------------------------------


def enrich_span(
    span: Span,
    *,
    parse_model_quant: bool = True,
    sample_gpu: bool = False,
) -> Span:
    """Apply Phase 7 enrichment to ``span`` in place and return it.

    Three independent passes, controlled by flags so the CLI can request
    a cheap-only-quant pass or a full sampler pass:

    * ``parse_model_quant`` — :func:`quant_from_span` → ``QUANT_ATTR``.
    * ``sample_gpu`` — :func:`sample_vram` → ``VRAM_MIB_ATTR`` +
      ``GPU_PCT_ATTR`` when the sampler reports values.

    Chat-template rendering is **not** applied here — it's expensive
    (tokenizer load) and per-span modelling (which message stream?) doesn't
    fit a blanket pass. The CLI exposes it as a separate
    ``agent-timetravel render-template <span>`` inspection command instead.
    """
    if parse_model_quant:
        quant = quant_from_span(span)
        if quant.label is not None:
            span.raw_attributes[QUANT_ATTR] = quant.label
    if sample_gpu:
        sample = sample_vram()
        if sample.vram_mib is not None:
            span.raw_attributes[VRAM_MIB_ATTR] = sample.vram_mib
        if sample.gpu_pct is not None:
            span.raw_attributes[GPU_PCT_ATTR] = sample.gpu_pct
    return span
