"""X (Twitter) adapter — uses tweepy with OAuth 1.0a user context."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from autoxpost.config import XConfig
from autoxpost.core.post import Post
from autoxpost.core.safety import RateLimited, RateLimitSignal
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
            raise self._coerce_rate_limit(exc) from exc

        tweet_id = str(resp.data["id"]) if resp.data else None
        url = f"https://x.com/i/web/status/{tweet_id}" if tweet_id else None
        return PublishOutcome(success=True, remote_id=tweet_id, remote_url=url)

    # --- helpers ------------------------------------------------------------

    @staticmethod
    def _coerce_rate_limit(exc: BaseException) -> BaseException:
        """Translate X / Twitter rate-limit responses into RateLimited.

        tweepy's ``TooManyRequests`` exposes ``response.headers`` with the
        standard ``x-rate-limit-reset`` (epoch seconds) and ``retry-after``.
        For other tweepy errors we fall through; the publisher will still
        record the failure as a generic failure for the kill switch.
        """
        try:
            import tweepy  # type: ignore
        except ImportError:
            return exc
        if not isinstance(exc, tweepy.errors.TooManyRequests):
            return exc
        headers = getattr(getattr(exc, "response", None), "headers", None) or {}
        retry_after = headers.get("retry-after")
        reset = headers.get("x-rate-limit-reset")
        signal = RateLimitSignal(
            retry_after_seconds=float(retry_after) if retry_after else None,
            reset_at=(
                datetime.utcfromtimestamp(int(reset)) if reset else None
            ),
            reason=f"x: 429 ({getattr(exc, 'api_codes', lambda: [])() or ''})".strip(),
        )
        return RateLimited(signal, original=exc)
