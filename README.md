# AutoXPost

Cross-platform social poster. Write once, publish to X (Twitter), Mastodon,
Bluesky, LinkedIn, and Threads from a single queue — locally, on a server,
or via a daily GitHub Action that pulls from `posts/*.json` and a Telegram
bot.

## Features

- **One queue, many platforms** — store a post once, publish it to whichever
  targets you enable.
- **Per-platform adapters** — each network has its own adapter that handles
  auth, character limits, and media uploads.
- **Two ingestion paths** —
  - **Predefined posts** are JSON files in `posts/` (great for scheduled
    campaigns).
  - **Ad hoc posts** arrive via a Telegram bot (great for "I just thought of
    something" from your phone, with a photo).
- **GitHub Actions–ready** — bundled workflow runs both flows three times a
  day and commits the results back, so the runner's ephemeral filesystem
  doesn't matter.
- **Pluggable** — adding a new platform is one class implementing
  `PlatformAdapter`.

---

## Quick start (local)

```bash
# 1. install
cd AutoXPost
python -m venv .venv && source .venv/bin/activate
pip install -e ".[x,mastodon,bluesky]"   # pick the platforms you use

# 2. configure
cp .env.example .env
# edit .env with your API tokens for each platform

# 3. enqueue a post
autoxpost add "Hello from AutoXPost!" --targets x,mastodon --at "2026-06-20 09:00"

# 4a. run the long-lived scheduler
autoxpost run

# 4b. OR run a one-shot tick (publishes due posts + new Telegram messages)
autoxpost tick
```

## Quick start (GitHub Actions)

The workflow at `.github/workflows/post.yml` runs at **8am, 1pm, and 9pm
US Eastern**, publishes any due predefined posts, polls the Telegram
bot for new messages, and commits the results back to the repo.

To enable it:

1. **Create a Telegram bot** (one-time, ~2 minutes):
   - In Telegram, message [@BotFather](https://t.me/BotFather).
   - Send `/newbot`, follow the prompts, copy the **bot token**.
   - In your bot's chat, send `/start` to initialize the conversation.

2. **Add secrets** in your GitHub repo under
   *Settings → Secrets and variables → Actions → Secrets*:

   | Secret | Where to get it |
   | --- | --- |
   | `TELEGRAM_BOT_TOKEN` | The token from @BotFather |
   | `X_BEARER_TOKEN`, `X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`, `X_ACCESS_SECRET` | https://developer.twitter.com → your app → Keys & Tokens |
   | `MASTODON_BASE_URL`, `MASTODON_ACCESS_TOKEN` | Mastodon → Preferences → Development → New Application |
   | `BLUESKY_HANDLE`, `BLUESKY_APP_PASSWORD` | https://bsky.app/settings/app-passwords |
   | `LINKEDIN_ACCESS_TOKEN`, `LINKEDIN_AUTHOR_URN` | LinkedIn OAuth2 with `w_member_social`; URN from `/v2/userinfo` |

   Only the platforms you actually use need secrets. The rest are
   silently skipped.

3. **(Optional) Add variables** under *Settings → Secrets and variables →
   Actions → Variables*:

   | Variable | Default | Purpose |
   | --- | --- | --- |
   | `AUTOXPOST_LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
   | `BLUESKY_PDS` | `https://api.bsky.app` | Custom PDS URL |
   | `AUTOXPOST_DEFAULT_TARGETS` | (all configured) | Comma-separated default for Telegram posts, e.g. `x,bluesky` |

4. **Author a post** — drop a file into `posts/`. The example file at
   `posts/example-launch.json` shows the schema; the date in the example
   is in the year 2099 so it won't fire by accident. To post something,
   change `scheduled_at` to a real time and `status` to `pending`, then
   commit and push.

5. **Send a Telegram message** — open your bot's chat, type anything,
   optionally attach a photo, send. The next scheduled run (or a manual
   trigger) picks it up and posts it. To target specific platforms, prefix
   the message: `/x,bluesky: hello from the phone!`.

6. **Trigger a run manually** for testing — *Actions → post → Run
   workflow*.

See `posts/README.md` and `state/README.md` for the on-disk formats.

---

## CLI

```
autoxpost add     "TEXT" [--targets x,mastodon,bluesky,linkedin] [--at ISO] [--media PATH]...
autoxpost list                        # show queued posts
autoxpost run                         # start the scheduler (foreground, long-lived)
autoxpost tick                        # one-shot: due posts + new Telegram messages
autoxpost publish <id>                # publish a single post immediately
autoxpost platforms                   # show configured platforms and their status
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
│   └── scheduler.py      # APScheduler wiring (long-lived mode)
├── runners/
│   ├── predefined.py     # Reads posts/*.json, publishes due ones
│   └── telegram.py       # Long-polls Telegram, publishes new messages
├── platforms/
│   ├── base.py           # PlatformAdapter ABC
│   ├── x.py              # X / Twitter (tweepy)
│   ├── mastodon.py       # Mastodon (mastodon-py)
│   ├── bluesky.py        # Bluesky (atproto)
│   └── linkedin.py       # LinkedIn (REST API)
└── storage/
    └── sqlite.py         # Schema + helpers
```

The **CLI**, the **local scheduler**, the **GitHub Action**, and any
custom automation all share the same `Publisher`. The runners just
provide different sources of `Post` objects.

## Configuration reference

All config is read from environment variables (or a `.env` file). See
`.env.example` for the full list. Each platform's tokens are optional —
only the platforms you configure will be used.

| Variable | Platform | Purpose |
| --- | --- | --- |
| `X_BEARER_TOKEN`, `X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`, `X_ACCESS_SECRET` | X | OAuth 1.0a user context |
| `MASTODON_BASE_URL`, `MASTODON_ACCESS_TOKEN` | Mastodon | App password or OAuth token |
| `BLUESKY_HANDLE`, `BLUESKY_APP_PASSWORD` | Bluesky | App password |
| `LINKEDIN_ACCESS_TOKEN`, `LINKEDIN_AUTHOR_URN` | LinkedIn | OAuth2 user token + `urn:li:person:...` |
| `THREADS_USER_ID`, `THREADS_ACCESS_TOKEN` | Threads | Long-lived OAuth2 token (60d) + numeric Threads user ID |
| `TELEGRAM_BOT_TOKEN` | Telegram | Bot token from @BotFather |
| `AUTOXPOST_DB` | — | SQLite path (default `./autoxpost.db`) |
| `AUTOXPOST_LOG_LEVEL` | — | `DEBUG` / `INFO` / `WARNING` (default `INFO`) |
| `AUTOXPOST_DEFAULT_TARGETS` | — | Comma-separated default targets for Telegram posts |

## Development

```bash
pip install -e ".[dev]"
pytest
```

## Risk control

AutoXPost ships with anti-ban protections enabled by default. A
`RiskGuard` wraps every adapter call and applies per-platform rate
limits, duplicate detection, jitter, and a kill switch that pauses a
platform after repeated failures.

**What's on by default**

- `min_interval_seconds` between posts to the same platform
- `daily_cap` and a shorter `burst_cap` (per hour) on each platform
- `jitter_range_seconds` of random extra sleep between successful posts
- Duplicate-text detection within a 24h window
- A kill switch that pauses a platform for `cooldown_minutes` after
  `kill_switch_threshold` consecutive failures (default 5 / 15 min for
  X/Mastodon/Bluesky; 3 / 60 min for LinkedIn)
- Auto-recognition of `429` / `RateLimitExceeded` from each platform:
  the wait hint (`Retry-After`, `x-rate-limit-reset`, atproto's
  `reset_at`) is extracted and used to set the cooldown

When any check trips, the target is recorded as
`TargetStatus.SKIPPED` with a human-readable reason in `target.error`
and the post status rolls up to `FAILED` so the runner surfaces it.
Nothing is retried until the cooldown elapses.

**Tuning per platform**

Every limit is env-overridable. The pattern is
`AUTOXPOST_<UPPER>_<FIELD>`, e.g.:

| Env var | Default (X) | Default (LinkedIn) |
| --- | --- | --- |
| `*_MIN_INTERVAL_SECONDS` | 90 | 300 |
| `*_DAILY_CAP` | 50 | 50 |
| `*_BURST_CAP` | 5 | 3 |
| `*_JITTER_MIN_SECONDS` / `_MAX_SECONDS` | 15 / 60 | 60 / 180 |
| `*_COOLDOWN_MINUTES` | 30 | 60 |
| `*_KILL_SWITCH_THRESHOLD` | 5 | 3 |

The full set of defaults lives in
[`src/autoxpost/core/safety.py`](src/autoxpost/core/safety.py). The
defaults err on the side of safety for **new / unverified** accounts;
established accounts with a track record should raise `*_DAILY_CAP`
and shrink `*_MIN_INTERVAL_SECONDS` via env.

Global knobs:

| Env var | Default | Purpose |
| --- | --- | --- |
| `AUTOXPOST_SAFETY_ENABLED` | `true` | Master switch; `false` bypasses every check |
| `AUTOXPOST_DUPLICATE_WINDOW_HOURS` | `24` | How far back to look for duplicate text |
| `AUTOXPOST_HASH_HISTORY` | `200` | Max posted-hash rows kept per platform |

**Disabling safety**

Set `AUTOXPOST_SAFETY_ENABLED=false`. The guard becomes a no-op; the
publisher calls adapters exactly as it did before this feature shipped.

## License

MIT
