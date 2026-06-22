"""CLI entry point — `autoxpost` script."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

import click

from autoxpost import __version__
from autoxpost.config import Config
from autoxpost.core.post import Post
from autoxpost.core.publisher import Publisher
from autoxpost.core.queue import PostQueue
from autoxpost.core.safety import QueueRiskStore, build_guard
from autoxpost.core.scheduler import PostScheduler
from autoxpost.platforms import build_adapters
from autoxpost.runners.predefined import PredefinedRunner
from autoxpost.runners.telegram import TelegramRunner


def _load_config() -> Config:
    cfg = Config.load()
    cfg.configure_logging()
    return cfg


def _build_publisher(cfg: Config) -> tuple[PostQueue, Publisher]:
    queue = PostQueue(cfg.db_path)
    adapters = build_adapters(cfg)
    store = QueueRiskStore(queue.conn)
    guard = build_guard(
        enabled=cfg.safety.enabled,
        store=store,
        platforms=list(adapters.keys()),
    )
    return queue, Publisher(queue, adapters, guard=guard)


@click.group()
@click.version_option(__version__, prog_name="autoxpost")
def main() -> None:
    """AutoXPost — queue once, publish to X, Mastodon, Bluesky, LinkedIn."""


@main.command()
@click.argument("text")
@click.option("--targets", "-t", required=True,
              help="Comma-separated platforms, e.g. 'x,mastodon,bluesky,linkedin'.")
@click.option("--at", "scheduled_at", default=None,
              help="ISO-8601 datetime (UTC) to publish at. Omit for immediate.")
@click.option("--media", "-m", multiple=True, type=click.Path(exists=True, dir_okay=False),
              help="Path to a media file. Repeat for multiple.")
@click.option("--metadata", "metadata_json", default=None,
              help="Optional JSON object of free-form metadata to attach.")
def add(text: str, targets: str, scheduled_at: str | None,
        media: tuple[str, ...], metadata_json: str | None) -> None:
    """Enqueue a new post."""
    cfg = _load_config()
    queue, publisher = _build_publisher(cfg)

    target_list = [t.strip() for t in targets.split(",") if t.strip()]
    unknown = [t for t in target_list if t not in publisher.adapters]
    if unknown:
        click.echo(
            f"warning: no adapter for {unknown}; configure env vars or remove from --targets",
            err=True,
        )

    when = datetime.fromisoformat(scheduled_at) if scheduled_at else None
    meta = json.loads(metadata_json) if metadata_json else {}
    post = Post(
        text=text,
        targets=target_list,
        media_paths=list(media),
        scheduled_at=when,
        metadata=meta,
    )
    queue.add(post)
    click.echo(f"queued {post.id} → {','.join(target_list)}"
               + (f" at {when.isoformat()}" if when else " (immediate)"))
    queue.close()


@main.command(name="list")
@click.option("--limit", default=20, show_default=True)
def list_cmd(limit: int) -> None:
    """Show recent queued posts."""
    cfg = _load_config()
    queue, _ = _build_publisher(cfg)
    posts = queue.list_all(limit=limit)
    if not posts:
        click.echo("(no posts)")
        return
    for p in posts:
        when = p.scheduled_at.isoformat() if p.scheduled_at else "—"
        click.echo(f"{p.id}  {p.status.value:10s}  {when}  "
                   f"→ {','.join(p.targets)}  {p.text[:60]!r}")
    queue.close()


@main.command()
@click.option("--interval", default=30, show_default=True,
              help="Seconds between queue polls.")
def run(interval: int) -> None:
    """Start the scheduler. Publishes due posts in the foreground."""
    cfg = _load_config()
    queue, publisher = _build_publisher(cfg)
    if not publisher.adapters:
        click.echo("error: no platforms configured. Fill in .env and retry.", err=True)
        sys.exit(2)
    scheduler = PostScheduler(queue, publisher, poll_interval_seconds=interval)
    scheduler.run()
    queue.close()


@main.command()
@click.argument("post_id")
def publish(post_id: str) -> None:
    """Publish a single post immediately, ignoring its schedule."""
    cfg = _load_config()
    queue, publisher = _build_publisher(cfg)
    post = queue.get(post_id)
    if not post:
        click.echo(f"error: post {post_id} not found", err=True)
        sys.exit(1)
    result = publisher.publish(post)
    click.echo(f"succeeded: {result.succeeded}")
    if result.failed:
        click.echo("failed:")
        for platform, err in result.failed:
            click.echo(f"  {platform}: {err}")
        sys.exit(1)
    queue.close()


@main.command()
def platforms() -> None:
    """Show which platforms are configured and ready."""
    cfg = _load_config()
    enabled = cfg.enabled_platforms()
    click.echo(f"database: {cfg.db_path}")
    if not enabled:
        click.echo("no platforms configured — see .env.example")
        return
    for name in sorted(enabled):
        click.echo(f"  ✓ {name}")
    # Also show which platforms were *requested* by env but missing SDKs
    # so the user can see extras to install.
    for platform, optional in [
        ("x", "autoxpost[x]"),
        ("mastodon", "autoxpost[mastodon]"),
        ("bluesky", "autoxpost[bluesky]"),
    ]:
        creds_present = {
            "x": bool(cfg.x.api_key and cfg.x.access_token),
            "mastodon": bool(cfg.mastodon.access_token),
            "bluesky": bool(cfg.bluesky.app_password),
        }.get(platform, False)
        if creds_present and platform not in enabled:
            click.echo(f"  ✗ {platform} (creds present, SDK missing — pip install {optional})")


@main.command()
@click.option("--posts-dir", default="posts", show_default=True,
              help="Directory of predefined post JSON files.")
@click.option("--telegram/--no-telegram", default=True,
              help="Poll the Telegram bot for new messages.")
@click.option("--telegram-offset-file", default="state/telegram_offset.txt",
              show_default=True,
              help="File storing the last processed Telegram update_id.")
@click.option("--telegram-bot-token-env", default="TELEGRAM_BOT_TOKEN",
              show_default=True,
              help="Env var name holding the Telegram bot token.")
@click.option("--default-targets", default=None,
              help="Comma-separated targets for Telegram posts. "
                   "Defaults to every configured platform.")
def tick(
    posts_dir: str,
    telegram: bool,
    telegram_offset_file: str,
    telegram_bot_token_env: str,
    default_targets: str | None,
) -> None:
    """One shot: publish due predefined posts and any new Telegram messages.

    Designed to be called from a cron (e.g. GitHub Actions). Use `--no-telegram`
    to disable the Telegram half and only run the predefined-post flow.
    """
    cfg = _load_config()
    queue, publisher = _build_publisher(cfg)
    if not publisher.adapters:
        click.echo("error: no platforms configured. Set env vars first.", err=True)
        sys.exit(2)

    # 1. Predefined posts.
    pre = PredefinedRunner(posts_dir=posts_dir, publisher=publisher)
    pre_result = pre.run()
    click.echo(
        f"predefined: published={pre_result.published} "
        f"skipped={pre_result.skipped} failed={pre_result.failed}"
    )

    # 2. Telegram.
    if telegram:
        token = os.environ.get(telegram_bot_token_env)
        if not token:
            click.echo(f"telegram: {telegram_bot_token_env} not set; skipping", err=True)
        else:
            offset_path = Path(telegram_offset_file)
            offset = _read_offset(offset_path)
            tg = TelegramRunner(
                bot_token=token,
                publisher=publisher,
                default_targets=(
                    [t.strip() for t in default_targets.split(",") if t.strip()]
                    if default_targets
                    else None
                ),
            )
            new_offset = tg.run(offset=offset)
            if new_offset != offset:
                _write_offset(offset_path, new_offset)
                click.echo(f"telegram: advanced offset to {new_offset}")
            else:
                click.echo("telegram: no new messages")

    queue.close()


def _read_offset(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        return int(path.read_text().strip() or "0")
    except ValueError:
        return 0


def _write_offset(path: Path, value: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{value}\n")


if __name__ == "__main__":
    main()
