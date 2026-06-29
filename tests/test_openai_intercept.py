"""Phase 3 Track 3B.2 — unit tests for the OpenAI interceptor.

Strategy
--------
We test the **dispatch logic** (``_dispatch_sync`` / ``_dispatch_async``)
directly by passing a fake ``orig_create`` callable that returns a canned
response. This isolates the frozen-serve / branch-forward / streaming-
failsclosed decisions without touching the real OpenAI HTTP client.

For the ``patch()`` context manager we install a *fake* ``openai.resources.
chat.completions`` module via ``sys.modules`` so install/uninstall and
idempotency can be exercised against a deterministic stub — real OpenAI
network behaviour is out of scope for unit tests.
"""

from __future__ import annotations

import asyncio
import sys
import types
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from rewind.enums import ReplayMode, SpanKind, SpanStatus
from rewind.models import Span, Trace, hash_payload
from rewind.openai_intercept import (
    InterceptError,
    _dispatch_async,
    _dispatch_sync,
    extract_signature,
    patch,
)
from rewind.replay import ReplayError, ReplaySession, active_session
from rewind.replay import replay as replay_ctx
from rewind.storage import TraceStore


# ----------------------------------------------------------------------
# Helpers / fixtures
# ----------------------------------------------------------------------
def _llm_span(
    trace_id: str,
    *,
    span_id: str,
    messages: list[dict[str, str]],
    model: str = "qwen3:32b",
    response_content: str = "hello",
) -> Span:
    """Build an LLM span carrying a stored ChatCompletion payload."""
    return Span(
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=None,
        name="chat.completions.create",
        kind=SpanKind.LLM,
        status=SpanStatus.OK,
        model_name=model,
        prompt_tokens=10,
        completion_tokens=2,
        total_tokens=12,
        messages_hash=hash_payload(messages),
        raw_attributes={
            "gen_ai.request.model": model,
            "gen_ai.response.model": model,
            "gen_ai.response": {
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 0,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": response_content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            },
        },
    )


@pytest.fixture
def store(tmp_path: Path) -> TraceStore:
    return TraceStore(str(tmp_path / "rewind.db"))


@pytest.fixture
def trace_id() -> str:
    return "abcd1234abcd1234abcd1234abcd1234"


@pytest.fixture
def seeded_store(
    store: TraceStore, trace_id: str
) -> tuple[TraceStore, list[Span], list[dict[str, str]]]:
    """Seed a 1-LLM-span trace and return (store, spans, messages)."""
    messages = [{"role": "user", "content": "hello"}]
    span = _llm_span(trace_id, span_id="a" * 16, messages=messages, response_content="hi")
    trace = Trace(trace_id=trace_id, spans=[span])
    store.upsert_trace(trace)
    store.insert_span(span)
    return store, [span], messages


def _fake_chat_completion(model: str, content: str) -> dict[str, Any]:
    return {
        "id": "chatcmpl-fake",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 9, "completion_tokens": 3, "total_tokens": 12},
    }


# ----------------------------------------------------------------------
# extract_signature
# ----------------------------------------------------------------------
def test_extract_signature_hashes_messages_and_tools() -> None:
    """``extract_signature`` returns deterministic hashes for a call."""
    msgs = [{"role": "user", "content": "hello"}]
    tools = [{"type": "function", "function": {"name": "search"}}]

    sig_no_tools = extract_signature(model="qwen3:32b", messages=msgs)
    sig_with_tools = extract_signature(model="qwen3:32b", messages=msgs, tools=tools)

    assert sig_no_tools.model == "qwen3:32b"
    assert sig_no_tools.messages_hash == hash_payload(msgs)
    assert sig_no_tools.tools_hash is None
    assert sig_with_tools.tools_hash == hash_payload(tools)
    # Different model name doesn't change messages_hash (model is logged separately).
    sig_other_model = extract_signature(model="gpt-4o", messages=msgs)
    assert sig_other_model.messages_hash == sig_no_tools.messages_hash


def test_extract_signature_empty_messages_yields_stable_hash() -> None:
    """Missing or empty messages produce a stable hash (not a crash)."""
    sig = extract_signature(model="x", messages=[])
    assert sig.messages_hash == hash_payload([])


# ----------------------------------------------------------------------
# _dispatch_sync (frozen serve / branch forward / streaming fail-closed)
# ----------------------------------------------------------------------
def test_dispatch_sync_serves_cached_payload_in_frozen(
    seeded_store: tuple[TraceStore, list[Span], list[dict[str, str]]],
    trace_id: str,
) -> None:
    """In FROZEN mode the dispatcher serves the recorded payload — no live call."""
    store, _spans, messages = seeded_store
    session = ReplaySession.for_root(store, trace_id, mode=ReplayMode.FROZEN)
    calls: list[Any] = []

    def orig_create(_self: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(kwargs)
        return _fake_chat_completion("live-caller", "LIVE")

    kwargs: dict[str, Any] = {"model": "qwen3:32b", "messages": messages}
    response = _dispatch_sync(object(), session, orig_create, (), kwargs)

    assert not calls  # No live call — entirely served from cache.
    body = response.model_dump() if hasattr(response, "model_dump") else response
    # The cached payload stores "hi" (from the seeded span).
    assert body["choices"][0]["message"]["content"] == "hi"
    assert session.cursor == 1


def test_dispatch_sync_branch_forwards_live_and_captures(
    seeded_store: tuple[TraceStore, list[Span], list[dict[str, str]]],
    trace_id: str,
) -> None:
    """In BRANCH mode a cache miss forwards live and records a new span."""
    store, _spans, _messages = seeded_store
    session = ReplaySession.for_root(store, trace_id, mode=ReplayMode.BRANCH)

    captured: dict[str, Any] = {}

    def orig_create(_self: Any, *args: Any, **kwargs: Any) -> Any:
        captured["called"] = True
        return _fake_chat_completion("live-model", "live-body")

    # Different messages so the cache misses.
    new_messages = [{"role": "user", "content": "different prompt"}]
    kwargs: dict[str, Any] = {"model": "qwen3:32b", "messages": new_messages}
    response = _dispatch_sync(object(), session, orig_create, (), kwargs)

    assert captured.get("called") is True
    body = response.model_dump() if hasattr(response, "model_dump") else response
    assert body["choices"][0]["message"]["content"] == "live-body"
    # The live-captured span is appended; cursor moves past the new tail
    # (seed had 1 span, +1 captured → cache length 2 → cursor is 2).
    assert session.cursor == 2


def test_dispatch_sync_streaming_in_frozen_raises(
    seeded_store: tuple[TraceStore, list[Span], list[dict[str, str]]],
    trace_id: str,
) -> None:
    """``stream=True`` in FROZEN mode fails closed (Phase 5 streaming replay)."""
    store, _spans, messages = seeded_store
    session = ReplaySession.for_root(store, trace_id, mode=ReplayMode.FROZEN)

    def orig_create(_self: Any, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("live call should not happen in frozen streaming test")

    kwargs: dict[str, Any] = {
        "model": "qwen3:32b",
        "messages": messages,
        "stream": True,
    }
    with pytest.raises(ReplayError, match="frozen streaming replay not yet supported"):
        _dispatch_sync(object(), session, orig_create, (), kwargs)


# ----------------------------------------------------------------------
# _dispatch_async
# ----------------------------------------------------------------------
def test_dispatch_async_serves_cached_payload_in_frozen(
    seeded_store: tuple[TraceStore, list[Span], list[dict[str, str]]],
    trace_id: str,
) -> None:
    """Async path mirrors sync: frozen replay returns the cached payload."""
    store, _spans, messages = seeded_store
    session = ReplaySession.for_root(store, trace_id, mode=ReplayMode.FROZEN)
    calls: list[Any] = []

    async def orig_create(_self: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(kwargs)
        return _fake_chat_completion("live", "LIVE")

    kwargs: dict[str, Any] = {"model": "qwen3:32b", "messages": messages}
    response = asyncio.run(_dispatch_async(object(), session, orig_create, (), kwargs))
    assert not calls  # No live call — entirely served from cache.
    body = response.model_dump() if hasattr(response, "model_dump") else response
    assert body["choices"][0]["message"]["content"] == "hi"


# ----------------------------------------------------------------------
# patch() lifecycle — uses a stub openai module installed in sys.modules
# ----------------------------------------------------------------------
@contextmanager
def _fake_openai_module() -> Iterator[dict[str, Any]]:
    """Install a fake ``openai.resources.chat.completions`` module in sys.modules.

    Provides a ``Completions`` and ``AsyncCompletions`` class whose ``create``
    methods are *replaced* by ``patch()`` and then restored. This is the
    minimal surface ``patch()`` touches.
    """
    # Save any previously-imported real submodule so we can restore it.
    saved = {
        key: sys.modules.get(key)
        for key in (
            "openai",
            "openai.resources",
            "openai.resources.chat",
            "openai.resources.chat.completions",
            "openai.types",
            "openai.types.chat",
        )
    }
    try:
        # Build the fake class objects. Create a fresh attribute each call so
        # the ``__rewind_patched__`` marker from a previous test doesn't leak.
        class Completions:
            def create(self, *args: Any, **kwargs: Any) -> Any:
                return {"_kind": "sync-original"}

        class AsyncCompletions:
            async def create(self, *args: Any, **kwargs: Any) -> Any:
                return {"_kind": "async-original"}

        completions_mod = types.ModuleType("openai.resources.chat.completions")
        completions_mod.Completions = Completions  # type: ignore[attr-defined]
        completions_mod.AsyncCompletions = AsyncCompletions  # type: ignore[attr-defined]

        chat_mod = types.ModuleType("openai.resources.chat")
        chat_mod.completions = completions_mod  # type: ignore[attr-defined]

        resources_mod = types.ModuleType("openai.resources")
        resources_mod.chat = chat_mod  # type: ignore[attr-defined]

        openai_mod = types.ModuleType("openai")
        openai_mod.resources = resources_mod  # type: ignore[attr-defined]

        sys.modules["openai"] = openai_mod
        sys.modules["openai.resources"] = resources_mod
        sys.modules["openai.resources.chat"] = chat_mod
        sys.modules["openai.resources.chat.completions"] = completions_mod
        # No types module → _chat_completion_module() returns None (deterministic).
        sys.modules.pop("openai.types", None)
        sys.modules.pop("openai.types.chat", None)

        yield {
            "Completions": Completions,
            "AsyncCompletions": AsyncCompletions,
        }
    finally:
        # Restore saved modules (or remove if they were absent).
        for key, mod in saved.items():
            if mod is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = mod


def test_patch_installs_and_restores() -> None:
    """``patch()`` installs the patched ``create`` and restores the original in exit."""
    with _fake_openai_module() as fake:
        Completions = fake["Completions"]
        original_create = Completions.create

        with patch():
            patched = Completions.create
            assert patched is not original_create
            assert getattr(patched, "__rewind_patched__", False) is True

        # Restored.
        assert Completions.create is original_create


def test_patch_is_idempotent_nested() -> None:
    """Nested ``with patch():`` calls do not double-patch or fail to restore."""
    with _fake_openai_module() as fake:
        Completions = fake["Completions"]
        original = Completions.create

        with patch():
            inner_patched = Completions.create
            with patch():
                # Second patch is a no-op: same object as outer patch.
                assert Completions.create is inner_patched
                assert getattr(Completions.create, "__rewind_patched__", False) is True
            # After inner exits we're still patched (outer is still active).
            assert Completions.create is inner_patched
        # Outer exit fully restores.
        assert Completions.create is original


def test_patch_restores_even_on_exception() -> None:
    """If the body raises, ``patch()`` still restores the originals."""
    with _fake_openai_module() as fake:
        Completions = fake["Completions"]
        original = Completions.create
        with pytest.raises(RuntimeError, match="boom"), patch():
            raise RuntimeError("boom")
        assert Completions.create is original


def test_patch_without_openai_raises_intercept_error() -> None:
    """If ``openai`` is uninstallable, ``patch()`` raises InterceptError.

    We simulate this by hiding the real openai from the importer for the
    duration of the test.
    """
    saved_openai = sys.modules.pop("openai", None)
    saved_resources = sys.modules.pop("openai.resources", None)
    saved_chat = sys.modules.pop("openai.resources.chat", None)
    saved_completions = sys.modules.pop("openai.resources.chat.completions", None)
    # Block the import path too.
    import builtins

    real_import = builtins.__import__

    def blocking_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "openai.resources.chat.completions":
            raise ImportError("simulated missing openai")
        return real_import(name, *args, **kwargs)

    builtins.__import__ = blocking_import
    try:
        with pytest.raises(InterceptError, match="requires the `openai` package"), \
                patch():
            pass
    finally:
        builtins.__import__ = real_import
        for key, mod in [
            ("openai", saved_openai),
            ("openai.resources", saved_resources),
            ("openai.resources.chat", saved_chat),
            ("openai.resources.chat.completions", saved_completions),
        ]:
            if mod is not None:
                sys.modules[key] = mod


def test_patch_passthrough_when_no_active_session() -> None:
    """When no replay session is active, ``patch()`` forwards to the original create."""
    with _fake_openai_module() as fake:
        Completions = fake["Completions"]
        instance = Completions()
        with patch():
            # No active replay context — call goes straight to the original.
            assert active_session() is None
            result = instance.create(model="x", messages=[])
            assert result == {"_kind": "sync-original"}


def test_patch_routes_through_replay_when_active(
    seeded_store: tuple[TraceStore, list[Span], list[dict[str, str]]],
    trace_id: str,
) -> None:
    """An active replay context routes ``create`` through the dispatcher."""
    store, _spans, messages = seeded_store
    with _fake_openai_module() as fake:
        Completions = fake["Completions"]
        instance = Completions()
        with patch(), replay_ctx(store, trace_id, mode=ReplayMode.FROZEN):
            assert active_session() is not None
            response = instance.create(model="qwen3:32b", messages=messages)
            body = (
                response.model_dump() if hasattr(response, "model_dump") else response
            )
            # Served from cache — payload content is "hi", not original.
            assert body["choices"][0]["message"]["content"] == "hi"
