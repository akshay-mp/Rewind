"""Unit tests for packaged and checkout UI artifact resolution."""

from __future__ import annotations

from pathlib import Path

from agent_timetravel import ui_assets


def _make_ui(path: Path) -> Path:
    path.mkdir()
    (path / "index.html").write_text("<!doctype html>", encoding="utf-8")
    return path


def test_ui_dist_path_prefers_packaged_ui(tmp_path: Path, monkeypatch) -> None:
    packaged = _make_ui(tmp_path / "packaged")
    checkout = _make_ui(tmp_path / "checkout")
    monkeypatch.setattr(ui_assets, "_PACKAGED_UI", packaged)
    monkeypatch.setattr(ui_assets, "_CHECKOUT_UI", checkout)

    assert ui_assets.ui_dist_path() == packaged


def test_ui_dist_path_falls_back_to_checkout_ui(tmp_path: Path, monkeypatch) -> None:
    checkout = _make_ui(tmp_path / "checkout")
    monkeypatch.setattr(ui_assets, "_PACKAGED_UI", tmp_path / "missing-packaged")
    monkeypatch.setattr(ui_assets, "_CHECKOUT_UI", checkout)

    assert ui_assets.ui_dist_path() == checkout


def test_ui_dist_path_returns_none_when_index_is_missing(tmp_path: Path, monkeypatch) -> None:
    packaged = tmp_path / "packaged"
    packaged.mkdir()
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    monkeypatch.setattr(ui_assets, "_PACKAGED_UI", packaged)
    monkeypatch.setattr(ui_assets, "_CHECKOUT_UI", checkout)

    assert ui_assets.ui_dist_path() is None
