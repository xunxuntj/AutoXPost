"""Threads (Meta) adapter.

Posts to the Threads Graph API using a long-lived user access token.
The Threads API uses a two-step flow: create a media container, then
publish it. See https://developers.facebook.com/docs/threads/overview
for the protocol details.

Image posts require a publicly accessible HTTPS URL — Threads does
not accept direct file uploads. If a post has local ``media_paths``
but no ``metadata["threads_image_url"]``, the adapter returns a clear
``PublishOutcome(success=False, ...)`` so the user knows what to set.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlencode

import requests

from autoxpost.config import ThreadsConfig
from autoxpost.core.post import Post
from autoxpost.core.safety import RateLimited, RateLimitSignal
from autoxpost.platforms.base import PlatformAdapter, PublishOutcome

log = logging.getLogger(__name__)

API_BASE = "https://graph.threads.net/v1.0"
DEFAULT_TIMEOUT = 30


class ThreadsAdapter(PlatformAdapter):
    name = "threads"
    char_limit = 500

    def __init__(self, config: ThreadsConfig):
        super().__init__(config)
        self._user_id = config.user_id
        self._access_token = config.access_token
        self._session = requests.Session()

    # --- validation ---------------------------------------------------------

    def validate(self, post: Post) -> str | None:
        if len(post.text) > self.char_limit:
            return f"text is {len(post.text)} chars; Threads limit is {self.char_limit}"
        if post.media_paths and not post.metadata.get("threads_image_url"):
            return (
                "Threads requires a public image URL; "
                "set metadata.threads_image_url to a publicly accessible HTTPS URL"
            )
        return None

    # --- public -------------------------------------------------------------

    def publish(self, post: Post) -> PublishOutcome:
        if err := self.validate(post):
            return PublishOutcome(success=False, error=err)

        try:
            container_id = self._create_container(post)
            post_id = self._publish_container(container_id)
        except RateLimited:
            # Already shaped correctly; let the publisher's guard catch it.
            raise
        except requests.RequestException as exc:
            return PublishOutcome(success=False, error=f"network: {exc}")
        except _ThreadsAPIError as exc:
            return PublishOutcome(success=False, error=str(exc))

        url = f"https://www.threads.net/@{self._user_id}/post/{post_id}"
        return PublishOutcome(success=True, remote_id=post_id, remote_url=url)

    # --- internals ----------------------------------------------------------

    def _create_container(self, post: Post) -> str:
        image_url = post.metadata.get("threads_image_url")
        body: dict[str, str] = {
            "media_type": "IMAGE" if image_url else "TEXT",
            "text": post.text,
            "access_token": self._access_token or "",
        }
        if image_url:
            body["image_url"] = str(image_url)
        url = f"{API_BASE}/{self._user_id}/threads"
        resp = self._session.post(url, data=body, timeout=DEFAULT_TIMEOUT)
        payload = self._check_rate_limit(resp)
        container_id = payload.get("id") if isinstance(payload, dict) else None
        if not container_id:
            raise _ThreadsAPIError(
                f"container create returned no id (status {resp.status_code}): "
                f"{resp.text[:200]}"
            )
        return str(container_id)

    def _publish_container(self, container_id: str) -> str:
        url = f"{API_BASE}/{self._user_id}/threads_publish"
        body = {
            "creation_id": container_id,
            "access_token": self._access_token or "",
        }
        resp = self._session.post(url, data=body, timeout=DEFAULT_TIMEOUT)
        payload = self._check_rate_limit(resp)
        post_id = payload.get("id") if isinstance(payload, dict) else None
        if not post_id:
            raise _ThreadsAPIError(
                f"threads_publish returned no id (status {resp.status_code}): "
                f"{resp.text[:200]}"
            )
        return str(post_id)

    def _check_rate_limit(self, resp: requests.Response) -> dict[str, Any]:
        """Inspect the response, raise RateLimited on 429, parse JSON otherwise."""
        if resp.status_code == 429:
            retry = resp.headers.get("Retry-After") or resp.headers.get("retry-after")
            raise RateLimited(
                RateLimitSignal(
                    retry_after_seconds=float(retry) if retry else None,
                    reset_at=None,
                    reason="threads: 429",
                )
            )
        if resp.status_code >= 300:
            raise _ThreadsAPIError(
                f"HTTP {resp.status_code}: {resp.text[:500]}"
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise _ThreadsAPIError(
                f"non-JSON response (status {resp.status_code}): {resp.text[:200]}"
            ) from exc


class _ThreadsAPIError(Exception):
    """Recoverable non-rate-limit API failure from the Threads API."""
