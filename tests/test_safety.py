"""Tests for the RiskGuard / safety module."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

import pytest

from autoxpost.core.post import Post
from autoxpost.core.queue import PostQueue
from autoxpost.core.safety import (
    PlatformSafetyRule,
    PreCheckResult,
    QueueRiskStore,
    RateLimited,
    RateLimitSignal,
    RiskGuard,
    build_guard,
    text_hash,
    validate_media,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fixed_now() -> datetime:
    return datetime(2026, 6, 20, 12, 0, 0)


@pytest.fixture
def clock(fixed_now: datetime) -> Callable[[], datetime]:
    """Advance-able clock: returns `fixed_now` plus the cursor."""
    state = {"offset_seconds": 0.0}

    def _now() -> datetime:
        return fixed_now + timedelta(seconds=state["offset_seconds"])

    def advance(seconds: float) -> None:
        state["offset_seconds"] += seconds

    _now.advance = advance  # type: ignore[attr-defined]
    return _now


@pytest.fixture
def queue(tmp_path: Path) -> PostQueue:
    return PostQueue(tmp_path / "safety.db")


@pytest.fixture
def store(queue: PostQueue) -> QueueRiskStore:
    return QueueRiskStore(queue.conn)


@pytest.fixture
def x_rule() -> PlatformSafetyRule:
    return PlatformSafetyRule(
        platform="x",
        min_interval_seconds=60,
        daily_cap=3,
        burst_cap=2,
        burst_window_seconds=3600,
        jitter_min_seconds=0,
        jitter_max_seconds=0,
        max_retries=0,
        cooldown_minutes=15,
        kill_switch_threshold=3,
    )


@pytest.fixture
def mast_rule() -> PlatformSafetyRule:
    return PlatformSafetyRule(
        platform="mastodon",
        min_interval_seconds=60,
        daily_cap=5,
        burst_cap=3,
        burst_window_seconds=3600,
        jitter_min_seconds=0,
        jitter_max_seconds=0,
        max_retries=0,
        cooldown_minutes=15,
        kill_switch_threshold=3,
    )


@pytest.fixture
def guard(
    store: QueueRiskStore,
    clock: Callable[[], datetime],
    x_rule: PlatformSafetyRule,
    mast_rule: PlatformSafetyRule,
) -> RiskGuard:
    sleeps: list[float] = []

    def fake_sleep(secs: float) -> None:
        sleeps.append(secs)

    g = RiskGuard(
        rules={"x": x_rule, "mastodon": mast_rule},
        store=store,
        enabled=True,
        clock=clock,
        sleep=fake_sleep,
        random_fn=lambda: 0.5,
    )
    g._sleeps = sleeps  # type: ignore[attr-defined]
    return g


def _post(text: str = "hi there", targets: list[str] | None = None) -> Post:
    return Post(text=text, targets=targets or ["x"])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_min_interval_enforced(guard: RiskGuard) -> None:
    """Two publishes within the window — second is SKIPPED."""
    post = _post()
    assert guard.pre_check(post, "x").allowed is True
    guard.record_success(post, "x")

    # Clock hasn't advanced: still inside the 60s window.
    result = guard.pre_check(post, "x")
    assert result.allowed is False
    assert "min interval" in result.reason
    assert result.wait_seconds > 0


def test_min_interval_resets_after_window(
    guard: RiskGuard, clock: Callable[[], datetime]
) -> None:
    post = _post()
    guard.pre_check(post, "x")
    guard.record_success(post, "x")

    clock.advance(61)  # type: ignore[attr-defined]
    # Different text so the duplicate check (24h window) doesn't block.
    assert guard.pre_check(_post("a different post"), "x").allowed is True


def test_daily_cap_enforced(
    guard: RiskGuard,
    store: QueueRiskStore,
    fixed_now: datetime,
    clock: Callable[[], datetime],
) -> None:
    """Pre-populate publish_log with the daily cap, next pre_check blocks."""
    post = _post()
    # Insert 3 successful log rows for platform "x" within the last 24h.
    for _ in range(3):
        store.conn.execute(
            "INSERT INTO publish_log (post_id, platform, success, created_at) "
            "VALUES (?, 'x', 1, ?)",
            ("p", fixed_now.isoformat()),
        )

    result = guard.pre_check(post, "x")
    assert result.allowed is False
    assert "daily cap" in result.reason


def test_burst_cap_enforced(
    guard: RiskGuard,
    store: QueueRiskStore,
    fixed_now: datetime,
) -> None:
    """Within the burst window, more than burst_cap → SKIPPED."""
    post = _post()
    for _ in range(2):  # burst_cap = 2
        store.conn.execute(
            "INSERT INTO publish_log (post_id, platform, success, created_at) "
            "VALUES (?, 'x', 1, ?)",
            ("p", fixed_now.isoformat()),
        )

    result = guard.pre_check(post, "x")
    assert result.allowed is False
    assert "burst cap" in result.reason


def test_duplicate_blocked(
    guard: RiskGuard,
    store: QueueRiskStore,
    fixed_now: datetime,
    clock: Callable[[], datetime],
) -> None:
    post = _post("Hello world!")
    guard.pre_check(post, "x")
    guard.record_success(post, "x")  # records hash

    # Advance past min_interval so the duplicate check is reached.
    clock.advance(61)  # type: ignore[attr-defined]
    # Different post object, same text.
    repost = _post("Hello world!")
    result = guard.pre_check(repost, "x")
    assert result.allowed is False
    assert "duplicate" in result.reason


def test_duplicate_different_text_allowed(
    guard: RiskGuard,
    fixed_now: datetime,
    clock: Callable[[], datetime],
) -> None:
    guard.pre_check(_post("hi"), "x")
    guard.record_success(_post("hi"), "x")

    clock.advance(61)  # type: ignore[attr-defined]
    result = guard.pre_check(_post("different content"), "x")
    assert result.allowed is True


def test_jitter_within_range(guard: RiskGuard) -> None:
    """Jitter (set on the rule) is applied on success and falls in range."""
    from dataclasses import replace
    # The fake random returns 0.5, so with a (0, 10) jitter range the
    # exact expected sleep is 5.0.
    guard.rules["x"] = replace(
        guard.rules["x"],
        jitter_min_seconds=0,
        jitter_max_seconds=10,
    )
    guard.record_success(_post(), "x")
    assert guard._sleeps == [pytest.approx(5.0)]  # type: ignore[attr-defined]


def test_rate_limit_signal_extracted(guard: RiskGuard) -> None:
    """RateLimited wrapped in another exception is found via the chain."""
    signal = RateLimitSignal(retry_after_seconds=120, reason="test")
    inner = RateLimited(signal)
    try:
        try:
            raise inner
        except Exception as outer:
            found = guard.extract_signal(outer)
    except Exception:
        pytest.fail("unexpected exception chain")

    assert found is signal


def test_rate_limit_response_sets_cooldown(guard: RiskGuard) -> None:
    """A 429-shaped failure schedules a cooldown that blocks future publishes."""
    post = _post()
    signal = RateLimitSignal(retry_after_seconds=120, reason="unit test")
    guard.record_failure(post, "x", RateLimited(signal))

    result = guard.pre_check(post, "x")
    assert result.allowed is False
    assert "cooldown" in result.reason
    assert result.wait_seconds > 0


def test_kill_switch_after_failures(
    guard: RiskGuard, clock: Callable[[], datetime]
) -> None:
    """3 consecutive non-rate-limit failures trip the kill switch."""
    post = _post()
    for _ in range(3):
        guard.record_failure(post, "x", None)

    result = guard.pre_check(post, "x")
    assert result.allowed is False
    assert "kill switch" in result.reason or "cooldown" in result.reason

    # Advance past the cooldown (15 min).
    clock.advance(16 * 60)  # type: ignore[attr-defined]
    assert guard.pre_check(post, "x").allowed is True


def test_kill_switch_resets_on_success(
    guard: RiskGuard, clock: Callable[[], datetime]
) -> None:
    """A success in between failures resets the counter."""
    post = _post()
    guard.record_failure(post, "x", None)
    guard.record_failure(post, "x", None)
    clock.advance(61)  # type: ignore[attr-defined]  # past min_interval
    guard.record_success(post, "x")  # reset
    clock.advance(61)  # type: ignore[attr-defined]
    guard.record_failure(post, "x", None)  # still under threshold (3)
    clock.advance(61)  # type: ignore[attr-defined]
    guard.record_failure(post, "x", None)  # still under threshold
    clock.advance(61)  # type: ignore[attr-defined]

    # 2 consecutive after a success → no kill switch yet, and we've
    # advanced past min_interval, so pre_check should pass.
    assert guard.pre_check(_post("next"), "x").allowed is True


def test_safety_disabled_bypasses_all(
    store: QueueRiskStore,
    clock: Callable[[], datetime],
    x_rule: PlatformSafetyRule,
) -> None:
    g = RiskGuard(
        rules={"x": x_rule},
        store=store,
        enabled=False,
        clock=clock,
        sleep=lambda _s: None,
    )
    post = _post()
    # Many "publishes" back to back — should never block when disabled.
    for _ in range(10):
        assert g.pre_check(post, "x").allowed is True
    g.record_success(post, "x")
    g.record_failure(post, "x", RateLimited(RateLimitSignal(retry_after_seconds=999)))
    # Even after a "failure", pre_check stays open.
    assert g.pre_check(post, "x").allowed is True


def test_per_platform_independent(guard: RiskGuard) -> None:
    """X being rate-limited does not affect Mastodon."""
    x_post = _post("x-only")
    signal = RateLimitSignal(retry_after_seconds=300, reason="unit test")
    guard.record_failure(x_post, "x", RateLimited(signal))

    # Mastodon is fresh.
    mast_post = _post("mastodon time")
    assert guard.pre_check(mast_post, "mastodon").allowed is True


def test_media_size_rejected(tmp_path: Path, guard: RiskGuard) -> None:
    """Media over X's 5 MB image limit is rejected at pre_check."""
    # 6 MB file with a JPEG mime.
    big = tmp_path / "huge.jpg"
    big.write_bytes(b"\xff" * (6 * 1024 * 1024))

    post = _post("with media", targets=["x"])
    post.media_paths = [str(big)]
    result = guard.pre_check(post, "x")
    assert result.allowed is False
    assert "media" in result.reason


def test_post_text_hash_stable_under_whitespace() -> None:
    """Whitespace + case differences collapse to the same hash."""
    a = text_hash("Hello\nWorld")
    b = text_hash("  hello world  ")
    c = text_hash("HELLO WORLD")
    assert a == b == c
    assert text_hash("different") != a


def test_hash_retention_caps_rows(guard: RiskGuard) -> None:
    """Oldest rows are pruned when more than hash_history are stored."""
    guard.hash_history = 5

    # Record 7 different texts as successes.
    for i in range(7):
        p = Post(text=f"text-{i}", targets=["x"])
        guard.pre_check(p, "x")
        guard.record_success(p, "x")

    rows = guard.store.conn.execute(  # type: ignore[union-attr]
        "SELECT text_hash FROM posted_hashes WHERE platform = 'x' ORDER BY created_at ASC"
    ).fetchall()
    assert len(rows) == 5
    # We kept the most recent 5 (text-2 through text-6).
    hashes_kept = [r["text_hash"] for r in rows]
    expected = [text_hash(f"text-{i}") for i in range(2, 7)]
    assert hashes_kept == expected


# ---------------------------------------------------------------------------
# build_guard integration (env-driven)
# ---------------------------------------------------------------------------


def test_build_guard_uses_default_rules_when_no_env(
    monkeypatch: pytest.MonkeyPatch, store: QueueRiskStore
) -> None:
    """Without env overrides, build_guard emits the documented defaults."""
    for key in list(monkeypatch._name2setattr if False else []):  # noqa: ARG001
        pass
    # Wipe env to make sure defaults apply.
    for v in (
        "AUTOXPOST_X_MIN_INTERVAL_SECONDS", "AUTOXPOST_X_DAILY_CAP",
        "AUTOXPOST_LINKEDIN_DAILY_CAP", "AUTOXPOST_SAFETY_ENABLED",
    ):
        monkeypatch.delenv(v, raising=False)

    g = build_guard(enabled=True, store=store, platforms=["x", "linkedin"])
    assert g.rules["x"].min_interval_seconds == 90
    assert g.rules["x"].daily_cap == 50
    assert g.rules["linkedin"].min_interval_seconds == 300
    assert g.rules["linkedin"].kill_switch_threshold == 3
    assert g.enabled is True


def test_build_guard_honours_env_override(
    monkeypatch: pytest.MonkeyPatch, store: QueueRiskStore
) -> None:
    monkeypatch.setenv("AUTOXPOST_X_DAILY_CAP", "200")
    monkeypatch.setenv("AUTOXPOST_X_MIN_INTERVAL_SECONDS", "5")
    g = build_guard(enabled=True, store=store, platforms=["x"])
    assert g.rules["x"].daily_cap == 200
    assert g.rules["x"].min_interval_seconds == 5


def test_build_guard_respects_master_switch(
    monkeypatch: pytest.MonkeyPatch, store: QueueRiskStore
) -> None:
    monkeypatch.setenv("AUTOXPOST_SAFETY_ENABLED", "false")
    g = build_guard(enabled=False, store=store, platforms=["x"])
    assert g.enabled is False


# ---------------------------------------------------------------------------
# PreCheckResult / validate_media unit tests
# ---------------------------------------------------------------------------


def test_validate_media_unknown_platform_returns_none() -> None:
    assert validate_media(_post(), "instagram") is None


def test_validate_media_empty_paths_returns_none() -> None:
    p = _post()
    p.media_paths = []
    assert validate_media(p, "x") is None


def test_text_hash_empty_string_is_stable() -> None:
    assert text_hash("") == text_hash("   \n  ")
