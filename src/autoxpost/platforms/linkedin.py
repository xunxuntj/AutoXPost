"""LinkedIn adapter — posts to the user's UGC posts endpoint.

Uses LinkedIn's REST API directly (no third-party SDK). Requires an
OAuth2 access token with the `w_member_social` scope and the user's
author URN (`urn:li:person:...`).
"""

from __future__ import annotations

import logging
from pathlib import Path

import requests

from autoxpost.config import LinkedInConfig
from autoxpost.core.post import Post
from autoxpost.platforms.base import PlatformAdapter, PublishOutcome

log = logging.getLogger(__name__)

API_BASE = "https://api.linkedin.com/v2"
UPLOAD_BASE = "https://api.linkedin.com/v2/assets"
VERSION = "202401"


class LinkedInAdapter(PlatformAdapter):
    name = "linkedin"
    char_limit = 3000

    def __init__(self, config: LinkedInConfig) -> None:
        super().__init__(config)
        self._headers = {
            "Authorization": f"Bearer {config.access_token}",
            "X-Restli-Protocol-Version": "2.0.0",
            "LinkedIn-Version": VERSION,
        }
        self._author = config.author_urn

    def validate(self, post: Post) -> str | None:
        if len(post.text) > self.char_limit:
            return f"text is {len(post.text)} chars; LinkedIn limit is {self.char_limit}"
        return None

    def publish(self, post: Post) -> PublishOutcome:
        if err := self.validate(post):
            return PublishOutcome(success=False, error=err)

        # Media is uploaded as a separate "asset" then referenced by URN.
        media_urns: list[str] = []
        for path in post.media_paths:
            urn = self._upload_asset(path)
            if urn is None:
                return PublishOutcome(success=False, error=f"failed to upload {path}")
            media_urns.append(urn)

        body: dict = {
            "author": self._author,
            "lifecycleState": "PUBLISHED",
            "specificContent": {
                "com.linkedin.ugc.ShareContent": {
                    "shareCommentary": {"text": post.text},
                    "shareMediaCategory": "IMAGE" if media_urns else "NONE",
                }
            },
            "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"},
        }
        if media_urns:
            body["specificContent"]["com.linkedin.ugc.ShareContent"]["media"] = [
                {"status": "READY", "media": urn} for urn in media_urns
            ]

        try:
            resp = requests.post(f"{API_BASE}/ugcPosts", json=body, headers=self._headers, timeout=30)
        except requests.RequestException as exc:
            return PublishOutcome(success=False, error=f"network: {exc}")

        if resp.status_code >= 300:
            return PublishOutcome(success=False, error=f"HTTP {resp.status_code}: {resp.text[:500]}")

        post_id = resp.headers.get("x-restli-id") or resp.json().get("id")
        url = f"https://www.linkedin.com/feed/update/{post_id}" if post_id else None
        return PublishOutcome(success=True, remote_id=post_id, remote_url=url)

    # --- helpers ------------------------------------------------------------

    def _upload_asset(self, path: str) -> str | None:
        register = {
            "registerUploadRequest": {
                "recipes": ["urn:li:digitalmediaRecipe:feedshare-image"],
                "owner": self._author,
                "serviceRelationships": [
                    {
                        "relationshipType": "OWNER",
                        "identifier": "urn:li:userGeneratedContent",
                    }
                ],
            }
        }
        r = requests.post(UPLOAD_BASE, json=register, headers=self._headers, timeout=30)
        if r.status_code >= 300:
            log.error("LinkedIn register upload failed: %s", r.text)
            return None
        upload_url = r.json().get("uploadMechanism", {}).get("com.linkedin.digitalmedia.uploading.MediaUploadHttpRequest", {}).get("uploadUrl")
        asset = r.json().get("value", {}).get("asset")
        if not upload_url or not asset:
            return None
        with Path(path).open("rb") as f:
            put = requests.put(
                upload_url,
                data=f,
                headers={**self._headers, "Content-Type": "application/octet-stream"},
                timeout=120,
            )
        return asset if put.status_code < 300 else None
