"""Tests for the platform adapter base + per-platform build logic."""

from __future__ import annotations

import builtins
from unittest import mock

import pytest

from autoxpost.config import Config
from autoxpost.platforms import build_adapters
from autoxpost.platforms.base import PlatformAdapter


def test_empty_config_yields_no_adapters() -> None:
    cfg = Config()
    assert build_adapters(cfg) == {}


def test_linkedin_adapter_built_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LINKEDIN_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("LINKEDIN_AUTHOR_URN", "urn:li:person:abc")
    cfg = Config.load()
    adapters = build_adapters(cfg)
    assert "linkedin" in adapters
    assert isinstance(adapters["linkedin"], PlatformAdapter)


def test_x_adapter_skipped_when_tweepy_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the optional SDK isn't installed, the factory should skip the platform."""
    monkeypatch.setenv("X_BEARER_TOKEN", "x")
    monkeypatch.setenv("X_API_KEY", "k")
    monkeypatch.setenv("X_API_SECRET", "s")
    monkeypatch.setenv("X_ACCESS_TOKEN", "a")
    monkeypatch.setenv("X_ACCESS_SECRET", "v")
    cfg = Config.load()
    assert cfg.x.is_configured

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "tweepy":
            raise ImportError("simulated missing tweepy")
        return real_import(name, *args, **kwargs)

    with mock.patch.object(builtins, "__import__", side_effect=fake_import):
        adapters = build_adapters(cfg)

    assert "x" not in adapters
