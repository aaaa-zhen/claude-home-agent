# State And Backup

The home agent should be backed up by state class, not by a blind copy of the
whole working tree.

## Durable State

Back up these intentionally:

- `memory/` - personal memory, follow-ups, learned facts, session handoff.
  This includes the latest structured session checkpoint and its small rolling
  archive under `memory/session-checkpoints/`.
- `media/` - inbound media, generated source assets, and `index.jsonl`.
  Downloaded video delivery copies under `media/outbox/` use a seven-day
  retention policy and do not need long-term backup.
- `projects/previews/` - publishable private web previews.
- `/Users/zhen/home-agent/.claude-agent` - live WeChat Claude session and
  configuration state.
- `/Users/zhen/home-agent/.codex-weixin` - optional Codex executor config and
  sqlite state.
- `~/.openclaw/openclaw-weixin` - WeChat login/account state.
- `runtime/` - runtime-only state such as remote workstation SSH profiles,
  connector tokens, and queued connector jobs.
- `.env` and other secret-bearing config, stored only in a private backup.

## Generated Or Disposable State

These can usually be rebuilt:

- `node_modules/`
- `venv/`
- `tmp/`
- downloaded/outbound video delivery copies older than seven days
- cache files
- local logs, unless they are needed for debugging an incident

## Current Backup

`backup-memory.sh` currently snapshots only `memory/` into
`tmp/memory-backups/`, with optional git mode.

That is enough for conversation continuity, but not enough for a full agent
restore. A full restore plan should include `.claude-agent`, `.codex-weixin`, media, previews,
WeChat login state, `.env`, and `runtime/`.

## Restore Order

1. Restore code and install dependencies.
2. Restore `.env` and secret-bearing config.
3. Restore `.claude-agent` and `.codex-weixin`.
4. Restore WeChat login state.
5. Restore `memory/`, `media/`, `projects/previews/`, and `runtime/`.
6. Run `healthcheck.sh`.
7. Restart launchd services.

## Secret Rule

Never commit `.env`, `runtime/remote-hosts.json`, auth files, WeChat account
tokens, connector tokens, or Codex sqlite state. Use private encrypted backup
for those.
