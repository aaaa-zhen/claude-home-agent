# Session-Agent OS v2 (sidecar)

持续主脑 + 每轮重组上下文的旁路实现。设计基线见 [`../../docs/session-agent-os-v2.md`](../../docs/session-agent-os-v2.md)。

**完全旁路**:状态写独立 `runtime/v2.db` / `runtime/shadow.db`,只读 `memory/`,不碰在线 `weixin-agent` / `agent_os` 主链路。

## 模块

| 文件 | 角色 |
|------|------|
| `memory_service.py` | 上下文组装器:近 N 轮 + task stack + inbox + 检索 memory + worker 结果,固定预算、grep 检索 |
| `brain.py` | 主脑:每轮全新调用 + turn 内 agent loop(复用 `codex_task`) |
| `tool_registry.py` | 工具箱:ha/weather/amap/stock/bus/flight/preply/send_file… |
| `store.py` | 最小 Store:tasks / events(delivered) / pending_questions |
| `worker.py` | 后台执行器:拿 Context Packet 跑 codex,写 done/failed 事件 |
| `probe.py` | 慢任务 ETA 估计(可选,只在 interactive 但像慢任务时调) |
| `scheduler.py` | 后台并发管控:FIFO + 并发上限 |
| `main_agent.py` | 主链路:mode 判定 → 分派(同步/后台)→ drain inbox → 回复 |
| `shadow_tap.py` | 影子测试:只读 tail 真实微信消息喂给 v2,只写日志 |
| `cli.py` | 本地 CLI |

## 跑

```bash
# 离线看组装上下文(不调 codex)
./venv/bin/python -m agent_os.v2 assemble "客厅空调开着吗"

# 单轮 / 多轮
./venv/bin/python -m agent_os.v2 turn "客厅空调开着吗"
./venv/bin/python -m agent_os.v2 chat

# 看任务 / inbox / 工具
./venv/bin/python -m agent_os.v2 tasks
./venv/bin/python -m agent_os.v2 events
./venv/bin/python -m agent_os.v2 tools
```

## 影子测试(需要真实微信流量)

```bash
# log-only,默认绝不发消息(推荐先这样)
./venv/bin/python -m agent_os.v2.shadow_tap
# 另开终端看影子结果
tail -f agent_os/v2/runtime/shadow.jsonl

# 额外把影子回复单独发给自己(前缀[v2影子]);永不替代主链路
./venv/bin/python -m agent_os.v2.shadow_tap --notify-me
```

影子 tap 只读 tail `/tmp/openclaw/openclaw-<date>.log` 的 `[weixin-msg] start` 行,
复制一份真实消息给 v2 跑,完全不碰在线 weixin-acp / 主链路。

## 落地状态

- ✅ Step 1 根治上下文(组装器 + 主脑 + 工具箱)
- ✅ Step 2 后台 + event inbox(store/worker + 派发/drain/主动推送/pending_question)
- ✅ Step 3 probe + scheduler(慢任务 ETA 判定 + 并发管控)
- ✅ 影子 tap(待真实流量验证)
- ⏳ 转正:tap 验证 OK 后,把主链路从 v1 切到 v2;Store 合并到生产 `store.py`
