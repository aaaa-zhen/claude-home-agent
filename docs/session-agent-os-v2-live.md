# Session-Agent OS v2 — 历史线上架构（当前已下线）

> 本文档描述 **当前线上实际运行** 的形态(2026-06-26 切换上线)。
> 设计动机/取舍见 [`session-agent-os-v2.md`](session-agent-os-v2.md)。本文记录曾经上线的 v2 形态；当前 live gateway 已关闭，真实主链路以 `start.sh` 为准。

## 一句话

一个**连续主脑**,每条微信消息按**快慢分流**,上下文**每轮重组**,记忆**分三层**。微信里只有一个声音——没有多 bot、没有影子。

替代了 v1 Agent OS("每条消息 = 一个失忆的一次性 exec + worker 各自当脑子",会丢上下文、误把简单问题甩给 job agent)。

## 消息链路(线上真实)

```
微信消息
  │
  ▼
weixin-acp(长轮询 ilink 网关,openclaw-weixin 传输层)        ← launchd com.zhen.weixin-agent
  │   SDK processOneMessage → maybeHandleAgentOSGateway
  │   env: WEIXIN_AGENT_OS_GATEWAY=1, WEIXIN_AGENT_OS_MODULE=agent_os.v2, WEIXIN_AGENT_OS_LONG_JOB_ACK=0
  ▼
python -m agent_os.v2 gateway <text> --user-id … --json     ← 每条消息一个一次性进程
  │
  ├ 1. open_question? 有 → 这句当答案,绑回原 task,恢复执行(decision 三)
  ├ 2. 判快慢 classify_mode + probe:
  │      instant      寒暄/确认           → 当场答
  │      interactive  天气/股票/控设备/导航查询 → 当场办+答
  │      background   导航/下载/写东西/调研/批量  → 秒回 ack「约X分钟,办好叫你」,入队 queued
  ├ 3. (快任务) drain inbox(后台结果) → 组装上下文 → 主脑(codex) → 回复
  └ 4. 落库 messages(FTS)+ 写回 recent-context.md
  │
  ▼
{"response":{"text": …}} → weixin-acp 发回微信
```

慢任务由常驻 worker 接力:

```
v2.db tasks(status=queued)
  │
  ▼
worker_service(常驻,每 2s 轮询 claim_queued)                ← launchd com.zhen.v2-worker
  │  拉任务 → 组装上下文 → codex 跑完 → set done + 写 task.done 事件
  ▼
weixin-send.mjs 主动推送结果给用户 + 写回 recent-context
```

## 五个零件

| 零件 | 角色 | 文件 |
|---|---|---|
| **Main Brain** | 每轮一次全新 codex 调用 + turn 内调工具;连贯来自"组装好的上下文"而非长会话(decision 一) | `agent_os/v2/brain.py` |
| **Memory Service** | 近N轮 + 任务栈 + 后台结果 + 检索记忆 → 按 token 预算确定性组装;每轮写回 recent-context | `agent_os/v2/memory_service.py` |
| **Store** | SQLite(`runtime/v2.db`):tasks / events(inbox, delivered) / pending_questions / **messages(jieba FTS5)** | `agent_os/v2/store.py` |
| **probe + scheduler** | 估慢任务 ETA 决定是否后台化(>30s 干等预算转后台);后台限并发 FIFO | `agent_os/v2/probe.py` `scheduler.py` |
| **Worker** | 常驻,把队列里慢任务跑完并主动推送(decision 二·唤醒源 b) | `agent_os/v2/worker_service.py` |
| 入口/CLI | `gateway`(微信入口)/ `recall` / `chat` / `tasks` / `events` / `tools` | `agent_os/v2/cli.py` |

## 快慢分流(优先级)

不靠长短猜,靠**签名 + probe 估时**(`probe.py`):

| 任务 | 估时 | 处理 |
|---|---|---|
| 天气 ~12s、股票 ~26s | < 30s 预算 | interactive,当场等 |
| 导航 ~90s、调研 ~120s、下载 ~240s | > 30s 预算 | background,秒 ack + 后台办完推送 |
| 寒暄/确认 | — | instant,直接答 |

`V2_INTERACTIVE_BUDGET_SEC`(默认 30)可调干等阈值。

## 记忆三层

1. **短期** — `memory/recent-context.md`:每轮写回的滚动缓冲(~50 行)。跨消息 + **跨重启**连贯;一次性 gateway 进程之间靠它接上下文。("明天的天气呢"接住"天气"就是它)
2. **长期事实** — `memory/*.md`(devices / user-profile / learned-facts / zhuhai-guide …):关键词检索注入。
3. **全量历史** — `v2.db` messages 表 + **jieba 中文分词 FTS5(bm25)+ 2字滑窗 LIKE 兜底**:主脑用 `recall` 工具搜任意过去对话;中文召回 6/6。借鉴 Hermes 的 FTS5 思路,按中文场景改良。

## 进程(launchd,7×24)

| label | 作用 | 状态 |
|---|---|---|
| `com.zhen.weixin-agent` | weixin-acp 传输 + 路由到 v2 | 已切 v2 |
| `com.zhen.v2-worker` | 后台慢任务执行 + 主动推送 | 新增 |
| `com.zhen.v2-shadow` | 影子(只读旁路) | 已停(上线后冗余) |
| `com.zhen.weixin-monitor` | 到家/离家/温度/Tunnel | 照旧 |
| `com.zhen.weixin-session-manager` | 空闲才重置 session | 照旧 |
| `com.zhen.chelaile-bus` / `api-server` / `cloudflared` / `caffeinate` | 公交/API/隧道/防睡 | 照旧 |

plist 由 `_migration/gen-plists.sh` 生成。

## 上线接线(cutover)

微信入口的 gateway 补丁(`patches/patch-weixin-agent-os-gateway.sh` 注入 SDK)原本写死 `-m agent_os`;改成 env 驱动:

```
node_modules/weixin-agent-sdk/dist/index.mjs:2038
  "-m", process.env.WEIXIN_AGENT_OS_MODULE || "agent_os", "gateway", text,
```

`start.sh` 设默认:

```
WEIXIN_AGENT_OS_MODULE=agent_os.v2      # 路由到 v2
WEIXIN_AGENT_OS_LONG_JOB_ACK=0          # 关掉 SDK 那段硬编码"已交给 job agent"预回复(v2 自己判快慢 ack)
```

**回退到 v1**:`start.sh` 把 `WEIXIN_AGENT_OS_MODULE` 默认值改回 `agent_os`,然后
`launchctl kickstart -k gui/$(id -u)/com.zhen.weixin-agent`。v1 代码一行没删。

> ⚠️ 已知小项:SDK 有第二份不活跃副本 `~/.npm/_npx/*/node_modules/weixin-agent-sdk`,其 module 行尚未改成 env 驱动(当前在跑的是 `node_modules/` 那份,不影响)。若 npx 解析切换需同步改它,防止静默回退 v1。

## 状态文件

- `agent_os/v2/runtime/v2.db` —— 任务/事件/待答问题/消息全文索引(gitignored)
- `memory/recent-context.md` —— 短期对话缓冲(gateway 每轮写)
- `memory/*.md` —— 长期记忆
- 日志:`_migration/logs/com.zhen.{weixin-agent,v2-worker}.{out,err}.log`

## 还没做(后续可选)

- **记忆自动 curate**(Hermes 冻结快照 + 后台 fork 复盘):每 N 轮 fork 子任务把偏好/事实沉淀进 `memory/`,不靠手写。
- **任务状态机硬化**(OpenClaw):deliveryStatus / timed_out / cancelled / lost / 60s 对账。
- _npx 副本 env 化(见上)。
