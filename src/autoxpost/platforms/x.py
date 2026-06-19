"""X (Twitter) adapter — uses tweepy with OAuth 1.0a user context."""

from __future__ import annotations

import logging
from pathlib import Path

from autoxpost.config import XConfig
from autoxpost.core.post import Post
from autoxpost.platforms.base import PlatformAdapter, PublishOutcome

log = logging.getLogger(__name__)


class XAdapter(PlatformAdapter):
    name = "x"

    def __init__(self, config: XConfig) -> None:
        super().__init__(config)
        try:
            import tweepy  # type: ignore
        except ImportError as e:
            raise ImportError(
                "tweepy is required for X posting. "
                "Install with: pip install autoxpost[x]"
            ) from e

        self._client = tweepy.Client(
            bearer_token=config.bearer_token,
            consumer_key=config.api_key,
            consumer_secret=config.api_secret,
            access_token=config.access_token,
            access_token_secret=config.access_secret,
        )
        # v1.1 API is still needed for media upload.
        auth = tweepy.OAuth1UserHandler(
            config.api_key, config.api_secret,
            config.access_token, config.access_secret,
        )
        self._v1 = tweepy.API(auth)
        self._char_limit = config.char_limit

    def validate(self, post: Post) -> str | None:
        limit = self._char_limit or 280
        if len(post.text) > limit:
            return f"text is {len(post.text)} chars; X limit is {limit}"
        return None

    def publish(self, post: Post) -> PublishOutcome:
        if err := self.validate(post):
            return PublishOutcome(success=False, error=err)

        media_ids: list[str] = []
        for path in post.media_paths:
            try:
                media = self._v1.media_upload(filename=Path(path).name, file=Path(path).open("rb"))
                media_ids.append(str(media.media_id))
            except Exception as exc:  # noqa: BLE001
                return PublishOutcome(success=False, error=f"media upload failed: {exc}")

        try:
            resp = self._client.create_tweet(text=post.text, media_ids=media_ids or None)
        except Exception as exc:  # noqa: BLE001
            return PublishOutcome(success=False, error=f"{type(exc).__name__}: {exc}")

        tweet_id = str(resp.data["id"]) if resp.data else None
        url = f"https://x.com/i/web/status/{tweet_id}" if tweet_id else None
        return PublishOutcome(success=True, remote_id=tweet_id, remote_url=url)
