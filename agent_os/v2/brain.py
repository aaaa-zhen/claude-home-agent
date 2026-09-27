"""Main Session Agent — 主脑 (step 1).

决定一:主脑 = 每个 turn 一次「带组装好上下文」的全新调用,turn 内跑 agent loop。
连续感来自组装的上下文(Memory Service),不来自长命进程。

step 1 主脑直调 Tool Registry(codex exec 自带 bash/工具循环),不开 worker、不写 Store。
backend 复用现有 codex_task.run_codex(env/proxy/timeout 都已就绪)。
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import codex_task  # noqa: E402

from . import tool_registry  # noqa: E402

PERSONA = """你是 Zhen 的本地微信家庭助手。你在跟用户微信聊天,直接回答他。

最重要的一条:**你输出的每一个字都会原样发到用户微信。所以你只输出"要对用户说的话"本身。**
绝对不要输出任何对消息的分析、分类或元说明——比如不要回复"这是常识问题,不需要工具""casual chitchat, no tools needed""用户在闲聊"之类。
要用工具就**静默地用**,不要在回复里描述你是否用工具、怎么判断的。直接给答案/结果。

回复规则:
- 用户问知识/常识(比如"什么是 ethical non monogamy")→ 直接用自己的话回答,不需要任何工具。
- 用户问实时信息(天气、室温、行情、设备状态)→ 先静默调对应工具拿真实结果再答;别凭记忆说"不能"。
- **控制设备是动作,不是闲聊**:开关灯/空调、调温、调风、关设备等,必须真的调用 ha_control,并把意图整理成"清楚的中文指令"传进去(例:用户"cool set studio ac 24 is bit colder now"→ 传"把书房空调调到24度";不通顺/中英混杂的原话直接塞进去会 handled=false 解析失败)。只有返回里 verification.confirmed=true 才能说"已开/已调到X度";若 handled=false 或没 confirmed,就如实说没设置成功(可换清楚说法重试一次)。**绝不允许没核实就假报成功——宁可说"没调成"也不能假说"已调到"。**
- 短口语("在吗/你好/Yeah?/Where are you bro/谢谢")→ 自然口语回应一句即可。
- 用户说"记下来/记住/以后…"或纠正你 → 先写对应 memory/ 文件,再自然回复。
- 风格:适合微信,简短口语;用户用中文就中文、用英文就英文;不要 markdown/列表/emoji(除非必要)。
- 上下文里若有「后台事件」区且有未告知的后台结果 → 这轮主动把结果自然带给用户。
- 不暴露 token、密钥、内部路径、实现细节;涉及当前时间先 `date`。"""


FEWSHOT = """示范(← 左边是用户消息,→ 右边是你该输出的、原样发给用户的话):
- 卖5800 → 5800 这价位还行,关键验机:能退 Apple ID、无监管锁、电池循环现场核对,没问题就值得入。
- 这 → 哈哈你又发个"这",到底想说啥?
- What is ethical non monogamy → Ethical non-monogamy 就是在所有人知情同意下,同时和不止一个人保持亲密关系,核心是诚实和同意,不是出轨。
- Where are you bro → Just at home bro, what's up?

反面(绝对不要这样输出 —— 这是把内部判断当回复了):
- ✗ "这是接着卖东西的对话,用户说卖5800。我直接自然回应即可。"
- ✗ "The user just sent '这' — an incomplete message."
- ✗ "no tools needed" / "casual chitchat"
"""


def _media_block(media: dict | None) -> str:
    """用户随消息发来的图片/文件 → 指示主脑先看再答(claude_task 的 Read 工具能直接看图)。"""
    if not media or not media.get("path"):
        return ""
    kind = media.get("type") or "image"
    path = media["path"]
    if kind == "image":
        return (
            "\n\n# 用户随这条消息发来一张图片(必须先看再答)\n"
            f"本地绝对路径(已存在,可直接读):{path}\n"
            "先用 Read 工具打开这张图片看清内容(可能是聊天截图、照片、表格或文档),"
            "再据此回答。如果是聊天截图、用户想要回复建议:先读懂截图里到底谁在跟谁聊、聊到哪、"
            "最后一句是谁说的,再给贴合上下文的回复,别脱离截图乱猜。"
        )
    if kind in ("video", "audio"):
        return (
            f"\n\n# 用户随这条消息发来一段{'视频' if kind == 'video' else '语音'}\n"
            f"本地绝对路径:{path}\n"
            "需要时用合适的命令行工具(ffmpeg/ffprobe 等)处理或转写它,再回答用户。"
        )
    return (
        "\n\n# 用户随这条消息发来一个文件\n"
        f"本地绝对路径:{path}\n"
        "需要时先读取/处理它,再回答用户。"
    )


def build_prompt(message: str, context_render: str, media: dict | None = None) -> str:
    return (
        f"{PERSONA}\n\n"
        f"{FEWSHOT}\n\n"
        f"{tool_registry.render()}\n\n"
        f"# 已为你组装的上下文\n{context_render}"
        f"{_media_block(media)}\n\n"
        f"# 用户刚发来的消息\n{message}\n\n"
        f"现在只输出你要发给用户的那句话本身(微信原样发出)。不要复述消息、不要分析、不要解释你要不要用工具。"
    )


def run_prompt(prompt: str, *, timeout: int = 180, model: str = "") -> dict:
    """按 V2_BRAIN_BACKEND 跑一段 prompt(claude / codex 可切换)。返回结构一致。"""
    timeout = int(os.getenv("V2_BRAIN_TIMEOUT", str(timeout)))
    backend = os.getenv("V2_BRAIN_BACKEND", "claude").lower()  # 现实默认 claude;env 仍可覆盖成 codex
    if backend == "claude":
        import claude_task  # noqa: PLC0415
        return claude_task.run_claude(
            prompt=prompt, cwd=ROOT, timeout=timeout,
            model=model or os.getenv("V2_BRAIN_CLAUDE_MODEL", "sonnet"),
            effort=os.getenv("V2_BRAIN_EFFORT", "medium"),
            permission_mode=os.getenv("V2_BRAIN_PERMISSION", "bypassPermissions"),
            max_budget_usd="", max_output_chars=80000, add_dirs=[],
            output_format="text", verbose=False,
        )
    return codex_task.run_codex(
        prompt=prompt, cwd=ROOT, timeout=timeout,
        model=model or os.getenv("V2_BRAIN_MODEL", ""),
        sandbox=os.getenv("V2_BRAIN_SANDBOX", "danger-full-access"),
        max_output_chars=80000, add_dirs=[],
    )


# 元说明特征:把"对消息的分析/旁白"当回复吐出来的典型句式
_META = re.compile(
    r"这是.{0,8}(对话|消息|聊天|场景|问题)"
    r"|用户(说|刚|发来|想|在|的意思|只是)"
    r"|我(直接|就|这边)?.{0,4}(自然)?回(应|复)即可"
    r"|无需(工具|调用|任何操作)|不需要(工具|调用)"
    r"|no tools needed|casual chitchat|general knowledge question"
    r"|\bThe user (just )?(said|sent|wants|is|asked|message)"
    r"|^This is (just )?a ",
    re.I,
)


def _looks_like_meta(text: str) -> bool:
    t = (text or "").strip()
    return bool(t) and len(t) < 240 and bool(_META.search(t))


def run(message: str, context_render: str, *, timeout: int = 180, model: str = "",
        media: dict | None = None) -> dict:
    result = run_prompt(build_prompt(message, context_render, media), timeout=timeout, model=model)
    # 兜底:若主脑把内部判断当回复吐出来了,用更狠的指令重跑一次
    if result.get("ok") and _looks_like_meta(result.get("result", "")):
        strict = (
            "你上一次错误地输出了对消息的分析/旁白,而不是回复用户本身。\n"
            "现在【只】输出要原样发到微信的那一句话:中文或英文的真实回复,"
            "绝不能出现'用户说/这是…的对话/我回应即可/no tools needed'这类字眼。\n\n"
            + build_prompt(message, context_render, media)
        )
        retry = run_prompt(strict, timeout=timeout, model=model)
        if retry.get("ok") and retry.get("result", "").strip():
            return retry
    return result
