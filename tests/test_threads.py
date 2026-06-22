"""Tests for the Threads (Meta) adapter."""

from __future__ import annotations

import json
from unittest import mock

import pytest
import requests

from autoxpost.config import Config, ThreadsConfig
from autoxpost.core.post import Post
from autoxpost.core.safety import RateLimited, RateLimitSignal
from autoxpost.platforms import build_adapters
from autoxpost.platforms.threads import ThreadsAdapter


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _adapter() -> ThreadsAdapter:
    cfg = ThreadsConfig(user_id="12345", access_token="fake-token")
    return ThreadsAdapter(cfg)


def _post(text: str = "hello threads", metadata: dict | None = None,
          media_paths: list[str] | None = None) -> Post:
    return Post(
        text=text,
        targets=["threads"],
        media_paths=media_paths or [],
        metadata=metadata or {},
    )


def _mock_response(status: int = 200, body: dict | None = None,
                   headers: dict | None = None) -> mock.Mock:
    r = mock.Mock(spec=requests.Response)
    r.status_code = status
    r.headers = headers or {}
    r.text = json.dumps(body) if body is not None else ""
    r.json.return_value = body if body is not None else {}
    if status >= 300:
        r.raise_for_status.side_effect = requests.HTTPError(
            f"{status} error", response=r
        )
    else:
        r.raise_for_status.return_value = None
    return r


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_threads_config_is_configured_requires_both() -> None:
    assert ThreadsConfig(user_id=None, access_token=None).is_configured is False
    assert ThreadsConfig(user_id="u", access_token=None).is_configured is False
    assert ThreadsConfig(user_id=None, access_token="t").is_configured is False
    assert ThreadsConfig(user_id="u", access_token="t").is_configured is True


def test_threads_adapter_built_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("THREADS_USER_ID", "12345")
    monkeypatch.setenv("THREADS_ACCESS_TOKEN", "tok")
    cfg = Config.load()
    adapters = build_adapters(cfg)
    assert "threads" in adapters
    assert isinstance(adapters["threads"], ThreadsAdapter)


def test_threads_adapter_not_built_without_env() -> None:
    cfg = Config()
    assert "threads" not in build_adapters(cfg)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_threads_validate_text_over_500_chars() -> None:
    a = _adapter()
    err = a.validate(_post("a" * 501))
    assert err is not None
    assert "500" in err


def test_threads_validate_text_at_limit_ok() -> None:
    a = _adapter()
    assert a.validate(_post("a" * 500)) is None


def test_threads_validate_media_without_metadata_url_rejects() -> None:
    """Local media without a hosted URL is rejected with a clear hint."""
    a = _adapter()
    post = _post("with media", media_paths=["/tmp/photo.jpg"])
    err = a.validate(post)
    assert err is not None
    assert "threads_image_url" in err


def test_threads_validate_media_with_metadata_url_ok() -> None:
    a = _adapter()
    post = _post(
        "with media",
        media_paths=["/tmp/photo.jpg"],
        metadata={"threads_image_url": "https://example.com/img.jpg"},
    )
    assert a.validate(post) is None


# ---------------------------------------------------------------------------
# publish — happy paths
# ---------------------------------------------------------------------------


def test_threads_publish_text_only_hits_both_endpoints() -> None:
    """A text-only post calls the container endpoint with TEXT and the
    publish endpoint with the returned container id."""
    a = _adapter()
    responses = [
        _mock_response(200, {"id": "container_abc"}),  # create
        _mock_response(200, {"id": "post_xyz"}),       # publish
    ]
    with mock.patch.object(a._session, "post", side_effect=responses) as p:
        result = a.publish(_post("hello"))

    assert result.success is True
    assert result.remote_id == "post_xyz"
    assert result.remote_url and result.remote_url.endswith("/post_xyz")

    # First call: container create with TEXT media_type.
    container_call = p.call_args_list[0]
    assert container_call.args[0].endswith("/12345/threads")
    body = container_call.kwargs["data"]
    assert body["media_type"] == "TEXT"
    assert body["text"] == "hello"
    assert body["access_token"] == "fake-token"

    # Second call: publish with creation_id.
    publish_call = p.call_args_list[1]
    assert publish_call.args[0].endswith("/12345/threads_publish")
    publish_body = publish_call.kwargs["data"]
    assert publish_body["creation_id"] == "container_abc"
    assert publish_body["access_token"] == "fake-token"


def test_threads_publish_with_image_url_uses_image_type() -> None:
    a = _adapter()
    responses = [
        _mock_response(200, {"id": "container_img"}),
        _mock_response(200, {"id": "post_img"}),
    ]
    post = _post(
        "with image",
        media_paths=["/tmp/x.jpg"],
        metadata={"threads_image_url": "https://cdn.example.com/x.jpg"},
    )
    with mock.patch.object(a._session, "post", side_effect=responses) as p:
        result = a.publish(post)

    assert result.success is True
    container_body = p.call_args_list[0].kwargs["data"]
    assert container_body["media_type"] == "IMAGE"
    assert container_body["image_url"] == "https://cdn.example.com/x.jpg"
    assert container_body["text"] == "with image"


def test_threads_publish_network_error_returns_failure() -> None:
    a = _adapter()
    with mock.patch.object(
        a._session, "post",
        side_effect=requests.ConnectionError("dns blew up"),
    ):
        result = a.publish(_post("hi"))
    assert result.success is False
    assert "network" in (result.error or "")


def test_threads_publish_5xx_returns_failure() -> None:
    a = _adapter()
    with mock.patch.object(
        a._session, "post",
        return_value=_mock_response(500, body={"error": "boom"}),
    ):
        result = a.publish(_post("hi"))
    assert result.success is False
    assert "HTTP 500" in (result.error or "")


def test_threads_publish_container_missing_id_returns_failure() -> None:
    """A 200 with no id is treated as a failure, not a silent success."""
    a = _adapter()
    with mock.patch.object(
        a._session, "post",
        return_value=_mock_response(200, body={}),  # no id
    ):
        result = a.publish(_post("hi"))
    assert result.success is False
    assert "no id" in (result.error or "")


# ---------------------------------------------------------------------------
# publish — rate limit
# ---------------------------------------------------------------------------


def test_threads_429_translates_to_rate_limited() -> None:
    """A 429 from the API is raised as RateLimited with the Retry-After hint."""
    a = _adapter()
    resp = _mock_response(
        429, body={"error": "rate limited"}, headers={"Retry-After": "60"},
    )
    with mock.patch.object(a._session, "post", return_value=resp):
        with pytest.raises(RateLimited) as excinfo:
            a.publish(_post("hi"))

    signal: RateLimitSignal = excinfo.value.signal
    assert signal.retry_after_seconds == 60.0
    assert "threads" in signal.reason


def test_threads_429_without_retry_after_still_raises() -> None:
    a = _adapter()
    resp = _mock_response(429, body={"error": "rate limited"})
    with mock.patch.object(a._session, "post", return_value=resp):
        with pytest.raises(RateLimited):
            a.publish(_post("hi"))


def test_threads_429_during_publish_step_also_caught() -> None:
    """If the container succeeds but the publish step 429s, we still raise."""
    a = _adapter()
    container_ok = _mock_response(200, {"id": "container_abc"})
    publish_429 = _mock_response(429, headers={"Retry-After": "30"})
    with mock.patch.object(
        a._session, "post", side_effect=[container_ok, publish_429],
    ):
        with pytest.raises(RateLimited):
            a.publish(_post("hi"))


# ---------------------------------------------------------------------------
# Safety integration (default rule + media limit)
# ---------------------------------------------------------------------------


def test_threads_in_safety_defaults() -> None:
    """The default safety rule set includes Threads."""
    from autoxpost.core.safety import _default_rules, build_guard

    rules = _default_rules()
    assert "threads" in rules
    assert rules["threads"].min_interval_seconds == 60
    assert rules["threads"].daily_cap == 50

    g = build_guard(enabled=True, store=None, platforms=["threads"])
    assert "threads" in g.rules


def test_threads_in_media_limits() -> None:
    from autoxpost.core.safety import MEDIA_LIMITS, validate_media

    assert "threads" in MEDIA_LIMITS
    # Empty media_paths returns None regardless of platform limits.
    p = _post("no media")
    assert validate_media(p, "threads") is None
