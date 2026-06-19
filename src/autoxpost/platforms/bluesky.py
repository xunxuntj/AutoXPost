"""Bluesky adapter (atproto)."""

from __future__ import annotations

import logging
from pathlib import Path

from autoxpost.config import BlueskyConfig
from autoxpost.core.post import Post
from autoxpost.platforms.base import PlatformAdapter, PublishOutcome

log = logging.getLogger(__name__)


class BlueskyAdapter(PlatformAdapter):
    name = "bluesky"
    # Grapheme limit; atproto enforces this in the client, not the server.
    char_limit = 300

    def __init__(self, config: BlueskyConfig) -> None:
        super().__init__(config)
        try:
            from atproto import Client  # type: ignore
        except ImportError as e:
            raise ImportError(
                "atproto is required. "
                "Install with: pip install autoxpost[bluesky]"
            ) from e
        self._client = Client(base_url=config.pds)
        self._client.login(config.handle, config.app_password)

    def validate(self, post: Post) -> str | None:
        # atproto measures graphemes, not code points; a rough code-point
        # check is a useful guardrail even if not exact.
        if len(post.text) > self.char_limit:
            return f"text is {len(post.text)} code points; Bluesky limit is {self.char_limit}"
        return None

    def publish(self, post: Post) -> PublishOutcome:
        if err := self.validate(post):
            return PublishOutcome(success=False, error=err)

        embed = None
        if post.media_paths:
            try:
                from atproto import models  # type: ignore
            except ImportError:
                return PublishOutcome(success=False, error="atproto.models not available")

            images = []
            for path in post.media_paths[:4]:  # atproto max 4 images per post
                data = Path(path).read_bytes()
                upload = self._client.upload_blob(data)
                images.append(models.AppBskyEmbedImages.Image(
                    alt="",  # user can override via metadata if needed
                    image=upload.blob,
                ))
            if images:
                embed = models.AppBskyEmbedImages.Main(images=images)

        try:
            from atproto import models  # type: ignore
            self._client.com.atproto.repo.create_record(
                models.ComAtprotoRepoCreateRecord.Data(
                    repo=self._client.me.did,
                    collection="app.bsky.feed.post",
                    record=models.AppBskyFeedPost.Main(
                        text=post.text,
                        embed=embed,
                        created_at=_now_iso(),
                    ),
                )
            )
        except Exception as exc:  # noqa: BLE001
            return PublishOutcome(success=False, error=f"{type(exc).__name__}: {exc}")

        did = self._client.me.did
        handle = self._client.me.handle
        return PublishOutcome(
            success=True,
            remote_id=did,
            remote_url=f"https://bsky.app/profile/{handle}",
        )


def _now_iso() -> str:
    from datetime import datetime
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
