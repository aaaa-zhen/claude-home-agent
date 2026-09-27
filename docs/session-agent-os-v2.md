# Session-Agent OS v2

一个由**持续主脑**驱动的个人执行系统。主脑负责理解、记忆、决策和回复；Agent OS Store 负责事件、任务、checkpoint 和 inbox；workers 只负责拿 Context Packet 执行工具并返回 evidence。

> 这是 v2 的**设计基线**(动手前的契约),不是已实现状态。实现进度见末尾「落地顺序」。

## 0. 为什么要 v2:把 v1 的职责反过来

v1(当前 Agent OS)的链路是 **worker 自己当脑子**:

```text
Worker 拿 goal → 自己当脑子 → 自己决定 → 自己回复
```

后果(已确认的根因):

- `agent_os/workers.py:307` 的 `_agent_prompt()` 只塞 `用户任务:{task.goal}` 这一条当前消息,**没有对话历史**。
- `codex_task.py` 是 `codex exec --output-last-message` 的**一次性进程**,没有 resume/thread,**每条微信消息 = 一个全新、失忆的 Codex**。
- Folder 表有 `context_snapshot_json` / `memory_buffer_json` 两列(`store.py:114/129`),但 prompt 构造**一个字都没读** —— 上下文槽位挖好了,线没接。

症状:多轮指代必丢(上下文不行)、决策分散在一堆失忆 exec 里(不够智能)。

v2 把链路**反转**:

```text
Main Agent 决定 → 生成 Context Packet → Worker 执行 → 回 evidence → Main Agent 回复
```

主脑独占决策与记忆;worker 退化成手。

## 1. 五个组件

| 组件 | 角色 | 现有代码归属 |
|------|------|------|
| **Main Session Agent** | 脑子:理解、管 task stack、决定 instant/interactive/background/blocked、回复 | 复用 codex-acp,但**降级为传输层 + 每轮组装调用**(见决定一) |
| **Agent OS Store** | 账本 / event inbox / checkpoint | **留** `store.py`,改造用途 |
| **Worker Pool** | 手:拿 Context Packet 执行工具、回 evidence | **留** launchd `agent-os-*-worker` 进程池 |
| **Tool Registry** | 工具箱:单次确定性能力 | **留** `ha-fast-*` / `tools/info/tools/info/weather.py` / `stock` / `amap` 等,登记成注册表 |
| **Memory Service** | 上下文管理器:检索 + 组装 + 卸载 | 新建,基于现有 `memory/` 目录 + grep |

一句话:**主脑是脑,Store 是账本/神经,Worker 是手,Tool 是工具,Memory 是长期经验。**

## 2. 主链路(最该先改的就是这条)

```text
user message (或 background 事件唤醒)
  ↓
drain event inbox            # 先看有没有后台结果/待办
  ↓
build main context           # Memory Service 组装,带 token 预算
  ↓
Main Session Agent decides   # 一次带组装上下文的全新调用 + turn 内 agent loop
  ↓
create task / reply / delegate
  ↓
worker executes Context Packet
  ↓
worker writes evidence event # 写回 Store inbox
  ↓
Main Agent later replies or 合并回复
```

## 3. 任务模型:按执行模式分,不按长短分

`mode` 由主脑判断(必要时调 probe),不靠静态规则:

| mode | 含义 | 例子 |
|------|------|------|
| `instant` | 1–10 秒直接答 | 寒暄、简单天气、门状态 |
| `interactive` | 用户正在等,需工具验证 | 查股票、查天气、发 Preply |
| `background` | 先启动,完成后通知 | 下视频、写网页/论文、全球股票大范围扫描、批量整理 |
| `blocked` | 缺信息/权限/需确认 | "哪个 Sophie?" |

Task schema(在 v1 基础上加 `mode/eta/confidence/user_waiting/interruptible/checkpointable/resources`):

```json
{
  "task_id": "t_001",
  "goal": "下载这个视频",
  "mode": "background",
  "eta_sec": 240,
  "confidence": 0.7,
  "user_waiting": false,
  "interruptible": true,
  "checkpointable": true,
  "resources": ["network", "disk"],
  "status": "running"
}
```

## 4. 四个一动手就会咬人的决定(本文件的核心)

### 决定一:主脑是「每轮重组」,不是「长命会话」

"持续主脑"和"每轮重新组装上下文"是矛盾的,**选后者**。连续感来自 Store + Memory,**不来自一个长命的 LLM 进程**。

- Main Agent = **每个 turn 一次「带组装好上下文」的全新调用**,不是累积历史的常驻会话。
- 好处:上下文爆窗问题**根本不存在**(每轮预算固定,自己控制塞多少)。
- `com.zhen.weixin-agent`(codex-acp 持久线程)**降级为纯传输层**(收/发微信),不再承担"记住"。记忆全在 Store/Memory。
- turn **内部**仍是真 agent loop(查 HA→读→决定→回复);turn **之间**不靠进程记忆。

### 决定二:background 回灌有两个唤醒源 + delivered 幂等

- **唤醒源 (a)**:收到用户消息 → drain inbox → 组装 → 决策。
- **唤醒源 (b)**:inbox 来了 `task.done` 且会话空闲 → **主动** compose 并用 `weixin-send.mjs` 推送。**(b) 不能漏**,否则后台任务是哑巴。
- 每个事件带 **`delivered` 标记**:assembler 会塞"最近 worker 结果",inbox 又有 `task.done`,不打标记会**就同一完成事件回两遍**。drain 后置 `delivered`,幂等。
- 关键纪律:background worker 完成后**绝不直接打断当前聊天**,只写 inbox 事件。

inbox 事件类型(在 v1 事件总线上扩展):

```text
task.done
task.failed
task.progress
task.needs_user
```

### 决定三:blocked / needs_user 的答案必须绑回 task,不能当新请求

worker 写 `task.needs_user`("哪个 Sophie?"),主脑问用户;用户的回答**以一条普通微信消息回来**。若主脑当成全新请求,blocked task 永远恢复不了。

- Store 新增 **`pending_question`** 状态,绑定 `task_id`。
- assembler 每轮把"你对 task X 还有一个没收到回答的问题"放进主脑上下文,主脑才知道这条消息是**在回答**而非新指令。

### 决定四:context 组装要有 token 预算 + 简单检索,别上向量库

主脑上下文 = 五块变长内容的组装,不设预算 assembler 自己会变慢/爆窗。

| 上下文块 | 来源 | 预算(起步值,可调) |
|------|------|------|
| 近 N 轮对话 | `memory/recent-context.md` | ~1500 tok |
| 当前 task stack | Store active/background/blocked | ~500 tok |
| event inbox 摘要 | Store 未 delivered 事件 | ~500 tok |
| 检索到的 memory | `memory/*` 关键词/recency grep | ~1500 tok |
| 最近 worker 结果 | Store 最近 evidence | ~500 tok |

- 检索**先用关键词/recency grep**(CLAUDE.md 既有那套),**别现在上 embedding**——个人 agent 量级用不上,先跑通。
- 组装器要**确定性**:固定顺序、固定预算、超预算就截断/摘要。

## 5. Probe:是工具,不是一层

不能每个任务都先 probe(常见路径平白加一次往返延迟)。逻辑:

```text
主脑一眼能判断  → 直接定 mode(天气/Preply/HA 不 probe)
主脑不确定且可能很久 → 调 probe(大文件下载、批量抓取、全球股票大范围扫描)
```

`eta_sec / confidence` 是 nice-to-have 元数据,**不是 gating stage**。

## 6. Context Packet(主脑 → worker 的契约)

worker **不能空跑**,必须收到:

```json
{
  "session_id": "zhen-main",
  "task_id": "t_001",
  "user_goal": "给 Sophie G. 发 Preply 消息",
  "recent_context": ["用户说原文是 Hi, this is from AI"],
  "relevant_memory": [
    "浏览器=已登录浏览器环境",
    "sosophia/Sophia 通常指 Sophie G."
  ],
  "tools": ["preply.send_message"],
  "verification": "必须确认 sent + confirmed",
  "reply_style": "微信中文简短"
}
```

worker 做完回 evidence 事件:

```json
{
  "status": "done",
  "result": "已发给 Sophie G.",
  "evidence": { "tool": "preply.send_message", "confirmed": true }
}
```

由 Main Agent 决定怎么把它落成给用户的回复。

## 7. Worker vs Tool:厘清,别造两个干同一件事的注册表

- **Tool** = 一次确定性能力(`tools/info/weather.py now` → 立即返回)。`instant/interactive` 任务,**主脑直接调工具,不开 worker**。
- **Worker** = 能托管长/后台任务生命周期的运行槽,内部可能调多个 tool + checkpoint。**只有 `background` 才需要 worker**。

## 8. 留 / 扔(对着现有代码)

| 现有 | v2 命运 |
|------|------|
| `store.py`(事件总线/task/folder/checkpoint/WAL) | **留**,改造成 Task Manager 账本 + 主脑 event inbox |
| codex-acp 持久会话 `com.zhen.weixin-agent` + `session-manager.py` | **留**,降级为传输层(决定一) |
| launchd `agent-os-*-worker` 进程池 | **留**,改造成"手" |
| `codex_task.py` / `claude_task.py` 一次性 exec | **留但降级**:对会话主循环是错的,对 **background job 是对的**;喂法从瘦 prompt 改成 Context Packet |
| `ha-fast-*` / `tools/info/tools/info/weather.py` / `stock` / `amap` 等脚本 | **留**,登记进 Tool Registry |
| `classifier.py`(每条消息先跑一次 LLM 分类) | **扔/并入主脑**:主脑有完整上下文,内联分类,省掉独立 router 那跳延迟和误路由 |
| `workers.py` 里"worker 跑完整 agent 自己决策" | **扔**:worker 收 Packet、调工具、回 evidence,不再自己重判业务 |

## 9. Store schema 增量(动手前钉死)

- `tasks`:加 `mode / eta_sec / confidence / user_waiting / interruptible / checkpointable / resources`。
- `events`:加 `delivered`(bool)。
- 新增 `pending_questions`:`(question_id, task_id, question, asked_at, answered_at)`,绑定 blocked task。
- inbox 事件类型扩展:`task.done / task.failed / task.progress / task.needs_user`。

## 10. 落地顺序(别七层一起上)

> 状态(2026-06):Step 1 ✅ / Step 2 ✅ / Step 3 ✅ 均已在 `agent_os/v2/` 旁路实现并验证;
> 影子 tap(`shadow_tap.py`)已就绪,待真实微信流量验证;之后才谈转正切流量。


1. **主脑 + Context Packet + 工具直调**:`instant/interactive` 全程跑通,先不碰 background。这一步就**根治上下文**,投入最小。链路 = drain(空) → build context(决定四) → 主脑每轮组装 + turn 内 agent loop → 直调 Tool Registry → 回复。
2. **background worker + event inbox 回灌**:复用 `store.py`。跑通"派后台任务 → 完成 → 下一轮 drain 通知 / 主动推送(决定二)"。补 `delivered`、`pending_question`(决定三)。
3. **probe 工具 + scheduler**:最后上。scheduler 起步就是 FIFO,等多 background 任务抢资源了再变聪明。

---

**核心结论**:v1 的 SQLite/event/task **不该扔**,它正好是 v2 的账本;要扔的是**"worker 自己当脑子"**这个模式。v2 = 持续主脑(每轮重组)+ Store 当账本/inbox + worker 拿 Context Packet 执行。
