"""Configuration loading.

Reads `.env` (if present) and exposes platform config as a small typed
namespace. Only platforms that have *all* required env vars set are
considered "enabled" — the rest are silently skipped so a user can drop
in tokens one at a time.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

log = logging.getLogger(__name__)

# Load .env from the cwd by default; users can point elsewhere with AUTOXPOST_ENV.
load_dotenv(os.environ.get("AUTOXPOST_ENV", ".env"), override=False)


def _env(key: str) -> str | None:
    val = os.environ.get(key)
    if val is None:
        return None
    val = val.strip()
    return val or None


@dataclass
class XConfig:
    bearer_token: str | None = None
    api_key: str | None = None
    api_secret: str | None = None
    access_token: str | None = None
    access_secret: str | None = None
    char_limit: int | None = None

    @property
    def is_configured(self) -> bool:
        # OAuth 1.0a user context is required for posting on behalf of a user.
        return all(
            [self.api_key, self.api_secret, self.access_token, self.access_secret]
        )


@dataclass
class MastodonConfig:
    base_url: str | None = None
    access_token: str | None = None

    @property
    def is_configured(self) -> bool:
        return bool(self.base_url and self.access_token)


@dataclass
class BlueskyConfig:
    handle: str | None = None
    app_password: str | None = None
    pds: str = "https://api.bsky.app"

    @property
    def is_configured(self) -> bool:
        return bool(self.handle and self.app_password)


@dataclass
class LinkedInConfig:
    access_token: str | None = None
    author_urn: str | None = None

    @property
    def is_configured(self) -> bool:
        return bool(self.access_token and self.author_urn)


@dataclass
class ThreadsConfig:
    """Meta Threads (graph.threads.net) configuration.

    Threads uses the Meta Graph API. A long-lived user access token
    (valid 60 days, refreshable) and a numeric Threads user ID are
    both required for posting. Get the user ID from the
    ``/me?fields=id`` endpoint using the same token.
    """
    user_id: str | None = None
    access_token: str | None = None

    @property
    def is_configured(self) -> bool:
        return bool(self.user_id and self.access_token)


@dataclass
class SafetyConfig:
    """Top-level toggles for the RiskGuard.

    Per-platform thresholds live in ``autoxpost.core.safety`` and are
    overridden via env (``AUTOXPOST_<UPPER>_<FIELD>``). The fields here
    only configure the global knobs.
    """
    enabled: bool = True
    duplicate_window_hours: int = 24
    hash_history: int = 200


@dataclass
class Config:
    db_path: Path = field(default_factory=lambda: Path("autoxpost.db"))
    log_level: str = "INFO"
    x: XConfig = field(default_factory=XConfig)
    mastodon: MastodonConfig = field(default_factory=MastodonConfig)
    bluesky: BlueskyConfig = field(default_factory=BlueskyConfig)
    linkedin: LinkedInConfig = field(default_factory=LinkedInConfig)
    threads: ThreadsConfig = field(default_factory=ThreadsConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)

    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        if db := _env("AUTOXPOST_DB"):
            cfg.db_path = Path(db).expanduser()
        cfg.log_level = (_env("AUTOXPOST_LOG_LEVEL") or "INFO").upper()

        cfg.x = XConfig(
            bearer_token=_env("X_BEARER_TOKEN"),
            api_key=_env("X_API_KEY"),
            api_secret=_env("X_API_SECRET"),
            access_token=_env("X_ACCESS_TOKEN"),
            access_secret=_env("X_ACCESS_SECRET"),
            char_limit=int(_env("X_CHAR_LIMIT")) if _env("X_CHAR_LIMIT") else None,
        )
        cfg.mastodon = MastodonConfig(
            base_url=_env("MASTODON_BASE_URL"),
            access_token=_env("MASTODON_ACCESS_TOKEN"),
        )
        cfg.bluesky = BlueskyConfig(
            handle=_env("BLUESKY_HANDLE"),
            app_password=_env("BLUESKY_APP_PASSWORD"),
            pds=_env("BLUESKY_PDS") or "https://api.bsky.app",
        )
        cfg.linkedin = LinkedInConfig(
            access_token=_env("LINKEDIN_ACCESS_TOKEN"),
            author_urn=_env("LINKEDIN_AUTHOR_URN"),
        )
        cfg.threads = ThreadsConfig(
            user_id=_env("THREADS_USER_ID"),
            access_token=_env("THREADS_ACCESS_TOKEN"),
        )
        cfg.safety = SafetyConfig(
            enabled=(_env("AUTOXPOST_SAFETY_ENABLED") or "true").lower()
            in ("1", "true", "yes", "on"),
            duplicate_window_hours=int(_env("AUTOXPOST_DUPLICATE_WINDOW_HOURS") or 24),
            hash_history=int(_env("AUTOXPOST_HASH_HISTORY") or 200),
        )
        return cfg

    def enabled_platforms(self) -> dict[str, Any]:
        """Return a name → config mapping for every platform that has creds."""
        out: dict[str, Any] = {}
        if self.x.is_configured:
            out["x"] = self.x
        if self.mastodon.is_configured:
            out["mastodon"] = self.mastodon
        if self.bluesky.is_configured:
            out["bluesky"] = self.bluesky
        if self.linkedin.is_configured:
            out["linkedin"] = self.linkedin
        if self.threads.is_configured:
            out["threads"] = self.threads
        return out

    def configure_logging(self) -> None:
        logging.basicConfig(
            level=getattr(logging, self.log_level, logging.INFO),
            format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        )
