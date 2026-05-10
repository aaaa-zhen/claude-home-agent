#!/usr/bin/env python3
"""Home Assistant MCP Server - provides guarded HA tools to Claude Code."""

import json
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

SCRIPT_DIR = Path(__file__).resolve().parent
load_dotenv(SCRIPT_DIR / ".env")

HA_URL = os.environ["HA_URL"]
HA_TOKEN = os.environ["HA_TOKEN"]
HEADERS = {
    "Authorization": f"Bearer {HA_TOKEN}",
    "Content-Type": "application/json",
}

DEFAULT_CONFIG = {
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
    merged.setdefault("haMcp", {}).update(user.get("haMcp", {}))
    return merged


CONFIG = load_config().get("haMcp", {})
ALLOWED_SERVICES = {
    domain: set(services)
    for domain, services in (CONFIG.get("allowedServices") or {}).items()
}
READ_ENTITY_PREFIXES = tuple(CONFIG.get("readEntityPrefixes") or [])
SERVICE_ENTITY_PREFIXES = tuple(CONFIG.get("serviceEntityPrefixes") or [])

mcp = FastMCP("homeassistant")


def _ha_get(path: str) -> dict | list:
    with httpx.Client(timeout=15) as client:
        response = client.get(f"{HA_URL}{path}", headers=HEADERS)
    response.raise_for_status()
    return response.json()


def _ha_post(path: str, data: dict | None = None) -> dict | list:
    with httpx.Client(timeout=15) as client:
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


if __name__ == "__main__":
    mcp.run(transport="stdio")
