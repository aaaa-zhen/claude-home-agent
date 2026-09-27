from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from .models import (
    Classification,
    PRIORITY_FILE_MEDIA,
    PRIORITY_HOME_CONTROL,
    PRIORITY_HOME_STATUS,
    PRIORITY_LONG_JOB,
    PRIORITY_SHORT_QA,
    WORKER_AGENT,
    WORKER_CONTROL,
    WORKER_JOB,
)


def classify_message(text: str) -> Classification:
    routed = classify_message_with_router_agent(text)
    if routed:
        return routed
    return classify_message_with_local_rules(text)


def classify_message_with_router_agent(text: str) -> Classification | None:
    prompt = ROUTER_PROMPT.format(text=(text or "").strip())
    root = Path(__file__).resolve().parents[1]
    timeout = int(os.getenv("AGENT_OS_ROUTER_TIMEOUT", "45"))
    command = [
        sys.executable,
        str(root / "core" / "codex_task.py"),
        "run",
        "--cwd",
        str(root),
        "--timeout",
        str(timeout),
        "--max-output-chars",
        "12000",
        "--json",
        "--",
        prompt,
    ]
    model = os.getenv("AGENT_OS_ROUTER_MODEL", os.getenv("AGENT_OS_MODEL", ""))
    if model:
        command[3:3] = ["--model", model]
    try:
        proc = subprocess.run(
            command,
            cwd=root,
            text=True,
            capture_output=True,
            timeout=timeout + 5,
            env=_router_env(root),
        )
        if proc.returncode != 0:
            return None
        payload = json.loads(proc.stdout)
        result = str(payload.get("result") or "").strip()
        data = _parse_router_json(result)
        return _classification_from_router_data(data)
    except Exception:  # noqa: BLE001 - route fallback must not break message intake.
        return None


def _router_env(root: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["PATH"] = f"{root / 'node_modules/.bin'}:/opt/homebrew/bin:{env.get('PATH', '')}"
    return env


def _parse_router_json(result: str) -> dict[str, object]:
    try:
        return json.loads(result)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", result, re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


def _classification_from_router_data(data: dict[str, object]) -> Classification | None:
    worker_type = str(data.get("worker_type") or "").strip()
    intent = str(data.get("intent") or "").strip() or "short_qa"
    if worker_type not in {WORKER_CONTROL, WORKER_AGENT, WORKER_JOB}:
        return None
    try:
        priority = int(data.get("priority") or _default_priority(worker_type, intent))
    except (TypeError, ValueError):
        priority = _default_priority(worker_type, intent)
    priority = max(1, min(100, priority))
    preferred_role = str(data.get("preferred_role") or _default_role(intent, worker_type)).strip() or "general"
    if preferred_role not in {"general", "home", "weather", "file", "browser", "research", "coder", "claude_ui"}:
        preferred_role = _default_role(intent, worker_type)
    target_agent_id_raw = str(data.get("target_agent_id") or "").strip()
    target_agent_id = target_agent_id_raw or None
    return Classification(intent, worker_type, priority, preferred_role=preferred_role, target_agent_id=target_agent_id)


def classify_message_with_local_rules(text: str) -> Classification:
    """Deterministic fallback used when the router agent is unavailable."""
    compact = re.sub(r"\s+", "", (text or "").strip().lower())
    if not compact:
        return Classification("short_qa", WORKER_AGENT, PRIORITY_SHORT_QA, preferred_role="general")

    if _looks_like_home(compact):
        intent = "home_status" if _looks_like_home_status(compact) else "home_control"
        return Classification(intent, WORKER_CONTROL, _default_priority(WORKER_CONTROL, intent), preferred_role="home")

    if re.search(r"天气|下雨|气温|降温|台风|空气质量|穿什么|外面.*温度|今天.*温度|明天.*温度", compact):
        return Classification("weather", WORKER_AGENT, PRIORITY_SHORT_QA, preferred_role="weather")

    if re.search(r"preply|老师|课表|发消息|联系人|浏览器|网页登录|网页代办", compact):
        return Classification("browser_action", WORKER_AGENT, PRIORITY_SHORT_QA, preferred_role="browser")

    if re.search(r"照片|图片|截图|视频|文件|附件|录音|语音|发给我|传给我|找一下", compact):
        return Classification("media_or_file", WORKER_AGENT, PRIORITY_FILE_MEDIA, preferred_role="file")

    if re.search(r"写|做|生成|实现|改造|修复|项目|网页|网站|app|代码|脚本|测试|构建|发布|预览", compact):
        role = "claude_ui" if "claude" in compact else "coder"
        return Classification("long_job", WORKER_JOB, PRIORITY_LONG_JOB, preferred_role=role)

    if re.search(r"论文|报告|调研|研究一下|整理|总结|长文|方案|规划", compact):
        return Classification("long_job", WORKER_JOB, PRIORITY_LONG_JOB, preferred_role="research")

    return Classification("short_qa", WORKER_AGENT, PRIORITY_SHORT_QA, preferred_role="general")


def _looks_like_home(compact: str) -> bool:
    return bool(re.search(r"空调|灯|窗帘|门|室温|家里.*温度|客厅.*温度|卧室.*温度|书房.*温度|设备|homeassistant|homepod|音响|电视|插座", compact))


def _looks_like_home_status(compact: str) -> bool:
    if re.search(r"开没开|关没关|开着|关着|状态|哪些|什么|多少度|室温|温度|吗|么|？|\?", compact):
        command_prefix = re.search(r"^(帮我|给我|请|把)?(打开|开启|开一下|关闭|关掉|关一下|调到|设到|设置|设为|切到)", compact)
        command_pattern = re.search(r"(帮我|给我|请|把).*(打开|开启|关闭|关掉|调到|设到|设置|设为|切到)", compact)
        return not command_prefix and not command_pattern
    return False


def _default_priority(worker_type: str, intent: str) -> int:
    if worker_type == WORKER_CONTROL:
        return PRIORITY_HOME_CONTROL if intent == "home_control" else PRIORITY_HOME_STATUS
    if worker_type == WORKER_JOB:
        return PRIORITY_FILE_MEDIA if intent == "media_or_file" else PRIORITY_LONG_JOB
    return PRIORITY_SHORT_QA


def _default_role(intent: str, worker_type: str) -> str:
    if intent in {"home_control", "home_status"} or worker_type == WORKER_CONTROL:
        return "home"
    if intent == "weather":
        return "weather"
    if intent == "media_or_file":
        return "file"
    if intent == "browser_action":
        return "browser"
    if intent == "long_job":
        return "research"
    return "general"


ROUTER_PROMPT = """你是 Agent OS 的 Router Agent，只负责调度分类，不执行用户任务。

你必须根据用户真实意图、预期耗时、是否需要持续产出/多步骤/checkpoint、是否需要控制设备来判断。
不要因为用户口头说“这是长任务/短任务”就盲从；要按任务内容判断。
注意：worker_type 只是 lane/priority/timeout 标签，不代表串行队列；只要有空闲 worker，任何新任务都可以并发启动。

worker_type 只能选：
- control：家居设备控制/状态查询，例如灯、空调、门、Home Assistant 状态。需要高优先级。
- agent：短问答、天气、行情查询、旅行信息、简单查证、本地小文件发送、网页登录态代办等，一般几十秒内完成。
- job：论文、报告、长文、网页/代码项目、复杂文件处理、持续写作、预计超过 5-10 分钟或需要 checkpoint 的任务。

priority 建议：
- control: 80-95
- agent: 50-70
- job: 10-30

preferred_role 只能选：
- general：普通问答、小任务、兜底
- home：家居控制/状态
- weather：天气/实时查询
- file：文件、图片、视频、附件定位/发送
- browser：网页代办、需要使用已登录浏览器环境的任务，例如 Preply 发消息/查课表、登录态网页操作
- research：长文、论文、报告、调研、持续产出
- coder：代码、项目、网页、自动化开发
- claude_ui：只有用户明确要求 Claude Code 做 UI/网页/视觉打磨时才选

target_agent_id 默认 null；只有用户明确要求固定某个 agent，或系统上下文已经给出目标 agent 时才填。

只输出一行 JSON，不要 Markdown，不要解释，不要调用工具。
格式：
{{"intent":"home_control|home_status|weather|short_qa|media_or_file|browser_action|long_job","worker_type":"control|agent|job","priority":60,"preferred_role":"general|home|weather|file|browser|research|coder|claude_ui","target_agent_id":null,"expected_duration":"short|medium|long","reason":"一句话原因"}}

用户消息：
{text}
"""
