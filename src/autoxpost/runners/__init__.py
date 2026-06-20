"""Pre-built runners for the two scheduled flows:

- `predefined`: posts authored as JSON files in a directory, published when
  their `scheduled_at` arrives.
- `telegram`: a long-poll that reads new messages from a Telegram bot,
  downloads attached images, and publishes them.

Both are used by the bundled `autoxpost tick` command and by the GitHub
Actions workflow under `.github/workflows/post.yml`.
"""

from autoxpost.runners.predefined import PredefinedRunner, PredefinedResult
from autoxpost.runners.telegram import TelegramRunner, TelegramMessage

__all__ = [
    "PredefinedRunner",
    "PredefinedResult",
    "TelegramRunner",
    "TelegramMessage",
]
