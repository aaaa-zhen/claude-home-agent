#!/bin/bash
# memory-distill.sh — 会话重置前的记忆蒸馏(session-manager 调用)。
#
# 三段式:build context → LLM 只输出决策 JSON → memory-apply.mjs 落盘。
# 蒸馏器不再直接编辑记忆文件(写工具已禁用),所有写入由 memory-apply.mjs 执行:
# 它分配 id、校验 target 真实存在、把被取代的旧事实搬进 archive,
# 任何一条不合法就整批拒绝并存盘等重放 —— 半批写入比不写更糟。
#
# 结构化 session checkpoint/handoff 由独立脚本负责；这里只沉淀长期记忆。
# 任何失败都只记日志退出 0,绝不阻塞重置流程(调用方有超时兜底)。
set -uo pipefail

ROOT="/Users/zhen/home-agent/weixin-agent"
LOG="$ROOT/tmp/memory-distill.log"
CLAUDE_BIN="$ROOT/node_modules/.bin/claude"
APPLY="$ROOT/scripts/memory-apply.mjs"

mkdir -p "$ROOT/tmp"
ts() { date '+%Y-%m-%d %H:%M:%S'; }
echo "[$(ts)] distill start (reason=${1:-manual})" >> "$LOG"

# 和 _migration/run-with-env.sh 一致的环境:agent 独立 Claude 授权 + Clash 代理
export CLAUDE_CONFIG_DIR="/Users/zhen/home-agent/.claude-agent"
export PATH="/opt/homebrew/bin:$PATH"
export TZ="Asia/Shanghai"
export HTTP_PROXY="http://127.0.0.1:7897"  http_proxy="http://127.0.0.1:7897"
export HTTPS_PROXY="http://127.0.0.1:7897" https_proxy="http://127.0.0.1:7897"
export NO_PROXY="192.168.1.100,localhost,127.0.0.1,.weixin.qq.com,ilinkai.weixin.qq.com,.example.com,.amap.com,.gtimg.cn,.qq.com"
export no_proxy="$NO_PROXY"

if [ ! -x "$CLAUDE_BIN" ]; then
  echo "[$(ts)] distill skip: claude binary not found" >> "$LOG"
  exit 0
fi

# 1) 组上下文。顺带给没有标记的旧事实补 id,保证模型看到的 id 和磁盘一致。
CONTEXT="$(node "$APPLY" --context 2>>"$LOG")"
if [ -z "${CONTEXT// }" ]; then
  echo "[$(ts)] distill abort: context build failed" >> "$LOG"
  exit 0
fi

TODAY="$(date '+%Y-%m-%d')"
PROMPT="你是记忆蒸馏器,在微信家庭助手会话重置前运行。今天是 ${TODAY}。

stdin 里是一个 JSON 上下文,字段:
- facts: 当前生效的长期事实,每条有 id / date / section / source / confidence / seen / text
- pending_facts: **还没生效的候选**(推断来的,等佐证)。这一轮如果对上了,就用
  add_evidence 或 merge 指向它的 id;够格了系统会自动把它升成生效规则
- sections: learned-facts.md 里允许写入的分类名(只能用这些)
- recent_context: 最近的对话流水(最新在末尾)
- recent_summary: conversation-summary.md 最近的条目,用来查重
- open_reviews: 已经在等用户答复的冲突,**同一件事不要再入队一次**

任务:判断 recent_context 里有没有值得沉淀的东西,输出决策。**不要使用任何工具**,
上下文已经全在 stdin 里；**只输出一个 JSON 对象**,前后不要有任何解释文字。

输出格式(键名照抄,每条决策的动作键叫 decision):
{
  \"summary\": [\"[${TODAY}] 主题 — 关键细节\"],
  \"life_events\": [\"[${TODAY}] 用户从魅族离职,离职后自驾大西北,约 8·13 回珠海\"],
  \"decisions\": [
    {\"decision\": \"add\", \"section\": \"设备操作\", \"text\": \"**厨房没有空调**：家里只有客厅/主卧/书房三台。\`#设备\` \`#空调\`\", \"source\": \"user\", \"confidence\": \"high\", \"rationale\": \"用户今天纠正\"},
    {\"decision\": \"update\", \"text\": \"**控制家居前先判断 Mac 是否在家庭网段**。\`#部署\` \`#HA\`\", \"supersedes\": [\"f0048\"], \"source\": \"user\", \"confidence\": \"high\", \"rationale\": \"离家后本地直连失效,旧规则已不成立\"},
    {\"decision\": \"add_evidence\", \"target_id\": \"f0032\", \"rationale\": \"这次对话再次印证\"},
    {\"decision\": \"review\", \"conflict_with\": [\"f0012\", \"f0055\"], \"rationale\": \"两条都像还生效,判不准该退休哪条\"}
  ]
}
三个数组都可以为空。

summary: 追加到对话摘要的行,最多 5 条。只记新决定/新偏好/问题解决/重要事件/
用户正在做的事。翻译、闲聊、单纯问答、阅读卡片推送不算。已在 recent_summary
里出现过的不要重复。没有就给空数组。

life_events: 人生量级的大事,最多 3 条,追加进 user-profile 的「近期重大动态」节。
**判据严格**:只有会长期改变用户处境的节点才算——离职/入职/换工作、搬家/换城市、
结婚/分手/家庭成员变化、重大疾病或住院、重大旅行或长期出行、大额消费(买房买车)。
日常出行、买菜、点咖啡、开关空调、翻译、闲聊、看新闻、写代码 demo **都不算**。
拿不准就不要放进来——宁可漏,不要把普通事项污染进 profile。每条一行,`[日期]` 开头,
写清楚发生了什么、大概时间。已在 recent_summary 或 facts 里出现过的同一件事不要重复。
没有就给空数组(绝大多数平常的日子都应该是空的)。

decisions: 对长期事实的操作,最多 12 条。整批推理,不要一条一条孤立地看。
每条七选一:

- add            新事实。需要 section / text / source / confidence
- update         新事实取代旧的(矛盾、过期)。需要 text / supersedes(旧 id 数组) / source / confidence
- merge          在原地改写某条事实,吸收新信息但还是同一件事。需要 target_id / text
- add_evidence   已有事实被再次印证,内容不变。需要 target_id
- skip_duplicate 这次说的事已经有了,什么都不用做。需要 target_id
- keep_both      两条事实兼容且各自成立,不需要动。可给 rationale
- review         矛盾但你判不准谁对。需要 conflict_with(id 数组) / rationale

source(这条信息哪来的,决定可信度):
  user=用户在微信里明说 / correct=用户纠正你的错误 / tool=工具或 API 验证过
  infer=你从对话里推断出来的 / cron=定时推送内容
confidence: high / med / low

规矩:
- id 只能用 facts 里真实存在的。**编造 id 会导致整批被拒绝**,拿不准就用 review。
- 只有真的过期或被推翻才 update;两件事能同时成立就 keep_both 或干脆不给决策。
- 宁缺勿滥,没东西可沉淀就两个数组都留空。
- open_reviews 里已有的冲突不要重复入队;蒸馏器不负责回答它们,那是要问用户的。
- **source 要如实填**,不要为了让事实生效就把推断写成 user。infer/cron 来源和 low
  置信度会自动进候选区等佐证,这是设计好的,不是你要绕开的东西。用户明确说过的
  才是 user,你从对话里推出来的就是 infer。
- 推测不能拿来推翻已生效的规则(带 supersedes 的 update 必须是 user/correct/tool
  且非 low),这种情况用 review 交给用户裁决。
- text 一行写完,沿用原有的 \`#标签\` 风格,不要换行。
- **字符串里不要出现 ASCII 双引号**(那会让 JSON 解析失败)。要引用用户说的话就用
  中文引号「」或直接不加引号,例如写 用户说「打开厨房空调」。
- 不要试图整体编辑 user-profile.md 或 devices.md(它们会话内实时维护)。user-profile
  的更新只走 life_events 这一个口子——你给出结构化的一行,系统只做安全追加,不会重写
  已有内容。devices.md 完全不碰。"

# 2) LLM 只做语义判断,写工具全部禁用 —— 它已经不需要碰文件了。
OUT="$(printf '%s' "$CONTEXT" | "$CLAUDE_BIN" -p "$PROMPT" \
  --model sonnet \
  --disallowed-tools Write Edit NotebookEdit Bash \
  --setting-sources user \
  2>>"$LOG")"
RC=$?

if [ $RC -ne 0 ] || [ -z "${OUT// }" ]; then
  echo "[$(ts)] distill abort: llm rc=$RC empty=$([ -z "${OUT// }" ] && echo yes || echo no)" >> "$LOG"
  exit 0
fi

# 3) 确定性执行。批次不合法时一条都不写,原始输出存盘可用 --file 重放。
APPLIED="$(printf '%s' "$OUT" | node "$APPLY" --stdin 2>>"$LOG")"
ARC=$?
echo "[$(ts)] distill done apply_rc=$ARC result=$(printf '%s' "$APPLIED" | tr -d '\n' | cut -c1-400)" >> "$LOG"
exit 0
