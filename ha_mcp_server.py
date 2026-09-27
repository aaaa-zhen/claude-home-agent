#!/usr/bin/env python3
"""Home Assistant MCP Server - provides guarded HA tools to Claude Code."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP, Image

SCRIPT_DIR = Path(__file__).resolve().parent
load_dotenv(SCRIPT_DIR / ".env")

HA_URL = os.environ["HA_URL"]
HA_TOKEN = os.environ["HA_TOKEN"]
HEADERS = {
    "Authorization": f"Bearer {HA_TOKEN}",
    "Content-Type": "application/json",
}

DEFAULT_CONFIG = {
    "homePod": {
        "entityId": "media_player.living_room",
        "ttsProvider": "openai",
        "defaultVoice": "nova",
        "openaiModel": "gpt-4o-mini-tts",
        "openaiInstructions": (
            "请使用自然、温暖的普通话，像家里人当面提醒一样；语速稍慢，停顿自然，"
            "不要使用新闻播音腔，也不要添加输入文字以外的内容。"
        ),
        "edgeVoice": "zh-CN-XiaoxiaoNeural",
    },
    "livingRoomCamera": {
        "defaultPreset": "餐桌",
    },
    "haMcp": {
        "allowedServices": {
            "light": ["turn_on", "turn_off", "toggle"],
            "switch": ["turn_on", "turn_off", "toggle"],
            "climate": ["turn_on", "turn_off", "set_temperature", "set_hvac_mode", "set_fan_mode"],
            "fan": ["turn_on", "turn_off", "toggle", "set_percentage"],
            "cover": ["open_cover", "close_cover", "stop_cover", "set_cover_position"],
        },
        "readEntityPrefixes": ["person.", "sensor.", "binary_sensor.", "climate.", "switch.", "light.", "fan.", "cover."],
        "serviceEntityPrefixes": ["climate.", "switch.", "light.", "fan.", "cover."],
    }
}


def load_config():
    path = SCRIPT_DIR / "config.json"
    if not path.exists():
        return DEFAULT_CONFIG
    try:
        with path.open("r", encoding="utf-8") as f:
            user = json.load(f)
    except Exception:
        return DEFAULT_CONFIG
    merged = json.loads(json.dumps(DEFAULT_CONFIG))
    merged.setdefault("homePod", {}).update(user.get("homePod", {}))
    merged.setdefault("livingRoomCamera", {}).update(user.get("livingRoomCamera", {}))
    merged.setdefault("haMcp", {}).update(user.get("haMcp", {}))
    return merged


APP_CONFIG = load_config()
CONFIG = APP_CONFIG.get("haMcp", {})
HOMEPOD_CONFIG = APP_CONFIG.get("homePod", {})
CAMERA_CONFIG = APP_CONFIG.get("livingRoomCamera", {})
ALLOWED_SERVICES = {
    domain: set(services)
    for domain, services in (CONFIG.get("allowedServices") or {}).items()
}
READ_ENTITY_PREFIXES = tuple(CONFIG.get("readEntityPrefixes") or [])
SERVICE_ENTITY_PREFIXES = tuple(CONFIG.get("serviceEntityPrefixes") or [])

mcp = FastMCP("homeassistant")
HOMEPOD_SCRIPT = SCRIPT_DIR / "scripts" / "homepod-say.py"
HOMEPOD_ENTITY = HOMEPOD_CONFIG.get("entityId", "media_player.living_room")
HOMEPOD_TTS_PROVIDER = HOMEPOD_CONFIG.get("ttsProvider", "openai")
HOMEPOD_DEFAULT_VOICE = HOMEPOD_CONFIG.get("defaultVoice", "nova")
HOMEPOD_OPENAI_MODEL = HOMEPOD_CONFIG.get("openaiModel", "gpt-4o-mini-tts")
HOMEPOD_OPENAI_INSTRUCTIONS = HOMEPOD_CONFIG.get("openaiInstructions", "")
HOMEPOD_EDGE_VOICE = HOMEPOD_CONFIG.get("edgeVoice", "zh-CN-XiaoxiaoNeural")
CAMERA_SCRIPT = SCRIPT_DIR / "scripts" / "camera-ptz.py"
CAMERA_PRESETS_FILE = SCRIPT_DIR / "memory" / "camera-presets.json"
CAMERA_SNAP_DIR = SCRIPT_DIR / "tmp" / "camera-snaps"
CAMERA_DEFAULT_PRESET = CAMERA_CONFIG.get("defaultPreset", "餐桌")


def _ha_get(path: str) -> dict | list:
    with httpx.Client(timeout=15, trust_env=False) as client:
        response = client.get(f"{HA_URL}{path}", headers=HEADERS)
    response.raise_for_status()
    return response.json()


def _ha_post(path: str, data: dict | None = None) -> dict | list:
    with httpx.Client(timeout=15, trust_env=False) as client:
        response = client.post(f"{HA_URL}{path}", headers=HEADERS, json=data or {})
    response.raise_for_status()
    return response.json()


def _entity_prefix_allowed(entity_id: str, prefixes: tuple[str, ...]) -> bool:
    return bool(entity_id) and any(entity_id.startswith(prefix) for prefix in prefixes)


def _validate_read_entity(entity_id: str):
    if READ_ENTITY_PREFIXES and not _entity_prefix_allowed(entity_id, READ_ENTITY_PREFIXES):
        raise ValueError(f"Reading entity {entity_id!r} is not allowed by config.json")


def _validate_service_call(domain: str, service: str, entity_id: str):
    allowed = ALLOWED_SERVICES.get(domain)
    if not allowed or service not in allowed:
        raise ValueError(f"Service {domain}.{service} is not allowed by config.json")
    if SERVICE_ENTITY_PREFIXES and not _entity_prefix_allowed(entity_id, SERVICE_ENTITY_PREFIXES):
        raise ValueError(f"Service calls for entity {entity_id!r} are not allowed by config.json")


@mcp.tool()
def ha_get_state(entity_id: str) -> str:
    """获取 HA 实体状态。返回 state + attributes JSON。"""
    _validate_read_entity(entity_id)
    result = _ha_get(f"/states/{entity_id}")
    return json.dumps({
        "entity_id": result["entity_id"],
        "state": result["state"],
        "attributes": result["attributes"],
        "last_changed": result["last_changed"],
    }, ensure_ascii=False)


@mcp.tool()
def ha_call_service(domain: str, service: str, entity_id: str, data: str = "{}") -> str:
    """调用允许列表内的 HA 服务。data 是 JSON 字符串，包含额外参数。"""
    _validate_service_call(domain, service, entity_id)
    payload = json.loads(data)
    payload["entity_id"] = entity_id
    result = _ha_post(f"/services/{domain}/{service}", payload)
    return json.dumps({"ok": True, "result_count": len(result)}, ensure_ascii=False)


@mcp.tool()
def ha_list_entities(domain_filter: str = "") -> str:
    """列出 HA 实体。可选 domain_filter 过滤，如 climate/switch/sensor。"""
    states = _ha_get("/states")
    if domain_filter:
        states = [state for state in states if state["entity_id"].startswith(domain_filter + ".")]
    if READ_ENTITY_PREFIXES:
        states = [state for state in states if _entity_prefix_allowed(state["entity_id"], READ_ENTITY_PREFIXES)]
    return json.dumps([
        {
            "entity_id": state["entity_id"],
            "state": state["state"],
            "name": state["attributes"].get("friendly_name", ""),
        }
        for state in states
    ], ensure_ascii=False)


@mcp.tool()
def homepod_status() -> str:
    """查询客厅 HomePod 的状态、音量和当前媒体。"""
    result = _ha_get(f"/states/{HOMEPOD_ENTITY}")
    attributes = result.get("attributes") or {}
    return json.dumps({
        "entity_id": HOMEPOD_ENTITY,
        "name": attributes.get("friendly_name", "HomePod"),
        "state": result.get("state"),
        "volume_level": attributes.get("volume_level"),
        "media_title": attributes.get("media_title"),
        "last_changed": result.get("last_changed"),
    }, ensure_ascii=False)


@mcp.tool()
def homepod_say(message: str, voice: str = "", volume: float | None = None) -> str:
    """让客厅 HomePod 播放一条不超过 500 字的短语音。只用于说话/播报，不用于搜索歌曲。"""
    message = message.strip()
    if not message:
        raise ValueError("message 不能为空")
    if len(message) > 500:
        raise ValueError("message 不能超过 500 字")
    selected_voice = voice or HOMEPOD_DEFAULT_VOICE
    if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", selected_voice):
        raise ValueError("voice 格式不合法")
    if volume is not None and not 0 <= volume <= 1:
        raise ValueError("volume 必须在 0 到 1 之间")

    command = [sys.executable, str(HOMEPOD_SCRIPT), message, "--voice", selected_voice]
    if volume is not None:
        command.extend(["--volume", str(volume)])
    env = os.environ.copy()
    env["HOMEPOD_ENTITY"] = HOMEPOD_ENTITY
    env["HOMEPOD_TTS_PROVIDER"] = HOMEPOD_TTS_PROVIDER
    env["HOMEPOD_OPENAI_MODEL"] = HOMEPOD_OPENAI_MODEL
    env["HOMEPOD_OPENAI_VOICE"] = HOMEPOD_DEFAULT_VOICE
    env["HOMEPOD_OPENAI_INSTRUCTIONS"] = HOMEPOD_OPENAI_INSTRUCTIONS
    env["HOMEPOD_EDGE_VOICE"] = HOMEPOD_EDGE_VOICE
    try:
        completed = subprocess.run(
            command,
            cwd=SCRIPT_DIR,
            env=env,
            capture_output=True,
            text=True,
            timeout=240,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("HomePod 发声超时") from exc

    output = completed.stdout if completed.returncode == 0 else completed.stderr
    lines = [line for line in output.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError("HomePod 发声工具没有返回结果")
    try:
        payload = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise RuntimeError("HomePod 发声工具返回了无效结果") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("HomePod 发声工具返回了无效结果")
    if completed.returncode != 0 or not payload.get("ok"):
        raise RuntimeError(payload.get("error") or "HomePod 发声失败")
    return json.dumps(payload, ensure_ascii=False)


@mcp.tool()
def living_room_camera_snapshot(position: str = ""):
    """抓取一张客厅摄像头图片。position 可留空，或使用已登记机位如餐桌、沙发、门口。
    返回图片和原图文件路径；用户要原图时必须用本次返回的路径 [send_file:路径]，不要复用旧路径。"""
    position = position.strip()
    try:
        presets = json.loads(CAMERA_PRESETS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("摄像头机位配置不可用") from exc
    if not isinstance(presets, dict):
        raise RuntimeError("摄像头机位配置不可用")
    if position and position not in presets:
        raise ValueError(f"未知摄像头机位：{position}")

    command = [sys.executable, str(CAMERA_SCRIPT), "snap"]
    should_restore = bool(position and position != CAMERA_DEFAULT_PRESET)
    if position:
        command = [sys.executable, str(CAMERA_SCRIPT), "look", "--name", position]
    try:
        completed = subprocess.run(
            command,
            cwd=SCRIPT_DIR,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("客厅摄像头抓图超时") from exc
    finally:
        if should_restore:
            try:
                subprocess.run(
                    [
                        sys.executable,
                        str(CAMERA_SCRIPT),
                        "goto",
                        "--name",
                        CAMERA_DEFAULT_PRESET,
                    ],
                    cwd=SCRIPT_DIR,
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                pass

    if completed.returncode != 0:
        raise RuntimeError("客厅摄像头抓图失败")
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError("客厅摄像头没有返回图片")
    image_path = Path(lines[-1]).resolve()
    try:
        image_path.relative_to(CAMERA_SNAP_DIR.resolve())
    except ValueError as exc:
        raise RuntimeError("客厅摄像头返回了不安全的图片路径") from exc
    if not image_path.is_file():
        raise RuntimeError("客厅摄像头图片不存在")
    image_data = image_path.read_bytes()
    # 原图保留在 tmp/camera-snaps（cleanup-tmp.sh 7 天后清理），
    # 这样用户说"发原图"时才有确切文件可发。2026-08-13 之前是看完即删，
    # 导致 agent 只能翻旧文件/旧路径，把上次拍的甚至无关图片发出去。
    return [
        Image(data=image_data, format="jpeg"),
        f"原图路径：{image_path}（本次刚拍的；要发给用户就用 [send_file:{image_path}]，"
        "别用之前轮次的路径或 tmp/camera-snaps 里的旧文件）",
    ]


if __name__ == "__main__":
    mcp.run(transport="stdio")
