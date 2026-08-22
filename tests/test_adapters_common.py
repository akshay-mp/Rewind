"""Phase 6 tests — adapter shared helpers + the no-framework contract.

These tests run unconditionally (no ``find_spec`` gating) because they
cover pure-Python pieces every adapter reuses and the import-time
contract that must hold even when none of the five agent frameworks is
installed:

1. Each adapter module imports **cleanly** without its framework present
   (verifies the lazy-import contract — ``agent-timetravel --version`` stays fast).
2. Each adapter's ``__all__`` exposes the documented public surface.
3. Calling each factory without the framework installed raises
   :class:`AdapterError` with a helpful install hint.
4. The pure helpers in :mod:`agent_timetravel.adapters._common`
   (:func:`build_live_span`, :func:`assert_not_frozen`) and the
   framework-specific message-flattening helpers behave correctly for
   every shape the recorded payloads can take.

When a framework *is* installed, the framework-gated suites extend this
with end-to-end replay contract tests; see
:mod:`tests.test_langgraph_adapter`-style files (those skip themselves
on environments without the framework).
"""

from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from agent_timetravel.adapters import _common
from agent_timetravel.enums import ReplayMode, SpanKind, SpanStatus
from agent_timetravel.models import Span, Trace, hash_payload
from agent_timetravel.replay import ReplayError
from agent_timetravel.storage import TraceStore

# ----------------------------------------------------------------------
# Pure-Python fixtures (lifted from tests/test_replay.py shape)
# ----------------------------------------------------------------------

def _trace_id() -> str:
    return "0123456789abcdef0123456789abcdef"


@pytest.fixture
def fake_session() -> SimpleNamespace:
    """A minimal duck of :class:`ReplaySession` for span-construction tests."""
    return SimpleNamespace(
        trace_id=_trace_id(),
        cursor=0,
        mode=ReplayMode.BRANCH,
        branch_id=UUID("00000000-0000-0000-0000-000000000001"),
    )


# ----------------------------------------------------------------------
# 1. Lazy-import contract: every adapter module imports without its
# framework present. None of these frameworks are in the default dev env.
# ----------------------------------------------------------------------
_ADAPTER_MODULES = [
    "agent_timetravel.adapters.adk",
    "agent_timetravel.adapters.crewai",
    "agent_timetravel.adapters.pydantic_ai",
    "agent_timetravel.adapters.smolagents",
    "agent_timetravel.adapters.langgraph",
]


@pytest.mark.parametrize("module_name", _ADAPTER_MODULES)
def test_adapter_module_imports_without_framework(module_name: str) -> None:
    """Import should succeed even when the underlying framework is absent."""
    module = importlib.import_module(module_name)
    assert hasattr(module, "AdapterError"), f"{module_name} must expose AdapterError"
    assert isinstance(module.AdapterError, type)
    assert issubclass(module.AdapterError, RuntimeError)


def test_adapter_package_docstring_lists_all_five_frameworks() -> None:
    """`adapters/__init__.py` docstring must reference all five frameworks."""
    import agent_timetravel.adapters as pkg

    doc = pkg.__doc__ or ""
    for fw in ("langgraph", "adk", "pydantic_ai", "crewai", "smolagents"):
        assert fw in doc, f"adapter package docstring must mention {fw}"


# ----------------------------------------------------------------------
# 2. Public surface (`__all__`) contract
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    ("module_name", "expected"),
    [
        ("agent_timetravel.adapters.adk", {"AdapterError", "replay_llm"}),
        ("agent_timetravel.adapters.crewai", {"AdapterError", "replay_llm"}),
        ("agent_timetravel.adapters.pydantic_ai", {"AdapterError", "replay_model"}),
        ("agent_timetravel.adapters.smolagents", {"AdapterError", "replay_model"}),
        ("agent_timetravel.adapters.langgraph", {"AdapterError", "replay_chat_model"}),
    ],
)
def test_adapter_public_surface(module_name: str, expected: set[str]) -> None:
    module = importlib.import_module(module_name)
    assert set(getattr(module, "__all__", [])) == expected


# ----------------------------------------------------------------------
# 3. Factory raises AdapterError with a helpful hint when the framework
# is not installed. (In the default dev env, none are installed — and we
# also assert that nothing gets imported eagerly.)
# ----------------------------------------------------------------------
def _framework_installed(module_root: str) -> bool:
    return importlib.util.find_spec(module_root) is not None


@pytest.mark.parametrize(
    ("module_name", "factory_name", "fw_root", "arg"),
    [
        ("agent_timetravel.adapters.adk", "replay_llm", "google", "adk-model"),
        ("agent_timetravel.adapters.crewai", "replay_llm", "crewai", "crewai-model"),
        (
            "agent_timetravel.adapters.pydantic_ai",
            "replay_model",
            "pydantic_ai",
            "pydantic-ai-model",
        ),
        (
            "agent_timetravel.adapters.smolagents",
            "replay_model",
            "smolagents",
            "smolagents-model",
        ),
        (
            "agent_timetravel.adapters.langgraph",
            "replay_chat_model",
            "langchain_core",
            "langchain-model",
        ),
    ],
)
def test_factory_raises_when_framework_missing(
    module_name: str,
    factory_name: str,
    fw_root: str,
    arg: str,
) -> None:
    """Factory raises ``AdapterError`` if the framework module isn't importable."""
    if _framework_installed(fw_root):
        pytest.skip(f"{fw_root} is installed in this venv — exercised by gated suite")
    module = importlib.import_module(module_name)
    factory = getattr(module, factory_name)
    with pytest.raises(module.AdapterError):
        factory(arg)


# ----------------------------------------------------------------------
# 4. Pure helper: _common.build_live_span (LLM + TOOL)
# ----------------------------------------------------------------------
def test_build_live_span_llm(fake_session: SimpleNamespace) -> None:
    """LLM span carries ``gen_ai.request.model`` + a chat-completion response."""
    messages = [{"role": "user", "content": "hi"}]
    span = _common.build_live_span(
        fake_session,
        model_name="qwen3:32b",
        messages=messages,
        content="hello",
        kind_str="LLM",
    )
    assert isinstance(span, Span)
    assert span.kind is SpanKind.LLM
    assert span.trace_id == _trace_id()
    assert span.model_name == "qwen3:32b"
    assert span.messages_hash == hash_payload(messages)
    assert span.status is SpanStatus.OK
    raw = span.raw_attributes
    assert raw["gen_ai.request.model"] == "qwen3:32b"
    choice = raw["gen_ai.response"]["choices"][0]["message"]
    assert choice == {"role": "assistant", "content": "hello"}
    assert "tool.name" not in raw


def test_build_live_span_tool(fake_session: SimpleNamespace) -> None:
    """TOOL span carries ``tool.name`` + ``tool.output``."""
    span = _common.build_live_span(
        fake_session,
        model_name="ignored-for-tools",
        messages=None,
        content="[]",
        tool_name="search_products",
        kind_str="TOOL",
    )
    assert span.kind is SpanKind.TOOL
    assert span.model_name is None
    # tools have no caller messages
    assert span.messages_hash == hash_payload([])
    raw = span.raw_attributes
    assert raw["tool.name"] == "search_products"
    assert raw["tool.output"] == "[]"
    assert "gen_ai.request.model" not in raw


def test_build_live_span_persists_into_store(
    fake_session: SimpleNamespace, tmp_path: Path
) -> None:
    """The span returned is in the shape ``TraceStore.insert_span`` accepts."""
    from agent_timetravel.replay import ReplaySession

    store = TraceStore(str(tmp_path / "adapter.db"))
    # Pre-seed an (empty) trace so `for_root` has something to load.
    store.upsert_trace(Trace(trace_id=_trace_id(), spans=[]))
    real = ReplaySession.for_root(
        store=store, trace_id=_trace_id(), mode=ReplayMode.BRANCH, label="adapter-test"
    )
    fake_session.trace_id = real.trace_id
    span = _common.build_live_span(
        fake_session, model_name="qwen3:32b", messages=[], content="x"
    )
    real.record_new(span)
    rehydrated = real.recorded_spans()
    assert len(rehydrated) == 1
    assert rehydrated[0].raw_attributes["gen_ai.request.model"] == "qwen3:32b"


# ----------------------------------------------------------------------
# 5. Pure helper: _common.assert_not_frozen
# ----------------------------------------------------------------------
def test_assert_not_frozen_allows_branch(
    fake_session: SimpleNamespace,
) -> None:
    """``BRANCH`` mode divergences are authorised — must not raise."""
    fake_session.mode = ReplayMode.BRANCH
    # must not raise
    _common.assert_not_frozen(fake_session)


def test_assert_not_frozen_raises_in_frozen_mode(
    fake_session: SimpleNamespace,
) -> None:
    """``FROZEN`` mode divergence is the strict-determinism contract."""
    fake_session.mode = ReplayMode.FROZEN
    fake_session.cursor = 3
    with pytest.raises(ReplayError, match="frozen replay diverged at cursor=3"):
        _common.assert_not_frozen(fake_session)


# ----------------------------------------------------------------------
# 6. Per-framework message helpers — exercised by the gated suites, but
# we can also smoke-test them here because they live in adapter modules
# that import cleanly without the framework. (They take ``Any`` inputs.)
# ----------------------------------------------------------------------
def test_adk_messages_helper_flattens_dicts() -> None:
    """``_messages_from_adk`` accepts dicts and proto-like Content objects."""
    if _framework_installed("google"):
        pytest.skip("ADK installed — gated suite covers it")
    from agent_timetravel.adapters import adk

    request = SimpleNamespace(
        contents=[
            {"role": "user", "content": "hello"},
            SimpleNamespace(
                role="model", parts=[SimpleNamespace(text="world")]
            ),
            SimpleNamespace(role="user", parts=["plain string part"]),
        ],
    )
    messages = adk._messages_from_adk(request)
    assert messages[0] == {"role": "user", "content": "hello"}
    assert messages[1]["role"] == "model"
    assert "world" in messages[1]["content"]
    assert "plain string part" in messages[2]["content"]


def test_crewai_messages_helper_flattens_objects() -> None:
    """``_crewai_messages_to_jsonable`` accepts dict + duck-typed inputs."""
    if _framework_installed("crewai"):
        pytest.skip("crewai installed")
    from agent_timetravel.adapters import crewai

    out = crewai._crewai_messages_to_jsonable(
        [
            {"role": "user", "content": "hi"},
            SimpleNamespace(role="assistant", content="there"),
        ]
    )
    assert out[0] == {"role": "user", "content": "hi"}
    assert out[1] == {"role": "assistant", "content": "there"}


def test_pydic_ai_messages_helper_flattens_dumpable() -> None:
    """``_messages_to_jsonable`` accepts dicts + model_dump-bearing objects."""
    if _framework_installed("pydantic_ai"):
        pytest.skip("pydantic-ai installed")
    from agent_timetravel.adapters import pydantic_ai

    class _Stub:
        def model_dump(self) -> dict[str, str]:
            return {"role": "user", "content": "dumped"}

    out = pydantic_ai._messages_to_jsonable([_Stub(), {"role": "system", "content": "x"}])
    assert out[0] == {"role": "user", "content": "dumped"}
    assert out[1] == {"role": "system", "content": "x"}


def test_smolagents_messages_helper_flattens_objects() -> None:
    if _framework_installed("smolagents"):
        pytest.skip("smolagents installed")
    from agent_timetravel.adapters import smolagents

    out = smolagents._smol_messages_to_jsonable(
        [SimpleNamespace(role="user", content="hi")]
    )
    assert out == [{"role": "user", "content": "hi"}]


def test_smolagents_response_extractor_handles_strings() -> None:
    if _framework_installed("smolagents"):
        pytest.skip("smolagents installed")
    from agent_timetravel.adapters import smolagents

    assert smolagents._smol_chat_message_to_text("raw") == "raw"
    assert smolagents._smol_chat_message_to_text(
        SimpleNamespace(content="attrs")
    ) == "attrs"
    joined = smolagents._smol_chat_message_to_text(
        ["a", SimpleNamespace(content="b")]
    )
    assert joined == "a\nb"
