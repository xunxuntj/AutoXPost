"""Telegram long-poller runner.

Talks to the Telegram Bot API directly (no `python-telegram-bot`
dependency). On each `run` call, it asks the API for updates newer than
the last processed `update_id`, and for every new message:

1. Parses the text. A prefix like ``/x,mastodon: hello`` targets a
   specific list; anything else is published to all configured
   platforms.
2. Downloads the largest attached photo to a temp file and passes it
   along as media.
3. Publishes the post through the shared `Publisher`.

The caller persists the returned ``next_offset`` (typically by writing
it to a file in the repo) so the same messages are never re-posted.
"""

from __future__ import annotations

import logging
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

from autoxpost.core.publisher import Publisher

log = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
# `/targets: text` → target list. Allowed: a-z, digits, comma.
_TARGET_PREFIX = re.compile(r"^/([a-z0-9,\s]+?):\s*(.*)$", re.DOTALL)
# A long-poll timeout; the API will hold the connection open this long
# waiting for new messages. 0 = short-poll (return immediately).
POLL_TIMEOUT = 25


@dataclass
class TelegramMessage:
    update_id: int
    text: str
    targets: list[str]
    image_path: Path | None
    raw: dict[str, Any] = field(default_factory=dict)


class TelegramRunner:
    def __init__(
        self,
        bot_token: str,
        publisher: Publisher,
        default_targets: list[str] | None = None,
        api_base: str = API_BASE,
        timeout: int = POLL_TIMEOUT,
    ):
        self.bot_token = bot_token
        self.publisher = publisher
        self.default_targets = default_targets or list(publisher.configured_platforms)
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout
        self._session = requests.Session()

    # --- public -------------------------------------------------------------

    def run(self, offset: int = 0) -> int:
        """Process all available updates. Returns the new offset to persist."""
        updates = self._get_updates(offset=offset + 1)
        if not updates:
            return offset
        latest = offset
        for update in updates:
            latest = max(latest, update["update_id"])
            try:
                msg = self._parse_update(update)
            except Exception:  # noqa: BLE001
                log.exception("failed to parse update %s", update.get("update_id"))
                continue
            if msg is None:
                continue
            self._publish(msg)
        return latest

    # --- internals ----------------------------------------------------------

    def _get_updates(self, offset: int) -> list[dict[str, Any]]:
        url = f"{self.api_base}/bot{self.bot_token}/getUpdates"
        try:
            resp = self._session.get(
                url,
                params={"offset": offset, "timeout": self.timeout, "allowed_updates": '["message"]'},
                timeout=self.timeout + 10,
            )
        except requests.RequestException as exc:
            log.error("telegram getUpdates failed: %s", exc)
            return []
        if resp.status_code != 200:
            log.error("telegram getUpdates HTTP %s: %s", resp.status_code, resp.text[:200])
            return []
        body = resp.json()
        if not body.get("ok"):
            log.error("telegram getUpdates returned !ok: %s", body)
            return []
        return body.get("result", [])

    def _parse_update(self, update: dict[str, Any]) -> TelegramMessage | None:
        msg = update.get("message") or update.get("edited_message")
        if not msg:
            return None
        text = (msg.get("text") or msg.get("caption") or "").strip()
        if not text:
            # No text content; nothing to post. Still count the update as
            # processed so we don't loop on it forever.
            return TelegramMessage(
                update_id=update["update_id"], text="", targets=[],
                image_path=None, raw=update,
            )

        # Optional /targets: override.
        if (m := _TARGET_PREFIX.match(text)):
            targets = [t.strip() for t in m.group(1).split(",") if t.strip()]
            text = m.group(2).strip()
        else:
            targets = list(self.default_targets)

        # Photo handling — pick the largest size.
        image_path: Path | None = None
        photos = msg.get("photo") or []
        if photos:
            largest = max(photos, key=lambda p: p.get("file_size", 0))
            image_path = self._download_photo(largest["file_id"])

        return TelegramMessage(
            update_id=update["update_id"],
            text=text,
            targets=targets,
            image_path=image_path,
            raw=update,
        )

    def _download_photo(self, file_id: str) -> Path | None:
        try:
            r = self._session.get(
                f"{self.api_base}/bot{self.bot_token}/getFile",
                params={"file_id": file_id},
                timeout=30,
            )
            r.raise_for_status()
            file_path = r.json()["result"]["file_path"]
        except Exception as exc:  # noqa: BLE001
            log.error("telegram getFile failed for %s: %s", file_id, exc)
            return None
        # Telegram returns paths like "photos/file_12.jpg". URL-encode each
        # segment so a path with spaces or unicode doesn't 404.
        safe_path = "/".join(quote(seg, safe="") for seg in file_path.split("/"))
        download_url = f"{self.api_base}/file/bot{self.bot_token}/{safe_path}"
        try:
            img = self._session.get(download_url, timeout=60)
            img.raise_for_status()
        except requests.RequestException as exc:
            log.error("telegram file download failed: %s", exc)
            return None

        suffix = Path(file_path).suffix or ".jpg"
        tmp = Path(tempfile.mkstemp(suffix=suffix, prefix="tg-")[1])
        tmp.write_bytes(img.content)
        return tmp

    def _publish(self, msg: TelegramMessage) -> None:
        if not msg.text:
            return
        post = Post(
            text=msg.text,
            targets=msg.targets,
            media_paths=[str(msg.image_path)] if msg.image_path else [],
            metadata={"source": "telegram", "update_id": msg.update_id},
        )

        log.info("publishing telegram message (update %s) to %s",
                 msg.update_id, ",".join(post.targets))
        result = self.publisher.publish(post)
        log.info("  ok=%s failed=%d", ",".join(result.succeeded) or "-", len(result.failed))
