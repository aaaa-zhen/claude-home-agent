#!/bin/bash
# Optional generic Agent OS gateway. Disabled unless WEIXIN_AGENT_OS_GATEWAY=1.
# The gateway submits user.message/task/folder events and waits for response.created;
# it does not contain business-specific home-control logic.
set -euo pipefail

targets=(
  "/Users/zhen/home-agent/weixin-agent/node_modules/weixin-agent-sdk/dist/index.mjs"
  "/opt/homebrew/lib/node_modules/weixin-acp/node_modules/weixin-agent-sdk/dist/index.mjs"
  /Users/zhen/.npm/_npx/*/node_modules/weixin-agent-sdk/dist/index.mjs
)

patched=0
for target in "${targets[@]}"; do
  [ -f "$target" ] || continue

  if grep -q "weixin agent os generic gateway patch" "$target"; then
    if grep -q "weixin-agent-os-long-job-ack" "$target"; then
      echo "[patch] agent os generic gateway already applied: $target"
      continue
    fi
    python3 - "$target" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
text = path.read_text()
anchor = '''	const python = process.env.WEIXIN_AGENT_PYTHON || path.join(root, "venv/bin/python");
	const args = [
'''
insert = r'''	const python = process.env.WEIXIN_AGENT_PYTHON || path.join(root, "venv/bin/python");
	const ackContextToken = full.context_token;
	const shouldAckLongJob = /(写|做|生成|实现|改造|修复|项目|网页|网站|应用|app|代码|测试|构建|发布|预览|claude\s*code)/i.test(text) || text.length > 180;
	if (shouldAckLongJob && ackContextToken && process.env.WEIXIN_AGENT_OS_LONG_JOB_ACK !== "0") {
		try {
			await sendMessageWeixin({
				to: full.from_user_id ?? "",
				text: `收到，已交给 job agent 开始处理：${text.slice(0, 36)}${text.length > 36 ? "..." : ""}\n完成后我会把结果发回来。`,
				opts: {
					baseUrl: deps.baseUrl,
					token: deps.token,
					contextToken: ackContextToken
				}
			});
			logger.info(`[weixin-agent-os-long-job-ack] requestId=${requestId}`);
		} catch (err) {
			logger.warn(`[weixin-agent-os-long-job-ack] failed requestId=${requestId} error=${err instanceof Error ? err.message : String(err)}`);
		}
	}
	const args = [
'''
if anchor not in text:
    raise SystemExit(f"long job ack anchor not found in {path}")
path.write_text(text.replace(anchor, insert, 1))
PY
    echo "[patch] agent os long job ack applied: $target"
    continue
  fi

  python3 - "$target" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
text = path.read_text()

helper_anchor = '''/** Find the first downloadable media item from a message. */
function findMediaItem(itemList) {
'''
helper = r'''// weixin agent os generic gateway patch: submit all text messages to Agent OS when explicitly enabled.
async function maybeHandleAgentOSGateway(textBody, full, deps, requestId, requestStartedAt) {
	if (process.env.WEIXIN_AGENT_OS_GATEWAY !== "1") return false;
	const text = String(textBody ?? "").trim();
	if (!text || text.startsWith("/") || findMediaItem(full.item_list)) return false;
	const root = process.env.WEIXIN_AGENT_ROOT || "/Users/zhen/home-agent/weixin-agent";
	const python = process.env.WEIXIN_AGENT_PYTHON || path.join(root, "venv/bin/python");
	const ackContextToken = full.context_token;
	const shouldAckLongJob = /(写|做|生成|实现|改造|修复|项目|网页|网站|应用|app|代码|测试|构建|发布|预览|claude\s*code)/i.test(text) || text.length > 180;
	if (shouldAckLongJob && ackContextToken && process.env.WEIXIN_AGENT_OS_LONG_JOB_ACK !== "0") {
		try {
			await sendMessageWeixin({
				to: full.from_user_id ?? "",
				text: `收到，已交给 job agent 开始处理：${text.slice(0, 36)}${text.length > 36 ? "..." : ""}\n完成后我会把结果发回来。`,
				opts: {
					baseUrl: deps.baseUrl,
					token: deps.token,
					contextToken: ackContextToken
				}
			});
			logger.info(`[weixin-agent-os-long-job-ack] requestId=${requestId}`);
		} catch (err) {
			logger.warn(`[weixin-agent-os-long-job-ack] failed requestId=${requestId} error=${err instanceof Error ? err.message : String(err)}`);
		}
	}
	const args = [
		"-m", process.env.WEIXIN_AGENT_OS_MODULE || "agent_os", "gateway", text,
		"--user-id", full.from_user_id ?? "wechat:zhen",
		"--channel", "wechat",
		"--request-id", requestId,
		"--wait",
		"--mark-sent",
		"--json",
		"--timeout", String(Number(process.env.WEIXIN_AGENT_OS_GATEWAY_TIMEOUT_SECONDS || 120))
	];
	if (full.context_token) args.push("--context-token", full.context_token);
	if (process.env.WEIXIN_AGENT_OS_GATEWAY_INLINE === "1") args.push("--inline");
	let stdout = "";
	try {
		const { execFile } = await import("node:child_process");
		stdout = await new Promise((resolve, reject) => {
			execFile(python, args, {
				cwd: root,
				timeout: Number(process.env.WEIXIN_AGENT_OS_GATEWAY_TIMEOUT_MS || 125000),
				maxBuffer: 1024 * 1024,
				env: {
					...process.env,
					PATH: `${path.join(root, "node_modules/.bin")}:/opt/homebrew/bin:${process.env.PATH || ""}`
				}
			}, (error, out, err) => {
				if (error) {
					error.message = `${error.message}${err ? `\n${err}` : ""}`;
					reject(error);
					return;
				}
				resolve(out);
			});
		});
	} catch (err) {
		logger.warn(`[weixin-agent-os-gateway] fallback requestId=${requestId} error=${err instanceof Error ? err.message : String(err)}`);
		return false;
	}
	let payload;
	try {
		payload = JSON.parse(String(stdout).trim());
	} catch {
		logger.warn(`[weixin-agent-os-gateway] fallback requestId=${requestId} invalid json: ${String(stdout).slice(0, 500)}`);
		return false;
	}
	const replyText = payload?.response?.text;
	const contextToken = full.context_token;
	if (!replyText || !contextToken) return false;
	await sendMessageWeixin({
		to: full.from_user_id ?? "",
		text: markdownToPlainText(replyText),
		opts: {
			baseUrl: deps.baseUrl,
			token: deps.token,
			contextToken
		}
	});
	logger.info(`[weixin-agent-os-gateway] handled requestId=${requestId} task=${payload.task?.task_id ?? ""} durationMs=${Date.now() - requestStartedAt}`);
	return true;
}

'''
if helper_anchor not in text:
    raise SystemExit(f"helper anchor not found in {path}")
text = text.replace(helper_anchor, helper + helper_anchor, 1)

call_anchor = '''	if (await maybeHandleSelfRestart(textBody, full, deps, requestId, requestStartedAt)) return;
	let media;
'''
call_replacement = '''	if (await maybeHandleSelfRestart(textBody, full, deps, requestId, requestStartedAt)) return;
	if (await maybeHandleAgentOSGateway(textBody, full, deps, requestId, requestStartedAt)) return;
	let media;
'''
disabled_anchor = '''	if (await maybeHandleSelfRestart(textBody, full, deps, requestId, requestStartedAt)) return;
	// direct Agent OS home-control gateway disabled: keep accuracy by routing through the main agent.
	let media;
'''
disabled_replacement = '''	if (await maybeHandleSelfRestart(textBody, full, deps, requestId, requestStartedAt)) return;
	// direct Agent OS home-control gateway disabled: keep accuracy by routing through the main agent.
	if (await maybeHandleAgentOSGateway(textBody, full, deps, requestId, requestStartedAt)) return;
	let media;
'''
if call_anchor in text:
    text = text.replace(call_anchor, call_replacement, 1)
elif disabled_anchor in text:
    text = text.replace(disabled_anchor, disabled_replacement, 1)
else:
    raise SystemExit(f"call anchor not found in {path}")

path.write_text(text)
PY

  echo "[patch] agent os generic gateway applied: $target"
  patched=1
done

if [ "$patched" = 0 ]; then
  echo "[patch] no new agent os generic gateway bundles patched"
fi
