"""Read scheduled posts from a directory of JSON files.

Each file is a self-contained `Post` (see `autoxpost.core.post`). The
runner publishes any post whose `scheduled_at` is in the past and whose
status is not already `PUBLISHED`, then writes the file back with the
updated status and remote URLs so the next run can skip it.

This is the storage model that survives a GitHub Actions runner's
ephemeral filesystem: the posts live in the repo itself.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from autoxpost.core.post import Post, PostStatus
from autoxpost.core.publisher import Publisher

log = logging.getLogger(__name__)


@dataclass
class PredefinedResult:
    published: int = 0
    skipped: int = 0
    failed: int = 0


class PredefinedRunner:
    def __init__(self, posts_dir: str | Path, publisher: Publisher):
        self.posts_dir = Path(posts_dir)
        self.publisher = publisher

    def run(self, now: datetime | None = None) -> PredefinedResult:
        now = now or datetime.utcnow()
        result = PredefinedResult()
        if not self.posts_dir.exists():
            log.info("posts dir %s does not exist; skipping", self.posts_dir)
            return result

        for path in self._iter_posts():
            try:
                post = Post.from_dict(json.loads(path.read_text()))
            except Exception as exc:  # noqa: BLE001
                log.error("could not parse %s: %s", path, exc)
                continue

            if post.status in (PostStatus.PUBLISHED,):
                result.skipped += 1
                continue
            if post.scheduled_at and post.scheduled_at > now:
                result.skipped += 1
                continue

            log.info("publishing predefined post %s (%s)", post.id, path.name)
            outcome = self.publisher.publish(post)
            if outcome.ok:
                result.published += 1
            elif outcome.succeeded:
                result.published += 1
                result.failed += 1
            else:
                result.failed += 1
            _write_back(path, post)
        return result

    def _iter_posts(self):
        """Yield post JSON paths in ``scheduled_at`` order (earliest first).

        Files without ``scheduled_at`` are treated as due now (datetime.max
        would put them last; we want them grouped with the early ones, so
        we sort them with ``datetime.min`` as a tie-breaker).
        """
        def _key(p: Path):
            try:
                post = Post.from_dict(json.loads(p.read_text()))
            except Exception:  # noqa: BLE001
                return (datetime.min, p.name)
            return (post.scheduled_at or datetime.min, p.name)

        return sorted(self.posts_dir.glob("*.json"), key=_key)


def _write_back(path: Path, post: Post) -> None:
    """Persist the post back to disk with its current status/URLs."""
    post.target_results = post.target_results or []
    path.write_text(post.to_json() + "\n")
