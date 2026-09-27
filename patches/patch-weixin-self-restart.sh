#!/bin/bash
# Fast-path self restart requests before invoking Codex.
set -euo pipefail

targets=(
  "/Users/zhen/home-agent/weixin-agent/node_modules/weixin-agent-sdk/dist/index.mjs"
  "/opt/homebrew/lib/node_modules/weixin-acp/node_modules/weixin-agent-sdk/dist/index.mjs"
  /Users/zhen/.npm/_npx/*/node_modules/weixin-agent-sdk/dist/index.mjs
)

patched=0
for target in "${targets[@]}"; do
  [ -f "$target" ] || continue

  if grep -q "weixin self restart patch" "$target"; then
    echo "[patch] self restart already applied: $target"
    continue
  fi

  python3 - "$target" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
text = path.read_text()

helper_marker = "/** Extract raw text from item_list (for slash command detection). */"
helper = r'''// weixin self restart patch: acknowledge first, then restart from a detached helper.
function isSelfRestartCommand(text) {
	const normalized = String(text ?? "").replace(/\s+/g, "").toLowerCase();
	if (!normalized || normalized.length > 80 || normalized.startsWith("/")) return false;
	if (/(电脑|mac|机器|系统|空调|灯|电视|homepod|大门|门锁|路由|nas)/i.test(normalized)) return false;
	if (!/(重启|重载|刷新|restart|reload)/i.test(normalized)) return false;
	return /(你自己|你|自己|agent|bot|微信agent|微信bot|weixin|weixinagent|服务|进程|session|会话)/i.test(normalized);
}
async function scheduleWeixinSelfRestart(reason) {
	const scriptPath = path.join(process.cwd(), "scripts", "restart-weixin-agent.sh");
	if (!fs.existsSync(scriptPath)) throw new Error(`self restart script missing: ${scriptPath}`);
	const { spawn } = await import("node:child_process");
	const child = spawn("/bin/bash", [scriptPath, "--delay", "2", "--reason", reason], {
		cwd: process.cwd(),
		detached: true,
		stdio: "ignore",
		env: process.env
	});
	child.unref();
}
async function maybeHandleSelfRestart(textBody, full, deps, requestId, requestStartedAt) {
	if (!isSelfRestartCommand(textBody)) return false;
	if (findMediaItem(full.item_list)) return false;
	await sendReply({
		to: full.from_user_id ?? "",
		contextToken: full.context_token,
		baseUrl: deps.baseUrl,
		token: deps.token
	}, "收到，正在重启微信 agent。大概 5-10 秒后恢复。");
	await scheduleWeixinSelfRestart(`weixin self restart requestId=${requestId}`);
	logger.info(`[weixin-self-restart] scheduled requestId=${requestId} durationMs=${Date.now() - requestStartedAt}`);
	return true;
}
'''

if helper_marker not in text:
    raise SystemExit(f"helper insertion marker not found in {path}")
text = text.replace(helper_marker, helper + helper_marker, 1)

slash_needle = '''			case "/clear":
				ctx.onClear?.();
				await sendReply(ctx, "✅ 会话已清除，重新开始对话");
				return { handled: true };
			default: return { handled: false };
'''
slash_replacement = '''			case "/clear":
				ctx.onClear?.();
				await sendReply(ctx, "✅ 会话已清除，重新开始对话");
				return { handled: true };
			case "/restart":
			case "/self-restart":
				await sendReply(ctx, "收到，正在重启微信 agent。大概 5-10 秒后恢复。");
				await scheduleWeixinSelfRestart(`slash ${command}`);
				return { handled: true };
			default: return { handled: false };
'''
if slash_needle not in text:
    raise SystemExit(f"slash command insertion marker not found in {path}")
text = text.replace(slash_needle, slash_replacement, 1)

call_needle = '''	const contextToken = full.context_token;
	if (contextToken) setContextToken(deps.accountId, full.from_user_id ?? "", contextToken);
	let media;
'''
call_replacement = '''	const contextToken = full.context_token;
	if (contextToken) setContextToken(deps.accountId, full.from_user_id ?? "", contextToken);
	if (await maybeHandleSelfRestart(textBody, full, deps, requestId, requestStartedAt)) return;
	let media;
'''
if call_needle not in text:
    raise SystemExit(f"self restart call site not found in {path}")
text = text.replace(call_needle, call_replacement, 1)

path.write_text(text)
PY

  echo "[patch] self restart applied: $target"
  patched=1
done

if [ "$patched" = 0 ]; then
  echo "[patch] no new self restart bundles patched"
fi
