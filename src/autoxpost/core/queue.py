"""SQLite-backed post queue."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Iterable

from autoxpost.core.post import Post, PostStatus, PostTarget, TargetStatus
from autoxpost.storage.sqlite import init_db


class PostQueue:
    """Thin wrapper around the SQLite schema in `storage.sqlite`."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.conn = init_db(self.db_path)

    # --- write -------------------------------------------------------------

    def add(self, post: Post) -> Post:
        self.conn.execute(
            """
            INSERT INTO posts
                (id, text, media_paths, targets, status, scheduled_at,
                 created_at, published_at, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                post.id,
                post.text,
                json.dumps(post.media_paths),
                json.dumps(post.targets),
                post.status.value,
                post.scheduled_at.isoformat() if post.scheduled_at else None,
                post.created_at.isoformat(),
                post.published_at.isoformat() if post.published_at else None,
                json.dumps(post.metadata),
            ),
        )
        for t in post.targets:
            self.conn.execute(
                """
                INSERT OR IGNORE INTO post_targets
                    (post_id, platform, status, attempts, updated_at)
                VALUES (?, ?, ?, 0, ?)
                """,
                (post.id, t, TargetStatus.PENDING.value, datetime.utcnow().isoformat()),
            )
        return post

    def update_target(self, post_id: str, target: PostTarget) -> None:
        self.conn.execute(
            """
            UPDATE post_targets
               SET status = ?, remote_id = ?, remote_url = ?,
                   error = ?, attempts = ?, updated_at = ?
             WHERE post_id = ? AND platform = ?
            """,
            (
                target.status.value,
                target.remote_id,
                target.remote_url,
                target.error,
                target.attempts,
                datetime.utcnow().isoformat(),
                post_id,
                target.platform,
            ),
        )

    def update_status(self, post_id: str, status: PostStatus,
                      published_at: datetime | None = None) -> None:
        self.conn.execute(
            "UPDATE posts SET status = ?, published_at = ? WHERE id = ?",
            (status.value, published_at.isoformat() if published_at else None, post_id),
        )

    def log_publish(self, post_id: str, platform: str,
                    success: bool, error: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO publish_log (post_id, platform, success, error, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (post_id, platform, int(success), error, datetime.utcnow().isoformat()),
        )

    # --- read --------------------------------------------------------------

    def get(self, post_id: str) -> Post | None:
        row = self.conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
        if not row:
            return None
        return self._row_to_post(row)

    def list_due(self, now: datetime | None = None) -> list[Post]:
        """Posts whose scheduled time has passed and are still pending/due."""
        now = now or datetime.utcnow()
        rows = self.conn.execute(
            """
            SELECT * FROM posts
             WHERE status IN ('pending', 'due')
               AND (scheduled_at IS NULL OR scheduled_at <= ?)
             ORDER BY COALESCE(scheduled_at, created_at) ASC
            """,
            (now.isoformat(),),
        ).fetchall()
        return [self._row_to_post(r) for r in rows]

    def list_all(self, limit: int = 100) -> list[Post]:
        rows = self.conn.execute(
            "SELECT * FROM posts ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._row_to_post(r) for r in rows]

    def targets_for(self, post_id: str) -> list[PostTarget]:
        rows = self.conn.execute(
            "SELECT * FROM post_targets WHERE post_id = ? ORDER BY platform",
            (post_id,),
        ).fetchall()
        return [PostTarget.from_row(dict(r)) for r in rows]

    # --- internals ---------------------------------------------------------

    def _row_to_post(self, row: sqlite3.Row) -> Post:
        targets = json.loads(row["targets"] or "[]")
        media = json.loads(row["media_paths"] or "[]")
        meta = json.loads(row["metadata"] or "{}")
        post = Post(
            id=row["id"],
            text=row["text"],
            targets=targets,
            media_paths=media,
            status=PostStatus(row["status"]),
            scheduled_at=datetime.fromisoformat(row["scheduled_at"]) if row["scheduled_at"] else None,
            created_at=datetime.fromisoformat(row["created_at"]),
            published_at=datetime.fromisoformat(row["published_at"]) if row["published_at"] else None,
            metadata=meta,
        )
        # Attach persisted target results.
        post.target_results = self.targets_for(post.id)
        return post

    def close(self) -> None:
        self.conn.close()
