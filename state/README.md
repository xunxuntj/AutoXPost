# `state/` — runtime state files

The GitHub Action writes small state files here so it can be stateless
between runs.

| File | Purpose |
| --- | --- |
| `telegram_offset.txt` | The `update_id` of the last Telegram message the action processed. Written after a successful poll so the same messages are never re-posted. |

These files are committed back to the repo by the workflow after each
run. Treat them as machine-managed — feel free to delete one to force a
re-poll, but be aware that doing so will re-publish any unprocessed
messages.
