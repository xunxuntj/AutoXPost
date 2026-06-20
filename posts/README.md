# `posts/` — predefined scheduled posts

Each `*.json` file in this directory is a self-contained post. The
GitHub Action publishes any file whose `scheduled_at` is in the past and
whose `status` is not already `published`, then writes the file back
with the updated status and remote URLs.

## Schema

```json
{
  "id": "launch-2026-06-20",
  "text": "AutoXPost is live!",
  "targets": ["x", "mastodon", "bluesky", "linkedin"],
  "media_paths": [],
  "scheduled_at": "2026-06-20T13:00:00",
  "status": "pending",
  "metadata": { "campaign": "launch" }
}
```

`scheduled_at` is ISO-8601 in UTC. `targets` is a list of platform
names — anything not configured is silently skipped at publish time.

## Workflow

1. Author a file with `status: "pending"` and a future `scheduled_at`.
2. Commit and push.
3. The cron runs at 8am / 1pm / 9pm Eastern. When the time arrives, the
   action publishes the post and rewrites the file with
   `status: "published"` plus a `target_results` array containing the
   remote IDs and URLs.
4. Subsequent runs see `status: "published"` and skip it.

## Notes

- The action only commits files it actually changed — other unrelated
  edits in the repo are not touched.
- A failed publish (e.g. a transient X rate limit) leaves the file in
  `pending`; the next run will retry.
