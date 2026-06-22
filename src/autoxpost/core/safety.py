"""Risk control — anti-ban safety for the publisher.

AutoXPost ships with a ``RiskGuard`` that wraps every adapter call to:

- enforce a minimum interval between posts on the same platform
- enforce a daily cap and a short-window burst cap
- detect duplicate text within a window and skip the repost
- jitter the wait between successful posts (human-like randomness)
- recognise 429 / rate-limit responses on each platform and pause
- trip a kill switch that pauses a platform after N consecutive failures

Defaults err on the side of safety for *new* accounts; everything is
env-overridable via ``AUTOXPOST_<UPPER>_<FIELD>``. Set
``AUTOXPOST_SAFETY_ENABLED=false`` to disable the guard entirely.

The state for daily-cap counts and dedup hashes is persisted in SQLite
(via :class:`QueueRiskStore`) so it survives restarts; the rest is
in-memory on the guard and resets when the process restarts.
"""

from __future__ import annotations

import hashlib
import logging
import os
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Iterable

from autoxpost.core.post import Post

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Rate-limit signal
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RateLimitSignal:
    """Unified "wait until" hint parsed from any platform's 429 response."""

    retry_after_seconds: float | None = None
    reset_at: datetime | None = None
    reason: str = ""

    def wait_seconds(self, now: datetime) -> float:
        """Best estimate of how long the caller should back off (>=0)."""
        candidates: list[float] = []
        if self.retry_after_seconds is not None:
            candidates.append(max(0.0, float(self.retry_after_seconds)))
        if self.reset_at is not None:
            delta = (self.reset_at - now).total_seconds()
            candidates.append(max(0.0, delta))
        return max(candidates) if candidates else 0.0


class RateLimited(Exception):
    """Adapter raised a recoverable rate-limit. Carries a :class:`RateLimitSignal`."""

    def __init__(self, signal: RateLimitSignal, original: BaseException | None = None):
        super().__init__(signal.reason or "rate limited")
        self.signal = signal
        self.original = original


# ---------------------------------------------------------------------------
# Per-platform rules
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlatformSafetyRule:
    """Thresholds for a single platform. Override any field via env."""

    platform: str
    min_interval_seconds: int = 60
    daily_cap: int = 100
    burst_cap: int = 5
    burst_window_seconds: int = 3600
    jitter_min_seconds: int = 0
    jitter_max_seconds: int = 30
    max_retries: int = 2
    cooldown_minutes: int = 15
    kill_switch_threshold: int = 5

    @property
    def jitter_range(self) -> tuple[int, int]:
        return (self.jitter_min_seconds, self.jitter_max_seconds)


def _default_rules() -> dict[str, PlatformSafetyRule]:
    """Conservative defaults for *new/unverified* accounts.

    Veteran accounts with a track record should raise ``daily_cap`` and
    shrink ``min_interval_seconds`` via env.
    """
    return {
        "x": PlatformSafetyRule(
            platform="x",
            min_interval_seconds=90,
            daily_cap=50,
            burst_cap=5,
            burst_window_seconds=3600,
            jitter_min_seconds=15,
            jitter_max_seconds=60,
            max_retries=2,
            cooldown_minutes=30,
            kill_switch_threshold=5,
        ),
        "mastodon": PlatformSafetyRule(
            platform="mastodon",
            min_interval_seconds=60,
            daily_cap=300,
            burst_cap=10,
            burst_window_seconds=3600,
            jitter_min_seconds=10,
            jitter_max_seconds=45,
            max_retries=3,
            cooldown_minutes=15,
            kill_switch_threshold=5,
        ),
        "bluesky": PlatformSafetyRule(
            platform="bluesky",
            min_interval_seconds=30,
            daily_cap=1500,
            burst_cap=30,
            burst_window_seconds=3600,
            jitter_min_seconds=5,
            jitter_max_seconds=20,
            max_retries=3,
            cooldown_minutes=15,
            kill_switch_threshold=5,
        ),
        "linkedin": PlatformSafetyRule(
            platform="linkedin",
            min_interval_seconds=300,
            daily_cap=50,
            burst_cap=3,
            burst_window_seconds=3600,
            jitter_min_seconds=60,
            jitter_max_seconds=180,
            max_retries=1,
            cooldown_minutes=60,
            kill_switch_threshold=3,
        ),
    }


# ---------------------------------------------------------------------------
# Persistent store (SQLite-backed)
# ---------------------------------------------------------------------------


class QueueRiskStore:
    """SQLite-backed storage for things that must survive a restart.

    Daily-cap counts are computed from the existing ``publish_log`` table;
    the posted-hash dedup table is new but lives alongside it.
    """

    def __init__(self, conn) -> None:
        self.conn = conn

    # --- daily / burst counts (read existing publish_log) ---

    def success_count(self, platform: str, since: datetime) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM publish_log "
            "WHERE platform = ? AND success = 1 AND created_at >= ?",
            (platform, since.isoformat()),
        ).fetchone()
        return int(row["n"]) if row else 0

    # --- posted hash dedup ---

    def is_duplicate(self, platform: str, text_hash: str, since: datetime) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM posted_hashes "
            "WHERE platform = ? AND text_hash = ? AND created_at >= ? LIMIT 1",
            (platform, text_hash, since.isoformat()),
        ).fetchone()
        return row is not None

    def record_hash(self, post_id: str, platform: str, text_hash: str,
                    when: datetime) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO posted_hashes "
            "(platform, text_hash, post_id, created_at) VALUES (?, ?, ?, ?)",
            (platform, text_hash, post_id, when.isoformat()),
        )

    def trim_hashes(self, platform: str, keep: int) -> None:
        """Trim ``posted_hashes`` for ``platform`` down to the newest ``keep`` rows."""
        # Count current rows; if over the cap, delete the oldest excess.
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM posted_hashes WHERE platform = ?",
            (platform,),
        ).fetchone()
        total = int(row["n"]) if row else 0
        if total <= keep:
            return
        excess = total - keep
        # Delete the oldest `excess` rows by created_at.
        self.conn.execute(
            "DELETE FROM posted_hashes WHERE platform = ? AND rowid IN ("
            "  SELECT rowid FROM posted_hashes WHERE platform = ? "
            "  ORDER BY created_at ASC LIMIT ?"
            ")",
            (platform, platform, excess),
        )


# ---------------------------------------------------------------------------
# Pre-check / record outcome
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PreCheckResult:
    """Outcome of a single pre-publish safety check."""

    allowed: bool
    reason: str = ""
    wait_seconds: float = 0.0


def _normalise_text(text: str) -> str:
    """Stable normalisation used by the dedup hash.

    - collapse all whitespace to a single space
    - strip leading/trailing whitespace
    - lowercase (so case-only edits collapse)
    """
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def text_hash(text: str) -> str:
    """Stable SHA-1 of normalised text. Suitable for SQLite dedup."""
    return hashlib.sha1(_normalise_text(text).encode("utf-8")).hexdigest()


# Media size limits lifted from the platforms' own docs.
# Image max for X is 5 MB; Bluesky image max is ~1 MB.
MEDIA_LIMITS: dict[str, dict[str, int | tuple[str, ...]]] = {
    "x": {"image_max_bytes": 5 * 1024 * 1024, "allowed_mimes": (
        "image/jpeg", "image/png", "image/webp", "image/gif",
    )},
    "bluesky": {"image_max_bytes": 1 * 1024 * 1024, "allowed_mimes": (
        "image/jpeg", "image/png", "image/webp",
    )},
}


def validate_media(post: Post, platform: str) -> str | None:
    """Return an error string if any media file is over the platform's size/mime limit.

    Used by the publisher before invoking the adapter so we don't burn
    an upload attempt on something the platform would reject.
    """
    limits = MEDIA_LIMITS.get(platform)
    if not limits or not post.media_paths:
        return None
    max_bytes = int(limits["image_max_bytes"])
    allowed_mimes: tuple[str, ...] = limits["allowed_mimes"]  # type: ignore[assignment]
    import mimetypes
    for path in post.media_paths:
        try:
            size = os.path.getsize(path)
        except OSError as exc:
            return f"media {path}: {exc}"
        if size > max_bytes:
            mb = max_bytes / (1024 * 1024)
            return f"media {path}: {size} bytes exceeds {platform} limit ({mb:.0f} MB)"
        mime, _ = mimetypes.guess_type(path)
        if mime and mime not in allowed_mimes:
            return f"media {path}: mime {mime!r} not in {platform} allowed set"
    return None


# ---------------------------------------------------------------------------
# RiskGuard
# ---------------------------------------------------------------------------


class RiskGuard:
    """The publisher-side safety wrapper.

    Holds in-memory state (last publish time, consecutive-failure count,
    cooldown-until timestamp per platform) plus an optional
    :class:`QueueRiskStore` for durable counts and dedup.
    """

    def __init__(
        self,
        rules: dict[str, PlatformSafetyRule],
        store: QueueRiskStore | None = None,
        enabled: bool = True,
        duplicate_window: timedelta = timedelta(hours=24),
        hash_history: int = 200,
        clock: Callable[[], datetime] = datetime.utcnow,
        sleep: Callable[[float], None] = time.sleep,
        random_fn: Callable[[], float] = random.random,
    ) -> None:
        self.rules = rules
        self.store = store
        self.enabled = enabled
        self.duplicate_window = duplicate_window
        self.hash_history = hash_history
        self._clock = clock
        self._sleep = sleep
        self._random = random_fn

        # Per-platform in-memory state.
        self._last_publish_at: dict[str, datetime] = {}
        self._consecutive_failures: dict[str, int] = {}
        self._cooldown_until: dict[str, datetime] = {}

    # --- construction helpers ---

    @classmethod
    def from_rules(cls, rules: dict[str, PlatformSafetyRule],
                   store: QueueRiskStore | None = None) -> "RiskGuard":
        return cls(rules=rules, store=store)

    # --- pre-publish check ---

    def pre_check(self, post: Post, platform: str) -> PreCheckResult:
        if not self.enabled:
            return PreCheckResult(allowed=True)
        rule = self.rules.get(platform)
        if rule is None:
            # No rule → trust the caller. Unknown platforms should never
            # reach the publisher anyway (adapters are the only source).
            return PreCheckResult(allowed=True)

        now = self._clock()

        # Cooldown from a recent rate-limit response or kill-switch trip.
        until = self._cooldown_until.get(platform)
        if until and until > now:
            wait = (until - now).total_seconds()
            return PreCheckResult(
                allowed=False,
                reason=f"safety: cooldown active until {until.isoformat()} (wait {wait:.0f}s)",
                wait_seconds=wait,
            )

        # Minimum interval since last publish on this platform.
        last = self._last_publish_at.get(platform)
        if last is not None:
            elapsed = (now - last).total_seconds()
            if elapsed < rule.min_interval_seconds:
                wait = rule.min_interval_seconds - elapsed
                return PreCheckResult(
                    allowed=False,
                    reason=(
                        f"safety: min interval not elapsed "
                        f"({elapsed:.0f}s/{rule.min_interval_seconds}s; wait {wait:.0f}s)"
                    ),
                    wait_seconds=wait,
                )

        # Daily cap.
        if self.store is not None:
            since = now - timedelta(hours=24)
            count = self.store.success_count(platform, since)
            if count >= rule.daily_cap:
                return PreCheckResult(
                    allowed=False,
                    reason=f"safety: daily cap reached ({count}/{rule.daily_cap})",
                )

        # Burst cap (per burst_window_seconds).
        if self.store is not None and rule.burst_window_seconds > 0:
            since = now - timedelta(seconds=rule.burst_window_seconds)
            count = self.store.success_count(platform, since)
            if count >= rule.burst_cap:
                return PreCheckResult(
                    allowed=False,
                    reason=(
                        f"safety: burst cap reached "
                        f"({count}/{rule.burst_cap} in {rule.burst_window_seconds}s)"
                    ),
                )

        # Duplicate text.
        if self.store is not None:
            since = now - self.duplicate_window
            h = text_hash(post.text)
            if self.store.is_duplicate(platform, h, since):
                return PreCheckResult(
                    allowed=False,
                    reason=(
                        f"safety: duplicate post within "
                        f"{int(self.duplicate_window.total_seconds() // 3600)}h window"
                    ),
                )

        # Media size / mime.
        media_err = validate_media(post, platform)
        if media_err:
            return PreCheckResult(allowed=False, reason=f"safety: {media_err}")

        return PreCheckResult(allowed=True)

    # --- outcome recording ---

    def record_success(self, post: Post, platform: str) -> None:
        """Mark a successful publish: update state, store hash, apply jitter."""
        if not self.enabled:
            return
        now = self._clock()
        self._last_publish_at[platform] = now
        self._consecutive_failures.pop(platform, None)
        # NOTE: do NOT clear _cooldown_until here; it has an explicit
        # expiry checked in pre_check.
        if self.store is not None:
            self.store.record_hash(post.id, platform, text_hash(post.text), now)
            self.store.trim_hashes(platform, self.hash_history)

        rule = self.rules.get(platform)
        if rule is not None:
            lo, hi = rule.jitter_range
            if hi > 0 and hi >= lo:
                # self._random() in [0,1) so lo+.. never exceeds hi.
                jitter = lo + self._random() * max(0, hi - lo)
                if jitter > 0:
                    self._sleep(jitter)

    def record_failure(self, post: Post, platform: str,
                       exc: BaseException | None) -> None:
        """Mark a failed publish: bump failure counter; trip kill switch on Nth."""
        if not self.enabled:
            return
        rule = self.rules.get(platform)
        if rule is None:
            return

        # Was this a rate-limit? If so, set cooldown to that hint too.
        signal = self.extract_signal(exc) if exc is not None else None
        now = self._clock()
        if signal is not None:
            wait = signal.wait_seconds(now)
            if wait > 0:
                self._cooldown_until[platform] = now + timedelta(seconds=wait)
                log.warning(
                    "rate limit on %s: pausing for %.0fs (%s)",
                    platform, wait, signal.reason or "rate limited",
                )

        # Failure counter / kill switch.
        count = self._consecutive_failures.get(platform, 0) + 1
        self._consecutive_failures[platform] = count
        if count >= rule.kill_switch_threshold:
            cooldown_end = now + timedelta(minutes=rule.cooldown_minutes)
            existing = self._cooldown_until.get(platform)
            # Don't shorten an already-later cooldown.
            if existing is None or existing < cooldown_end:
                self._cooldown_until[platform] = cooldown_end
            log.warning(
                "kill switch on %s: %d consecutive failures, "
                "cooldown until %s",
                platform, count, self._cooldown_until[platform].isoformat(),
            )

    # --- rate-limit signal extraction ---

    def extract_signal(self, exc: BaseException) -> RateLimitSignal | None:
        """Walk the exception chain looking for a :class:`RateLimited`."""
        cur: BaseException | None = exc
        seen: set[int] = set()
        while cur is not None and id(cur) not in seen:
            if isinstance(cur, RateLimited):
                return cur.signal
            seen.add(id(cur))
            nxt = cur.__cause__ or cur.__context__
            cur = nxt if isinstance(nxt, BaseException) else None
        return None

    # --- introspection (mainly for tests / status commands) ---

    def status_snapshot(self) -> dict[str, dict[str, object]]:
        """Return a snapshot of per-platform state for status displays."""
        snap: dict[str, dict[str, object]] = {}
        now = self._clock()
        for p, rule in self.rules.items():
            snap[p] = {
                "rule": rule,
                "last_publish_at": self._last_publish_at.get(p),
                "consecutive_failures": self._consecutive_failures.get(p, 0),
                "cooldown_until": self._cooldown_until.get(p),
                "cooldown_active": (
                    self._cooldown_until.get(p, datetime.min) > now
                ),
            }
        return snap


# ---------------------------------------------------------------------------
# Env-driven rule loading
# ---------------------------------------------------------------------------


def _env_int(key: str) -> int | None:
    raw = os.environ.get(key)
    if raw is None or raw.strip() == "":
        return None
    try:
        return int(raw.strip())
    except ValueError:
        log.warning("ignoring invalid int env %s=%r", key, raw)
        return None


def load_rules(defaults: dict[str, PlatformSafetyRule] | None = None,
               platforms: Iterable[str] | None = None
               ) -> dict[str, PlatformSafetyRule]:
    """Build a ``{platform: rule}`` dict from defaults + env overrides.

    Recognised env keys per platform ``P`` (uppercase):

    - ``AUTOXPOST_P_MIN_INTERVAL_SECONDS``
    - ``AUTOXPOST_P_DAILY_CAP``
    - ``AUTOXPOST_P_BURST_CAP``
    - ``AUTOXPOST_P_BURST_WINDOW_SECONDS``
    - ``AUTOXPOST_P_JITTER_MIN_SECONDS``
    - ``AUTOXPOST_P_JITTER_MAX_SECONDS``
    - ``AUTOXPOST_P_MAX_RETRIES``
    - ``AUTOXPOST_P_COOLDOWN_MINUTES``
    - ``AUTOXPOST_P_KILL_SWITCH_THRESHOLD``
    """
    base = defaults if defaults is not None else _default_rules()
    out: dict[str, PlatformSafetyRule] = {}
    wanted = list(platforms) if platforms is not None else list(base.keys())
    for p in wanted:
        default = base.get(p) or PlatformSafetyRule(platform=p)
        u = p.upper()
        overrides: dict[str, int] = {}
        for field_name, env_key in [
            ("min_interval_seconds", f"AUTOXPOST_{u}_MIN_INTERVAL_SECONDS"),
            ("daily_cap", f"AUTOXPOST_{u}_DAILY_CAP"),
            ("burst_cap", f"AUTOXPOST_{u}_BURST_CAP"),
            ("burst_window_seconds", f"AUTOXPOST_{u}_BURST_WINDOW_SECONDS"),
            ("jitter_min_seconds", f"AUTOXPOST_{u}_JITTER_MIN_SECONDS"),
            ("jitter_max_seconds", f"AUTOXPOST_{u}_JITTER_MAX_SECONDS"),
            ("max_retries", f"AUTOXPOST_{u}_MAX_RETRIES"),
            ("cooldown_minutes", f"AUTOXPOST_{u}_COOLDOWN_MINUTES"),
            ("kill_switch_threshold", f"AUTOXPOST_{u}_KILL_SWITCH_THRESHOLD"),
        ]:
            if (v := _env_int(env_key)) is not None:
                overrides[field_name] = v
        if overrides:
            out[p] = default.__class__(platform=p, **{
                **{f: getattr(default, f) for f in default.__dataclass_fields__ if f != "platform"},
                **overrides,
            })
        else:
            out[p] = default
    return out


# ---------------------------------------------------------------------------
# Builder from Config
# ---------------------------------------------------------------------------


def build_guard(enabled: bool, store: QueueRiskStore | None = None,
                platforms: Iterable[str] | None = None) -> RiskGuard:
    """Construct a :class:`RiskGuard` from defaults + env, optional persistent store.

    Kept as a free function (rather than a method on :class:`Config`) so
    the safety module has no dependency on the config layer.
    """
    rules = load_rules(platforms=platforms)
    dup_hours = _env_int("AUTOXPOST_DUPLICATE_WINDOW_HOURS") or 24
    history = _env_int("AUTOXPOST_HASH_HISTORY") or 200
    return RiskGuard(
        rules=rules,
        store=store,
        enabled=enabled,
        duplicate_window=timedelta(hours=dup_hours),
        hash_history=history,
    )
