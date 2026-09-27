# Agent OS v1

这是当前微信家庭助手的第一版事件驱动编排层。它先以本机 SQLite WAL 实现 Event Bus / Task Store / Folder Runtime，保留替换成 Redis Streams 或 NATS 的边界。

## 目标

- 短任务和长任务隔离
- 家居控制高优先级执行
- 每个用户请求都有独立 Task / Folder / Run
- 工具调用、结果和失败可追踪
- 回复通过 Response Outbox/Event 生成，Gateway 不执行业务逻辑
- Worker Pool 可按类型扩展

## 当前架构

```text
WeChat UI
  ↓
weixin-acp Gateway
  ↓
Agent OS Scheduler
  ↓
SQLite Event Bus + Task/Folder/Response Store
  ↓
Worker Pool
  ├─ control-worker → narrow Home Assistant tool executor
  ├─ agent-worker   → one-shot agent backend
  └─ job-worker     → code/project/long jobs
  ↓
Tool Layer
  ↓
State / Events / Response Outbox
```

## 模块

```text
agent_os/
  classifier.py   intent + priority + worker routing
  scheduler.py    Task/Run creation facade
  store.py        SQLite event bus and durable state
  workers.py      worker pool execution backends
  cli.py          local gateway for submit/status/work
```

## 事件类型

当前已经落库的事件：

```text
task.created
task.queued
task.started
folder.created
folder.planned
folder.running
folder.step
folder.completed
tool.called
tool.succeeded
tool.failed
response.created
response.sent
task.completed
task.failed
```

事件结构：

```json
{
  "event_id": "e_xxx",
  "type": "task.created",
  "task_id": "t_xxx",
  "folder_id": "f_xxx",
  "run_id": "r_xxx",
  "priority": 90,
  "source": "wechat",
  "payload": {},
  "created_at": "2026-06-25T08:00:00.000Z"
}
```

## Task / Folder / Run / Response

Task 是用户请求的调度单元：

```json
{
  "task_id": "t_xxx",
  "folder_id": "f_xxx",
  "run_id": "r_xxx",
  "goal": "关掉客厅灯",
  "intent": "home_control",
  "worker_type": "control",
  "priority": 90,
  "status": "queued"
}
```

Folder 是执行单元，类似一个可暂停/恢复的 process：

```json
{
  "folder_id": "f_xxx",
  "task_id": "t_xxx",
  "run_id": "r_xxx",
  "state": "created | planned | running | blocked | completed | compressed | archived",
  "context_snapshot": {},
  "memory_buffer": []
}
```

Run 是一次执行尝试：

```json
{
  "run_id": "r_xxx",
  "folder_id": "f_xxx",
  "task_id": "t_xxx",
  "status": "running",
  "steps": [],
  "tool_calls": [],
  "artifacts": [],
  "memory_buffer": []
}
```

Response 是 Gateway 可发送的 outbox 行：

```json
{
  "response_id": "resp_xxx",
  "task_id": "t_xxx",
  "folder_id": "f_xxx",
  "channel": "wechat",
  "text": "已完成。",
  "status": "created | sent"
}
```

## 优先级

```text
90  home_control
80  home_status
60  short_qa
40  media_or_file
20  long_job
10  background
```

当前 v1 是“优先级队列 + worker 类型隔离”，不是强制中断正在运行的 LLM/tool call。真正 preemption 后续需要可取消 tool call、可恢复 Run，以及 worker 心跳。

注意：`control / agent / job` 是调度 lane，不是业务执行者。用户任务都由 agent backend 执行；control lane 只是给家居类任务更高优先级和独立并发槽位。家居、天气等本地脚本只能作为 agent 自己选择调用的工具，不能作为绕过 agent 的 shortcut。

## 使用

初始化：

```bash
./venv/bin/python -m agent_os init
```

分类：

```bash
./venv/bin/python -m agent_os classify "把客厅灯关掉"
```

提交任务：

```bash
./venv/bin/python -m agent_os submit "把客厅灯关掉" --json
```

Gateway 入口：只创建 event/task/folder，可选等待 response：

```bash
./venv/bin/python -m agent_os gateway "家里什么设备开着呢" --json
./venv/bin/python -m agent_os gateway "家里什么设备开着呢" --inline --wait --json
```

运行 control lane worker 一次：

```bash
./venv/bin/python -m agent_os work --worker control --once
```

提交并内联执行：

```bash
./venv/bin/python -m agent_os run "家里什么设备开着呢" --json
```

查看任务和事件：

```bash
./venv/bin/python -m agent_os list --json
./venv/bin/python -m agent_os folders --json
./venv/bin/python -m agent_os responses --json
./venv/bin/python -m agent_os events --task-id t_xxx --json
./venv/bin/python -m agent_os folder f_xxx --events --json
./venv/bin/python -m agent_os cancel t_xxx --reason "not needed"
```

兼容测试命令：

```bash
./venv/bin/python -m agent_os wechat-control "把客厅灯关掉"
```

这个命令只用于本地测试 `control-worker` 支持的家居状态/控制消息。当前微信入口不直接使用它；正式入口应使用 `gateway` 创建 `user.message -> task/folder/run -> response`。

查看 agent 实际执行证据：

```bash
./venv/bin/python -m agent_os trace t_xxx --json
```

`trace` 会显示 agent backend 的 Claude Code stream，包括 Bash/Web/tool use 和 tool result。比如天气任务应能看到 Claude agent 调用 `tools/info/weather.py`，家居任务应能看到 Claude agent 调用 Home Assistant 工具并读回验证结果。

安装常驻 control worker：

```bash
npm run agentos:install-control-worker
```

## 迁移边界

当前状态：

- Agent OS v1 的 Event/Task/Folder/Run/Response/Worker 基础设施已可运行。
- 直接微信家居旁路已禁用，避免绕过 agent 记忆、工具调用记录和验证造成误控。
- 微信入口已可通过 Agent OS Gateway 进入 `task/folder/run/response`。
- `control-worker` 可作为受控工具执行器保留，但不应作为默认语义决策者。

后续迁移建议：

1. 将微信入口改成只创建 Task，不直接调用控制工具。
2. 为每个 Task 启动独立 agent worker，使多任务并行但仍保留 Agent 规则、记忆和上下文。
3. control-worker 只执行 agent worker 生成的明确 tool plan，并负责状态验证。
4. 长任务完成后通过现有微信发送能力主动回消息。

这样可以先让家居控制和状态查询不再被长任务阻塞，再逐步迁移普通问答和项目任务。
