#!/bin/bash
# Keep WeChat polling responsive while Codex is busy. Home control/status still goes through Codex.
set -euo pipefail

targets=(
  "/Users/zhen/home-agent/weixin-agent/node_modules/weixin-agent-sdk/dist/index.mjs"
  "/opt/homebrew/lib/node_modules/weixin-acp/node_modules/weixin-agent-sdk/dist/index.mjs"
  /Users/zhen/.npm/_npx/*/node_modules/weixin-agent-sdk/dist/index.mjs
)

patched=0
for target in "${targets[@]}"; do
  [ -f "$target" ] || continue

  if grep -q "weixin message queue patch" "$target"; then
    echo "[patch] message queue already applied: $target"
    continue
  fi

  python3 - "$target" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
text = path.read_text()

if "maybeHandleFastHomeControl" in text or "maybeHandleFastHomeStatus" in text:
    raise SystemExit(f"fast home shortcuts still present in {path}; run patch-weixin-disable-fast-home-shortcuts.sh first")

old_queue_marker = "// weixin interrupt fast home patch: keep receiving messages while ACP handles a long prompt."
new_queue_marker = "// weixin message queue patch: keep receiving messages while ACP handles a long prompt."
if old_queue_marker in text:
    text = text.replace(old_queue_marker, new_queue_marker, 1)
    path.write_text(text)
    raise SystemExit(0)

chat_needle = '''		const response = await deps.agent.chat(request);
'''
chat_replacement = '''		const response = await (deps.runAgentChat ? deps.runAgentChat(request) : deps.agent.chat(request));
'''
if chat_needle not in text:
    raise SystemExit(f"agent chat call site not found in {path}")
text = text.replace(chat_needle, chat_replacement, 1)

queue_marker = "const DEFAULT_LONG_POLL_TIMEOUT_MS = 35e3;"
queue_helper = r'''// weixin message queue patch: keep receiving messages while ACP handles a long prompt.
let agentChatTail = Promise.resolve();
function enqueueAgentChat(agent, request) {
	const run = () => agent.chat(request);
	const next = agentChatTail.then(run, run);
	agentChatTail = next.catch(() => {});
	return next;
}
const activeMessageTasks = /* @__PURE__ */ new Set();
function trackMessageTask(task, errLog) {
	activeMessageTasks.add(task);
	task.catch((err) => errLog(`[weixin] async message processing failed: ${String(err)}`)).finally(() => activeMessageTasks.delete(task));
}
'''
if queue_marker not in text:
    raise SystemExit(f"queue insertion marker not found in {path}")
text = text.replace(queue_marker, queue_helper + queue_marker, 1)

loop_needle = '''			await processOneMessage(full, {
				accountId,
				agent,
				baseUrl,
				cdnBaseUrl,
				token,
				typingTicket: (await configManager.getForUser(fromUserId, full.context_token)).typingTicket,
				log,
				errLog
			});
'''
loop_replacement = '''			const task = processOneMessage(full, {
				accountId,
				agent,
				baseUrl,
				cdnBaseUrl,
				token,
				typingTicket: (await configManager.getForUser(fromUserId, full.context_token)).typingTicket,
				log,
				errLog,
				runAgentChat: (request) => enqueueAgentChat(agent, request)
			});
			trackMessageTask(task, errLog);
'''
if loop_needle not in text:
    raise SystemExit(f"monitor loop call site not found in {path}")
text = text.replace(loop_needle, loop_replacement, 1)

path.write_text(text)
PY

  echo "[patch] message queue applied: $target"
  patched=1
done

if [ "$patched" = 0 ]; then
  echo "[patch] no new message queue bundles patched"
fi
