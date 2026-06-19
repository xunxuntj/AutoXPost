# AutoXPost

Cross-platform social poster. Write once, publish to X (Twitter), Mastodon,
Bluesky, and LinkedIn from a single queue.

## Features

- **One queue, many platforms** — store a post once, publish it to whichever
  targets you enable.
- **Per-platform adapters** — each network has its own adapter that handles
  auth, character limits, and media uploads.
- **SQLite-backed queue** — durable, single-file, no extra services.
- **APScheduler-based scheduler** — schedule posts in the future or run on
  a recurring cron.
- **Pluggable** — adding a new platform is one class implementing
  `PlatformAdapter`.

## Quick start

```bash
# 1. install
cd AutoXPost
python -m venv .venv && source .venv/bin/activate
pip install -e .

# 2. configure
cp .env.example .env
# edit .env with your API tokens for each platform

# 3. enqueue a post
autoxpost add "Hello from AutoXPost!" --targets x,mastodon --at "2026-06-20 09:00"

# 4. run the scheduler (publishes due posts)
autoxpost run
```

## CLI

```
autoxpost add    "TEXT" [--targets x,mastodon,bluesky,linkedin] [--at ISO] [--media PATH]...
autoxpost list                       # show queued posts
autoxpost run                        # start the scheduler (foreground)
autoxpost publish <id>               # publish a single post immediately
autoxpost platforms                  # show configured platforms and their status
```

## Architecture

```
src/autoxpost/
├── cli.py                # Click-based CLI entry point
├── config.py             # .env + environment loading
├── core/
│   ├── post.py           # Post dataclass + status enum
│   ├── queue.py          # SQLite-backed post queue
│   ├── publisher.py      # Orchestrates "publish this post to that platform"
│   └── scheduler.py      # APScheduler wiring
├── platforms/
│   ├── base.py           # PlatformAdapter ABC
│   ├── x.py              # X / Twitter (twitter-api-v2 / tweepy)
│   ├── mastodon.py       # Mastodon (mastodon.py)
│   ├── bluesky.py        # Bluesky (atproto)
│   └── linkedin.py       # LinkedIn (REST API)
└── storage/
    └── sqlite.py         # Schema + helpers
```

## Configuration

All config is read from environment variables (or a `.env` file). See
`.env.example` for the full list. Each platform's tokens are optional —
only the platforms you configure will be used.

| Variable | Platform | Purpose |
| --- | --- | --- |
| `X_BEARER_TOKEN`, `X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`, `X_ACCESS_SECRET` | X | OAuth 1.0a user context |
| `MASTODON_BASE_URL`, `MASTODON_ACCESS_TOKEN` | Mastodon | App password or OAuth token |
| `BLUESKY_HANDLE`, `BLUESKY_APP_PASSWORD` | Bluesky | App password |
| `LINKEDIN_ACCESS_TOKEN`, `LINKEDIN_AUTHOR_URN` | LinkedIn | OAuth2 user token + `urn:li:person:...` |
| `AUTOXPOST_DB` | — | SQLite path (default `./autoxpost.db`) |
| `AUTOXPOST_LOG_LEVEL` | — | `DEBUG` / `INFO` / `WARNING` (default `INFO`) |

## Development

```bash
pip install -e ".[dev]"
pytest
```

## License

MIT
