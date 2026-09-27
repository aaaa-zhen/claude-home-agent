# Home Agent Architecture Layers

This Mac is the primary home-agent host. Other computers should be treated as
temporary execution endpoints unless they are explicitly promoted into the
runtime stack.

## 1. Agent Runtime

The runtime is the long-lived agent brain and tool executor.

- `start.sh` starts `weixin-acp` with `claude-agent-acp`.
- `AGENTS.md` defines the current operating contract.
- `CLAUDE_CONFIG_DIR=/Users/zhen/home-agent/.claude-agent` keeps the live
  WeChat Claude session isolated from other Claude sessions.
- Agent OS v1/v2 exists as an experimental task-orchestration sidecar. Its
  gateway is currently disabled in the live service.
- `session-manager.py` reads Claude Code's real prompt-token usage, writes a
  structured checkpoint, and rotates the live session during a safe idle
  window. The assistant identity and durable state survive the rotation.

## 2. Channel Adapters

Channel adapters receive user intent and return user-facing replies.

- WeChat: `weixin-acp`, `weixin-send.mjs`, and media send markers.
- Android app polling: `api-server.py` and `notifications.json`.
- Browser previews: `api-server.py` serves tokenized preview URLs.

Channel adapters should stay thin. They should translate messages, files, and
notifications without owning business logic.

## 3. Tool Layer

Tools are local scripts or MCP servers the agent can call.

- Home Assistant: `ha_mcp_server.py`, `ha_run.sh`, `scripts/ha-fast-status.mjs`.
- Maps and travel: `tools/travel/amap_nav.py`, `chelaile.py` (runs as the
  `chelaile-bus` launchd service), `tools/travel/flight.py`, `tools/travel/train_proxy.py`.
- Logistics and daily tasks: `tools/info/kuaidi100_proxy.py`, `tools/info/sf_order.py`, reminders.
- Media and browser helpers: `tools/media/image_gen.py`, `scripts/media-search.mjs`,
  and `scripts/browser-bridge.mjs`. Browser Bridge keeps account sessions in a
  dedicated visible Chrome profile outside the repository; site adapters expose
  narrow actions instead of arbitrary browser scripting.
- Apple apps: `scripts/apple-bridge.sh` provides a signed, JSON-speaking bridge
  to iCloud Calendar and Reminders through EventKit, and Notes through Apple
  automation. Its write commands preview by default.
- Remote workstations: `core/remote_hosts.py`.
- Codex delegation: `core/codex_task.py` starts a bounded non-interactive job for
  research, file work, coding, and other self-contained tasks.

Each tool should have a narrow CLI/API and keep secrets out of command output.

## 4. State Layer

State is everything needed to resume work after restart or migration.

- Personal memory: `memory/`.
- Runtime media: `media/`.
- Generated preview projects: `projects/previews/`.
- Live Claude session/config state: `/Users/zhen/home-agent/.claude-agent`.
- Optional Codex executor state: `/Users/zhen/home-agent/.codex-weixin`.
- WeChat login state: `~/.openclaw/openclaw-weixin`.
- Runtime-only agent state and remote host credentials: `runtime/`.
- Logs: `/Users/zhen/home-agent/_migration/logs/`, `tmp/`, and service logs.

State is more important than code for continuity. Any cloud-agent migration
should move or restore this layer deliberately.

## 5. Ops Layer

Ops keeps the system alive and inspectable.

- macOS launchd is the current supervisor.
- `healthcheck.sh` is the main readiness check.
- `backup-memory.sh` snapshots memory.
- `scripts/memory-cleanup.mjs` trims memory files.
- `_migration/` contains Mac migration helpers and legacy service notes.
- `deploy/` contains older Linux/systemd templates and should be treated as
  reference until refreshed.

## Repo Layout Convention

Where a new file goes, so root stays as a small set of entrypoints:

- **Root** — only long-running services and top-level entrypoints whose paths are
  hard-wired into launchd plists, crontab, or `CLAUDE.md`: `start.sh`,
  `monitor.py`, `session-manager.py`, `api-server.py`, `ha_mcp_server.py`,
  `chelaile.py`, `geofence_reminder.py`, `weixin-send.mjs`, `weixin-send-file.mjs`,
  `healthcheck.sh`. Do not move these without also updating every hard-wired path.
- **`tools/<info|travel|media>/`** — leaf helper CLIs/proxies the agent calls
  (weather, stock, maps, flight, image gen…). New single-purpose tools go here.
- **`core/`** — shared Python modules imported by services (notify, task runners,
  remote hosts, local file tool).
- **`scripts/`** — `.mjs` utilities and bridges (browser, apple, memory, market).
- **`patches/`** — one-off `patch-*.sh` fixups.

New helper scripts belong in `tools/` or `scripts/`, not root.

## Current Boundary

The current truth is Mac-first launchd. Linux/systemd docs and service files are
useful history, but they do not describe the live runtime unless they are
explicitly refreshed.

For the future cloud-agent shape, keep the Mac as the primary agent host and add
computers through channel adapters or remote workstation endpoints instead of
duplicating the whole stack onto each machine.
