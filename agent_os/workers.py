from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from .models import ClaimedTask, WORKER_AGENT, WORKER_CONTROL, WORKER_JOB
from .store import AgentOSStore


class WorkerError(RuntimeError):
    pass


class Worker:
    def __init__(self, *, store: AgentOSStore, root: Path, worker_type: str, worker_id: str):
        self.store = store
        self.root = root
        self.worker_type = worker_type
        self.worker_id = worker_id

    def run_once(self) -> bool:
        if self.worker_type == WORKER_AGENT and os.getenv("AGENT_OS_AGENT_CLAIM_ANY", "1") != "0":
            task = self.store.claim_next_any(self.worker_id)
        else:
            task = self.store.claim_next(self.worker_type, self.worker_id)
        if not task:
            return False
        self.execute_claimed(task)
        return True

    def run_task_once(self, task_id: str) -> bool:
        task = self.store.claim_task(task_id, self.worker_id)
        if not task:
            return False
        if task.worker_type != self.worker_type and not self._can_execute_task(task):
            self.store.fail_task(task, f"task worker_type is {task.worker_type}, not {self.worker_type}")
            return True
        self.execute_claimed(task)
        return True

    def execute_claimed(self, task: ClaimedTask) -> None:
        try:
            result_text = self.execute(task)
            self.store.complete_task(task, self.sanitize_wechat_reply(result_text, task))
        except Exception as exc:  # noqa: BLE001 - worker boundary records all failures.
            self.store.fail_task(task, str(exc))

    def loop(self, *, idle_sleep: float = 1.0, once: bool = False) -> None:
        while True:
            handled = self.run_once()
            if once:
                return
            if not handled:
                time.sleep(idle_sleep)

    def execute(self, task: ClaimedTask) -> str:
        if task.worker_type == WORKER_CONTROL:
            return self.execute_control(task)
        if task.worker_type == WORKER_JOB:
            return self.execute_job(task)
        if task.worker_type == WORKER_AGENT:
            return self.execute_agent(task)
        raise WorkerError(f"unknown worker_type: {task.worker_type}")

    def execute_control(self, task: ClaimedTask) -> str:
        self.store.record_step(task, "agent_lane", self._lane_step_payload(task, "control"))
        return self.run_backend_task(task, timeout=int(os.getenv("AGENT_OS_CONTROL_TIMEOUT", "180")))

    def execute_agent(self, task: ClaimedTask) -> str:
        self.store.record_step(task, "agent_lane", self._lane_step_payload(task, "agent"))
        return self.run_backend_task(task, timeout=int(os.getenv("AGENT_OS_AGENT_TIMEOUT", "180")))

    def execute_job(self, task: ClaimedTask) -> str:
        self.store.record_step(task, "agent_lane", self._lane_step_payload(task, "job"))
        return self.run_backend_task(task, timeout=int(os.getenv("AGENT_OS_JOB_TIMEOUT", "7200")))

    def run_backend_task(self, task: ClaimedTask, *, timeout: int) -> str:
        if self._backend_for_task(task) == "claude":
            return self.run_claude_task(task, timeout=timeout)
        return self.run_codex_task(task, timeout=timeout)

    def run_codex_task(self, task: ClaimedTask, *, timeout: int) -> str:
        prompt = self._agent_prompt(task)
        model = self._lane_env("MODEL", "", lane=task.worker_type)
        command = [
            str(self.root / "venv/bin/python"),
            "core/codex_task.py",
            "run",
            "--cwd",
            str(self.root),
            "--timeout",
            str(timeout),
            "--max-output-chars",
            "80000",
            "--json",
            "--",
            prompt,
        ]
        if model:
            command[3:3] = ["--model", model]
        tool_call_id = self.store.start_tool_call(
            task,
            tool="agent_backend.codex_task",
            args={
                "timeout": timeout,
                "model": model,
                "goal": task.goal,
                "assigned_agent_id": task.assigned_agent_id,
                "assigned_agent_role": task.assigned_agent_role,
                "assigned_backend": task.assigned_backend,
            },
            timeout_ms=timeout * 1000,
            idempotent=False,
        )
        proc = subprocess.run(
            command,
            cwd=self.root,
            text=True,
            capture_output=True,
            timeout=timeout + 10,
            env=self._env(),
        )
        payload = self._parse_json_output(proc.stdout)
        ok = proc.returncode == 0 and payload.get("ok", False)
        if not ok:
            error = self._backend_error(payload, proc.stderr, backend="codex")
            self.store.finish_tool_call(task, tool_call_id, ok=False, result=payload, error=error[:2000])
            raise WorkerError(error[:2000])
        trace = payload.get("trace") or {}
        self.store.finish_tool_call(
            task,
            tool_call_id,
            ok=True,
            result={
                "durationMs": payload.get("durationMs"),
                "result": payload.get("result"),
                "trace": trace,
                "toolUseCount": len(trace.get("toolUses") or []),
                "toolResultCount": len(trace.get("toolResults") or []),
            },
        )
        text = str(payload.get("result") or "").strip()
        return text or "已完成。"

    def run_claude_task(self, task: ClaimedTask, *, timeout: int) -> str:
        prompt = self._agent_prompt(task)
        model = self._lane_env("CLAUDE_MODEL", "claude-opus-4-8", lane=task.worker_type)
        effort = self._lane_env("EFFORT", "medium" if task.worker_type == WORKER_JOB else "low", lane=task.worker_type)
        command = [
            str(self.root / "venv/bin/python"),
            "core/claude_task.py",
            "run",
            "--cwd",
            str(self.root),
            "--timeout",
            str(timeout),
            "--output-format",
            "stream-json",
            "--max-output-chars",
            "80000",
            "--effort",
            effort,
            "--json",
            "--",
            prompt,
        ]
        if model:
            command[3:3] = ["--model", model]
        tool_call_id = self.store.start_tool_call(
            task,
            tool="agent_backend.claude_task",
            args={
                "timeout": timeout,
                "model": model,
                "effort": effort,
                "goal": task.goal,
                "assigned_agent_id": task.assigned_agent_id,
                "assigned_agent_role": task.assigned_agent_role,
                "assigned_backend": task.assigned_backend,
            },
            timeout_ms=timeout * 1000,
            idempotent=False,
        )
        proc = subprocess.run(
            command,
            cwd=self.root,
            text=True,
            capture_output=True,
            timeout=timeout + 10,
            env=self._env(),
        )
        payload = self._parse_json_output(proc.stdout)
        ok = proc.returncode == 0 and payload.get("ok", False)
        if not ok:
            error = self._backend_error(payload, proc.stderr, backend="claude")
            self.store.finish_tool_call(task, tool_call_id, ok=False, result=payload, error=error[:2000])
            raise WorkerError(error[:2000])
        trace = payload.get("trace") or {}
        self.store.finish_tool_call(
            task,
            tool_call_id,
            ok=True,
            result={
                "durationMs": payload.get("durationMs"),
                "result": payload.get("result"),
                "trace": trace,
                "toolUseCount": len(trace.get("toolUses") or []),
                "toolResultCount": len(trace.get("toolResults") or []),
            },
        )
        text = str(payload.get("result") or "").strip()
        return text or "已完成。"

    def _backend_for_task(self, task: ClaimedTask) -> str:
        assigned = (task.assigned_backend or "").strip().lower()
        if assigned in {"codex", "claude"}:
            return assigned
        forced = self._lane_env("BACKEND", os.getenv("AGENT_OS_BACKEND", "codex"), lane=task.worker_type).strip().lower()
        if forced in {"codex", "claude"}:
            if forced == "claude":
                return "claude"
            if self._explicit_claude_request(task.goal):
                return "claude"
            return "codex"
        return "codex"

    def _explicit_claude_request(self, text: str) -> bool:
        normalized = text.lower()
        return bool(
            re.search(
                r"(用|让|叫|请)\s*claude\s*(code)?\s*(写|做|改|生成|实现|跑|美化|检查|重构|修)"
                r"|claude\s*code\s*(写|做|改|生成|实现|跑|美化|检查|重构|修)",
                normalized,
                re.I,
            )
        )

    def sanitize_wechat_reply(self, text: str, task: ClaimedTask) -> str:
        reply = (text or "").strip()
        compact_goal = re.sub(r"\s+", "", task.goal.strip().lower())
        if not reply:
            return "已完成。"
        if compact_goal in {"在吗", "在不", "在不在", "人呢"} and re.search(r"(截断|完整指令|完整任务|看不到完整)", reply):
            return "在，我在。"
        if compact_goal in {"没事", "没事了"} and re.search(r"(用户说|无需|无须|不需要|无需任何操作)", reply):
            return "好，有事再叫我。"
        meta_prefix = re.compile(r"^\s*(用户|对方|你|他|她)?(说|表示|回复|输入|发来|的意思是)[：:\"“']?[^。\n]*[。,\n]\s*")
        if meta_prefix.search(reply) and re.search(r"(无需|无须|不需要|不用|无需任何操作|没有要执行)", reply):
            return "好，有事再叫我。"
        if re.match(r"^\s*用户说[\"“']?没事", reply):
            return "好，有事再叫我。"
        return reply

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["PATH"] = f"{self.root / 'node_modules/.bin'}:/opt/homebrew/bin:{env.get('PATH', '')}"
        env.setdefault("CODEX_HOME", "/Users/zhen/home-agent/.codex-weixin")
        env.setdefault("CODEX_SQLITE_HOME", "/Users/zhen/home-agent/.codex-weixin")
        return env

    def _parse_json_output(self, output: str) -> dict[str, Any]:
        text = output.strip()
        if not text:
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise WorkerError(f"tool returned non-json output: {text[:500]}") from exc

    def _can_execute_task(self, task: ClaimedTask) -> bool:
        return self.worker_type == WORKER_AGENT and os.getenv("AGENT_OS_AGENT_CLAIM_ANY", "1") != "0"

    def _lane_step_payload(self, task: ClaimedTask, lane: str) -> dict[str, Any]:
        return {
            "lane": lane,
            "intent": task.intent,
            "goal": task.goal,
            "preferred_role": task.preferred_role,
            "target_agent_id": task.target_agent_id,
            "assigned_agent_id": task.assigned_agent_id,
            "assigned_agent_role": task.assigned_agent_role,
            "assigned_backend": task.assigned_backend,
            "assigned_session_id": task.assigned_session_id,
            "worker_id": self.worker_id,
        }

    def _lane_env(self, suffix: str, default: str, *, lane: str | None = None) -> str:
        lane_name = (lane or self.worker_type).upper()
        return os.getenv(f"AGENT_OS_{lane_name}_{suffix}", os.getenv(f"AGENT_OS_{suffix}", default))

    def _backend_error(self, payload: dict[str, Any], stderr: str, *, backend: str) -> str:
        raw = str(payload.get("stderr") or payload.get("error") or stderr or "").strip()
        if payload.get("error") == "timeout" or "task timed out" in raw.lower():
            return f"{backend} agent timed out"
        if not raw:
            return "agent backend failed"
        first_line = raw.splitlines()[0].strip()
        if first_line.startswith("Reading additional input"):
            return "agent backend failed"
        return first_line[:500]

    def _agent_prompt(self, task: ClaimedTask) -> str:
        return (
            "你是 Zhen 的本地微信家庭助手 agent。所有任务都必须由你判断、执行、验证，再给最终回复；"
            "worker 只提供并发运行槽位，不替你做业务决定。\n\n"
            f"当前逻辑 agent：{task.assigned_agent_id or 'codex-general'}\n"
            f"agent role：{task.assigned_agent_role or task.preferred_role}\n"
            f"agent backend：{task.assigned_backend or 'codex'}\n"
            f"agent session：{task.assigned_session_id or task.folder_id}\n\n"
            "可用执行工具示例：\n"
            "- 家居状态：运行 `node scripts/ha-fast-status.mjs --json`，读取 Home Assistant 实时状态后再回答。\n"
            "- 家居控制：运行 `node scripts/ha-fast-control.mjs --json -- \"用户原话\"`，必须检查 JSON 里的 `verification.confirmed` 和目标实体状态；"
            "如果没有 confirmed，不要说已完成。\n"
            "- 天气：运行 `./venv/bin/python tools/info/weather.py now --city 城市` 或 `./venv/bin/python tools/info/weather.py forecast --city 城市 --days 3`。\n"
            "- 发文件/图片：由你自己定位并确认文件存在；优先运行 "
            "`./venv/bin/python core/local_file_tool.py resolve --query \"用户原话\" --json` 查候选路径，"
            "再用 `test -f /absolute/path` 验证选中的候选。不要直接枚举 Desktop 目录。"
            "最终回复里加入 `给你：[send_file:/absolute/path]`，不要把文件内容整段贴出来。\n"
            "- Preply/网页代办：优先使用已登录的 Agent Browser/浏览器环境。Preply 查课表运行 "
            "`node scripts/agent-browser-preply.mjs status`；列出联系人运行 "
            "`node scripts/agent-browser-preply.mjs list-tutors`；发消息运行 "
            "`node scripts/agent-browser-preply.mjs send-message --tutor \"老师名\" --message \"原文\"`，"
            "先检查 preview 的对象和原文；只有用户当前消息已明确要求给该对象发送该原文时，才用同一命令加 `--apply`，"
            "并检查 JSON status 是 `sent` 且 confirmed 为 true。"
            "不要临时手写长 Playwright 探测流程，不要用 `networkidle` 或 fullPage screenshot 等容易卡住的等待方式。\n"
            "- 网页/代码任务：先快速创建用户要求的文件，再用 `ls` 或必要的本地命令验证文件存在。"
            "如果用户指定桌面，就写到 `/Users/zhen/Desktop/`。\n"
            "- 论文/报告/长文等 job lane 任务：先创建可持续写入的文件，按章节/阶段写入并保存 checkpoint；"
            "最终回复给文件路径、完成内容摘要和下一步建议。\n"
            "- 其他实时信息：用可用工具、网页搜索或本地命令查证，不要凭空编。\n\n"
            "回复规则：\n"
            "- 最终回答要适合直接发微信，中文、简短。\n"
            "- 用户说“记下来/记住/以后...”或纠正你的叫法、偏好、操作习惯时，必须先写入合适的 `memory/` 文件再回复；"
            "不要把这种请求回答成话术建议或备选说法。\n"
            "- 写记忆时保存真实语义，不要把用户临时说法当成必须复述的口令；例如网页代办只需记住使用已登录的浏览器环境，"
            "回复时自然表达，不要包装成生硬的固定名称。\n"
            "- 工具或命令失败时，你有兜底责任：先换一种可验证的方法继续尝试；"
            "只有替代路径也不可用时，才简短说明失败原因和下一步需要什么。\n"
            "- 不要写“用户说/用户想/无需操作/任务内容被截断”这类内部判断口吻；要像微信聊天一样直接回复。\n"
            "- 对“在吗/在不/你好/没事/好的/谢谢”这类短口语，直接自然回应。\n"
            "- 不要展示分析过程、思考过程、调度判断或分类原因。\n"
            "- 不要暴露 token、密钥、内部路径或实现细节。\n"
            "- 不要说“我会去做”；你要先做完并验证，再回答结果。\n\n"
            f"调度 lane：{task.worker_type}\n"
            f"执行 worker：{self.worker_type}:{self.worker_id}\n"
            f"preferred role：{task.preferred_role}\n"
            f"target agent：{task.target_agent_id or ''}\n"
            f"assigned agent：{task.assigned_agent_id or ''}\n"
            f"intent：{task.intent}\n"
            f"用户任务：{task.goal}"
        )
