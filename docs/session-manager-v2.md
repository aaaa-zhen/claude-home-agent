# Session Manager 2.0 — As Built

## Goal

Keep natural conversation on one live Claude session without treating that
session as permanent identity. Rotate before context quality degrades, preserve
an auditable checkpoint, and never put Agent OS Gateway back in the live path.

## Live Path

```text
WeChat -> weixin-acp -> persistent Claude session
                           |
                           +-> tools / local APIs

session-manager.py (sidecar only)
  -> reads transcript metadata and token usage
  -> waits for a safe idle window
  -> builds structured checkpoint with a tool-less model call
  -> verifies no new message arrived
  -> atomically commits checkpoint
  -> rotates weixin-acp; launchd restarts it
```

The sidecar never classifies user intent, routes a user turn, or writes a
business response. It only manages session lifecycle.

## Pressure Policy

- Context window: 200,000 tokens by default, configurable.
- Soft pressure: 50% context or 45 queued user turns.
- Hard pressure: 70% context or 65 queued user turns.
- Soft rotation: after 10 idle minutes.
- Hard rotation: after 3 idle minutes.
- Minimum pressure-based session age: 45 minutes.
- Daily and 24-hour age resets remain as fallback policies.

All thresholds can be overridden with `SESSION_*` environment variables.

## Race Safety

Checkpoint generation takes several seconds. The manager records the latest
user activity before generating it, then re-reads the live transcript. If a new
message arrived, a turn is still running, or the active session changed, the
pending checkpoint is discarded and rotation is deferred.

## Checkpoint

`memory/session-checkpoint.json` contains:

- current topic and user goal;
- confirmed decisions;
- genuine open loops and active tasks;
- important entities and recent references;
- the last few exact, redacted turn summaries;
- pressure metrics and rotation reason.

The checkpoint model runs with no tools, safe mode, schema-constrained JSON,
and no session persistence. If it fails, a deterministic fallback still
produces a valid checkpoint. Older checkpoints are retained in a bounded local
archive for diagnosis.

## New Session Bootstrap

On its first message, the new Claude session reads the latest checkpoint,
recent context, active follow-ups, and the newest human-readable handoff. A
pressure rotation uses `continuation_style=continue`, so the assistant resumes
naturally without announcing a restart or greeting again.

## Operations

```bash
./venv/bin/python session-manager.py status --json
./venv/bin/python session-manager.py check --dry-run
./venv/bin/python -m unittest -v tests.test_session_manager
```

## 2.1 — Compact In Place (2026-08-31)

Rotation is no longer the default answer to pressure. The manager now sends
`/compact <focus>` into the live session through the chat bridge socket
(`runtime/acp-chat.sock`), so the session id — and with it conversation
continuity — survives. Verified live: 53,975 -> 2,077 tokens on the same
session id, end to end through weixin-acp -> claude-agent-acp -> Claude Code.

What changed:

- The transcript reader understands `system/compact_boundary` records:
  `context_tokens` resets to the boundary's `postTokens` (no usage record
  exists until the next real turn), and `user_turns` counts turns since the
  last compact (`total_user_turns` keeps the lifetime figure).
- An interval + growth gate (15 min / 20k tokens, configurable via
  `SESSION_COMPACT_MIN_INTERVAL_MINUTES` / `SESSION_COMPACT_MIN_GROWTH_TOKENS`)
  prevents a compact loop when the summary alone still clears the soft
  threshold.
- The checkpoint + handoff are still written before every compact — they are
  the archive of what the lossy summary may drop, and they keep the
  unexpected-reboot bootstrap fresh.
- `age_reset` is retired in compact mode. `daily_reset` compacts (or, when the
  context barely grew, just marks the day done) and still runs memory distill.
- Rotation survives only as the fallback: hard pressure plus
  `SESSION_COMPACT_FAIL_ROTATE_THRESHOLD` (default 2) consecutive compact
  failures. `SESSION_COMPACT_MODE=0` in `runtime/session-manager.env` restores
  rotation mode entirely.

Known footnote: the live model (`claude-opus-4-8`) actually reports a 1M
context window via `/context`; `SESSION_CONTEXT_WINDOW_TOKENS=200000` is kept
deliberately as a working *budget*, so compaction triggers around 100k where
long-context quality and cache cost stay comfortable — not because the model
would overflow there.
