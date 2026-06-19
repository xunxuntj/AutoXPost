"""Platform adapters — one per social network.

Each adapter lives in its own module and is registered by name. The
`build_adapters` factory below wires up the configured platforms using
the credentials from `Config`.
"""

from __future__ import annotations

import logging
from typing import Any

from autoxpost.platforms.base import PlatformAdapter, PublishOutcome

log = logging.getLogger(__name__)


def build_adapters(config: Any) -> dict[str, PlatformAdapter]:
    """Instantiate every adapter whose config is fully populated."""
    from autoxpost.config import Config  # local import to avoid cycles

    assert isinstance(config, Config)
    adapters: dict[str, PlatformAdapter] = {}

    if config.x.is_configured:
        try:
            from autoxpost.platforms.x import XAdapter

            adapters["x"] = XAdapter(config.x)
        except ImportError:
            log.warning("X configured but tweepy is not installed; "
                        "run `pip install autoxpost[x]` to enable X posting.")

    if config.mastodon.is_configured:
        try:
            from autoxpost.platforms.mastodon import MastodonAdapter

            adapters["mastodon"] = MastodonAdapter(config.mastodon)
        except ImportError:
            log.warning("Mastodon configured but mastodon-py is not installed; "
                        "run `pip install autoxpost[mastodon]` to enable it.")

    if config.bluesky.is_configured:
        try:
            from autoxpost.platforms.bluesky import BlueskyAdapter

            adapters["bluesky"] = BlueskyAdapter(config.bluesky)
        except ImportError:
            log.warning("Bluesky configured but atproto is not installed; "
                        "run `pip install autoxpost[bluesky]` to enable it.")

    if config.linkedin.is_configured:
        from autoxpost.platforms.linkedin import LinkedInAdapter

        adapters["linkedin"] = LinkedInAdapter(config.linkedin)

    return adapters


__all__ = ["PlatformAdapter", "PublishOutcome", "build_adapters"]
