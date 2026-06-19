"""Mastodon adapter."""

from __future__ import annotations

import logging
from pathlib import Path

from autoxpost.config import MastodonConfig
from autoxpost.core.post import Post
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
            return PublishOutcome(success=False, error=f"{type(exc).__name__}: {exc}")
        return PublishOutcome(
            success=True,
            remote_id=str(status.id),
            remote_url=status.url,
        )


def _guess_mime(path: Path) -> str:
    """Tiny built-in mime map; mastodon.py accepts None and sniffs, but
    we send an explicit one to avoid 422s on edge formats."""
    import mimetypes

    mime, _ = mimetypes.guess_type(str(path))
    return mime or "application/octet-stream"
