#!/bin/bash
# room-daily-digest.sh — 给房间事件帧补一句话文字描述(写回 events.jsonl 的 desc 字段),
# 相当于手机图库的"闲时 OCR 建索引":平时增量打标,查询时 grep 文本秒回,不用现场看图。
# launchd: com.zhen.room-digest 每小时增量跑(只处理没 desc 的新事件,没活直接退出不调 claude)。
# 手动跑: bash scripts/room-daily-digest.sh [YYYY-MM-DD];不带参数 = 昨天+今天。
# 日志: tmp/room-digest.log
set -uo pipefail

ROOT="/Users/zhen/home-agent/weixin-agent"
LOG="$ROOT/tmp/room-digest.log"
CLAUDE_BIN="$ROOT/node_modules/.bin/claude"
PY="$ROOT/venv/bin/python"
EVENTS="$ROOT/media/room-log/events.jsonl"
DATES="${1:-$(date -v-1d '+%Y-%m-%d') $(date '+%Y-%m-%d')}"
MAX_FRAMES=40
mkdir -p "$ROOT/tmp"
ts() { date '+%Y-%m-%d %H:%M:%S'; }

export CLAUDE_CONFIG_DIR="/Users/zhen/home-agent/.claude-agent"
export PATH="/opt/homebrew/bin:$PATH"
export TZ="Asia/Shanghai"
export HTTP_PROXY="http://127.0.0.1:7897"  http_proxy="http://127.0.0.1:7897"
export HTTPS_PROXY="http://127.0.0.1:7897" https_proxy="http://127.0.0.1:7897"
export NO_PROXY="192.168.1.100,localhost,127.0.0.1,.weixin.qq.com,ilinkai.weixin.qq.com,.example.com,.amap.com,.gtimg.cn,.qq.com"
export no_proxy="$NO_PROXY"

[ -x "$CLAUDE_BIN" ] || { echo "[$(ts)] skip: no claude" >> "$LOG"; exit 0; }
[ -f "$EVENTS" ] || { echo "[$(ts)] skip: no events.jsonl" >> "$LOG"; exit 0; }

for DATE in $DATES; do

# 选出该日期还没有 desc 的事件;超过 MAX_FRAMES 就均匀抽样。
# 每行格式: "主图路径" 或 "变化后路径|变化前路径"(change 事件给前后对比图)
FRAMES="$($PY - "$DATE" "$MAX_FRAMES" <<'PYEOF'
import json, sys
from pathlib import Path
date, cap = sys.argv[1], int(sys.argv[2])
events = Path("/Users/zhen/home-agent/weixin-agent/media/room-log/events.jsonl")
todo = []
for ln in events.read_text().splitlines():
    try:
        e = json.loads(ln)
    except Exception:
        continue
    if e.get("ts", "").startswith(date) and not e.get("desc") and Path(e.get("path", "")).exists():
        if e.get("type") == "change" and Path(e.get("before", "")).exists():
            todo.append(f"{e['path']}|{e['before']}")
        else:
            todo.append(e["path"])
if len(todo) > cap:
    step = len(todo) / cap
    todo = [todo[int(i * step)] for i in range(cap)]
print("\n".join(todo))
PYEOF
)"

if [ -z "$FRAMES" ]; then
  continue   # 这个日期没活;静默跳过(每小时跑,别刷日志)
fi
N=$(printf '%s\n' "$FRAMES" | wc -l | tr -d ' ')

PROMPT="你在给家庭摄像头的事件做离线标注(客厅摄像头,画面里是 Zhen 的家)。下面 $N 行,每行一个事件:

- 带 | 的行是**物品变化对比**:| 前是变化后、| 后是变化前。用 Read 看两张图,对比说清楚什么东西出现了/消失了/挪动了(尽量具体:什么东西、在哪)。**只描述物品,画面里如果有人,最多说'有人在场',不要描述人在做什么、穿什么**——这是用户明确要求的隐私边界。
- **如果两张图的差异只是人**(人出现/离开/换了姿势/挪了位置),没有任何物品层面的变化,value 只写 PERSON_ONLY(系统会把这条记录连图一起删掉,这是用户要求的隐私措施)。注意:人离开时留下了新东西(比如桌上多了杯子),那算物品变化,照常描述,不算 PERSON_ONLY。
- 单路径的行是离家时段的动静帧:正常描述画面(有没有人、在做什么、场景状态)。

最后只输出一个 JSON 对象(不要 markdown 代码块),key 是每行的**第一个**图片绝对路径,value 是那句中文描述:
$FRAMES"

OUT="$(cd "$ROOT" && "$CLAUDE_BIN" -p "$PROMPT" \
  --model sonnet \
  --permission-mode bypassPermissions \
  --setting-sources user \
  2>>"$LOG")"
RC=$?
if [ $RC -ne 0 ] || [ -z "$OUT" ]; then
  echo "[$(ts)] digest error rc=$RC stdout=$(printf '%s' "$OUT" | head -c 300 | tr '\n' ' ')" >> "$LOG"
  continue
fi

# 把描述合并回 events.jsonl(整体重写,desc 只加不覆盖)
# 注意:不能 printf | python - <<heredoc —— heredoc 会顶掉管道 stdin,只能走临时文件
OUT_FILE="$ROOT/tmp/room-digest-last.txt"
printf '%s' "$OUT" > "$OUT_FILE"
$PY - "$DATE" "$OUT_FILE" <<'PYEOF' >> "$LOG" 2>&1
import json, re, sys
from pathlib import Path
raw = Path(sys.argv[2]).read_text()
m = re.search(r"\{.*\}", raw, re.S)
if not m:
    print(f"digest merge: 输出里没找到 JSON: {raw[:200]}")
    sys.exit(0)
descs = json.loads(m.group(0))
events = Path("/Users/zhen/home-agent/weixin-agent/media/room-log/events.jsonl")
lines, updated, purged = [], 0, 0
for ln in events.read_text().splitlines():
    try:
        e = json.loads(ln)
        if not e.get("desc") and e.get("path") in descs:
            desc = str(descs[e["path"]]).strip()
            if e.get("type") == "change" and desc.upper().startswith("PERSON_ONLY"):
                # 纯人变化(坐姿/位置):按用户隐私要求,事件和前后图一起删
                for k in ("path", "before"):
                    try:
                        Path(e.get(k, "")).unlink(missing_ok=True)
                    except Exception:
                        pass
                purged += 1
                continue
            e["desc"] = desc[:200]
            updated += 1
        lines.append(json.dumps(e, ensure_ascii=False))
    except Exception:
        lines.append(ln)
events.write_text("\n".join(lines) + ("\n" if lines else ""))
print(f"digest merge: {sys.argv[1]} 标注 {updated} 条, 清除纯人变化 {purged} 条")
PYEOF
echo "[$(ts)] done $DATE ($N 帧)" >> "$LOG"

done
exit 0
