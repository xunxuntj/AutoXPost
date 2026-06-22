"""Mastodon adapter."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from autoxpost.config import MastodonConfig
from autoxpost.core.post import Post
from autoxpost.core.safety import RateLimited, RateLimitSignal
from autoxpost.platforms.base import PlatformAdapter, PublishOutcome

log = logging.getLogger(__name__)


class MastodonAdapter(PlatformAdapter):
    name = "mastodon"

    def __init__(self, config: MastodonConfig) -> None:
        super().__init__(config)
        try:
            from mastodon import Mastodon  # type: ignore
        except ImportError as e:
            raise ImportError(
                "mastodon-py is required. "
                "Install with: pip install autoxpost[mastodon]"
            ) from e
        self._client = Mastodon(
            access_token=config.access_token,
            api_base_url=config.base_url,
        )

    def publish(self, post: Post) -> PublishOutcome:
        media_ids: list = []
        for path in post.media_paths:
            try:
                m = self._client.media_post(Path(path).read_bytes(), mime_type=_guess_mime(path))
                media_ids.append(m.id)
            except Exception as exc:  # noqa: BLE001
                return PublishOutcome(success=False, error=f"media upload failed: {exc}")

        try:
            status = self._client.status_post(post.text, media_ids=media_ids or None)
        except Exception as exc:  # noqa: BLE001
            raise self._coerce_rate_limit(exc) from exc
        return PublishOutcome(
            success=True,
            remote_id=str(status.id),
            remote_url=status.url,
        )

    # --- helpers ------------------------------------------------------------

    @staticmethod
    def _coerce_rate_limit(exc: BaseException) -> BaseException:
        """Translate Mastodon 429 responses into RateLimited.

        mastodon-py (>= 1.8) raises ``MastodonRateLimitError`` and exposes
        ``.retry_after`` (seconds). Older versions raise a generic
        ``MastodonAPIError`` whose ``.response`` carries status/headers.
        """
        try:
            from mastodon import MastodonAPIError, MastodonRateLimitError  # type: ignore
        except ImportError:
            return exc
        if isinstance(exc, MastodonRateLimitError):
            retry = getattr(exc, "retry_after", None)
            return RateLimited(
                RateLimitSignal(
                    retry_after_seconds=float(retry) if retry else None,
                    reset_at=None,
                    reason="mastodon: rate limit",
                ),
                original=exc,
            )
        if isinstance(exc, MastodonAPIError):
            response = getattr(exc, "response", None)
            if response is not None and getattr(response, "status_code", None) == 429:
                headers = getattr(response, "headers", None) or {}
                retry = headers.get("Retry-After") or headers.get("retry-after")
                reset = headers.get("X-RateLimit-Reset") or headers.get("x-ratelimit-reset")
                reset_at: datetime | None = None
                if reset:
                    try:
                        reset_at = datetime.fromtimestamp(int(reset), tz=timezone.utc)
                    except (TypeError, ValueError):
                        reset_at = None
                return RateLimited(
                    RateLimitSignal(
                        retry_after_seconds=float(retry) if retry else None,
                        reset_at=reset_at,
                        reason="mastodon: 429",
                    ),
                    original=exc,
                )
        return exc


def _guess_mime(path: Path) -> str:
    """Tiny built-in mime map; mastodon.py accepts None and sniffs, but
    we send an explicit one to avoid 422s on edge formats."""
    import mimetypes

    mime, _ = mimetypes.guess_type(str(path))
    return mime or "application/octet-stream"
