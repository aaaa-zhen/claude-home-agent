"""
Home Assistant Monitor — event-driven architecture.
- Monitor: detects state changes and emits events
- Rules: decide what to do when events fire
- Actions: notify, log, etc.
"""

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from gps_convert import wgs84_to_gcj02

SCRIPT_DIR = Path(__file__).resolve().parent
load_dotenv(SCRIPT_DIR / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def load_config():
    defaults = {
        "homeAssistant": {
            "personEntity": "person.mafuzhen",
            "frontDoorEntity": "binary_sensor.front_door_contact",
            "notifyService": "notify/mobile_app_zhen",
        },
        "monitor": {
            "presenceDebounceSeconds": 120,
            "temperatureCheckIntervalSeconds": 600,
            "temperatureHighC": 32,
            "temperatureNormalC": 30,
            "acOfflineCheckIntervalSeconds": 600,
            "acOfflineGraceSeconds": 1200,
            "locationLogIntervalSeconds": 1800,
            "tunnelCheckIntervalSeconds": 300,
            "doorSecurityCooldownSeconds": 300,
            "loopIntervalSeconds": 60,
        },
        "entities": {
            "climate": {
                "climate.gree": "客厅空调",
                "climate.gree_e6d9": "主卧空调",
                "climate.studioroom": "书房空调",
            },
            "deviceChecks": [],
        },
    }
    path = SCRIPT_DIR / "config.json"
    if not path.exists():
        return defaults
    try:
        with path.open("r", encoding="utf-8") as f:
            user = json.load(f)
    except Exception as exc:
        log.error(f"Failed to load config.json, using defaults: {exc}")
        return defaults

    def merge(base, override):
        for key, value in override.items():
            if isinstance(value, dict) and isinstance(base.get(key), dict):
                merge(base[key], value)
            else:
                base[key] = value
        return base

    return merge(defaults, user)


CONFIG = load_config()
HA_CONFIG = CONFIG.get("homeAssistant", {})
MONITOR_CONFIG = CONFIG.get("monitor", {})
ENTITY_CONFIG = CONFIG.get("entities", {})

HA_URL = os.getenv("HA_URL", "http://192.168.3.6:8123/api")
HA_TOKEN = os.getenv("HA_TOKEN")
AMAP_KEY = os.getenv("AMAP_KEY")
PERSON_ENTITY = HA_CONFIG.get("personEntity", "person.mafuzhen")
FRONT_DOOR_ENTITY = HA_CONFIG.get("frontDoorEntity", "binary_sensor.front_door_contact")
NOTIFY_SERVICE = HA_CONFIG.get("notifyService", "notify/mobile_app_zhen").strip("/")
HEADERS = {
    "Authorization": f"Bearer {HA_TOKEN}",
    "Content-Type": "application/json",
}

HA_HOST = HA_URL.split("//")[1].split(":")[0] if "//" in HA_URL else ""
if HA_HOST and not HA_HOST.startswith("http"):
    os.environ["NO_PROXY"] = f"{HA_HOST},localhost,127.0.0.1"
    os.environ["no_proxy"] = f"{HA_HOST},localhost,127.0.0.1"

LOCATION_LOG = SCRIPT_DIR / "memory" / "location-log.md"
EVENT_LOG = SCRIPT_DIR / "memory" / "events.log"

HTTP = requests.Session()
RETRY = Retry(
    total=3,
    connect=3,
    read=2,
    backoff_factor=0.5,
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=frozenset(["GET", "POST"]),
)
ADAPTER = HTTPAdapter(max_retries=RETRY, pool_connections=8, pool_maxsize=8)
HTTP.mount("http://", ADAPTER)
HTTP.mount("https://", ADAPTER)


def cfg_int(key, default):
    try:
        return int(MONITOR_CONFIG.get(key, default))
    except (TypeError, ValueError):
        return default


def cfg_float(key, default):
    try:
        return float(MONITOR_CONFIG.get(key, default))
    except (TypeError, ValueError):
        return default


# ── HA helpers ──────────────────────────────────────────
def ha_get(entity_id):
    try:
        response = HTTP.get(f"{HA_URL}/states/{entity_id}", headers=HEADERS, timeout=10)
        if response.status_code == 200:
            return response.json()
        if response.status_code == 401:
            log.error(f"HA token rejected while reading {entity_id}")
        else:
            log.warning(f"HA get {entity_id}: HTTP {response.status_code}")
    except Exception as exc:
        log.error(f"HA get {entity_id}: {exc}")
    return None


def ha_notify(message, title=None):
    try:
        payload = {"message": message}
        if title:
            payload["title"] = title
        response = HTTP.post(
            f"{HA_URL}/services/{NOTIFY_SERVICE}",
            headers=HEADERS,
            json=payload,
            timeout=10,
        )
        if response.status_code >= 400:
            log.warning(f"Notify failed: HTTP {response.status_code} {response.text[:200]}")
            return
        log.info(f"Notify: {title or ''} {message}")
    except Exception as exc:
        log.error(f"Notify failed: {exc}")


def reverse_geocode(glat, glng):
    if not AMAP_KEY:
        return None
    try:
        response = HTTP.get(
            "https://restapi.amap.com/v3/geocode/regeo",
            params={"key": AMAP_KEY, "location": f"{glng},{glat}"},
            timeout=10,
        )
        if response.status_code == 200:
            data = response.json()
            if data.get("status") == "1":
                return data["regeocode"]["formatted_address"]
    except Exception as exc:
        log.error(f"Reverse geocode failed: {exc}")
    return None


# ── Event Bus ───────────────────────────────────────────
class EventBus:
    def __init__(self):
        self._handlers = {}

    def on(self, event_type, handler):
        self._handlers.setdefault(event_type, []).append(handler)

    def emit(self, event_type, data=None):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log.info(f"Event: {event_type} {data or ''}")
        try:
            with EVENT_LOG.open("a", encoding="utf-8") as f:
                f.write(f"[{ts}] {event_type} {json.dumps(data or {}, ensure_ascii=False)}\n")
        except Exception:
            pass
        for handler in self._handlers.get(event_type, []):
            try:
                handler(data)
            except Exception as exc:
                log.error(f"Handler error for {event_type}: {exc}")


bus = EventBus()


# ── Sensors ─────────────────────────────────────────────
class PresenceSensor:
    def __init__(self):
        self.confirmed = None
        self.pending = None
        self.pending_since = 0
        self.debounce = cfg_int("presenceDebounceSeconds", 120)

    def poll(self):
        state = ha_get(PERSON_ENTITY)
        if not state:
            return
        current = state["state"]
        now = time.time()

        if self.confirmed is None:
            self.confirmed = current
            log.info(f"Presence init: {current}")
            return

        if current == self.confirmed:
            self.pending = None
            self.pending_since = 0
            return

        if self.pending != current:
            self.pending = current
            self.pending_since = now
            return

        if now - self.pending_since >= self.debounce:
            old = self.confirmed
            self.confirmed = current
            self.pending = None
            self.pending_since = 0
            bus.emit("presence.changed", {"from": old, "to": current})


class DoorSecuritySensor:
    def __init__(self):
        self.entity_id = FRONT_DOOR_ENTITY
        self.last_state = None
        self.cooldown = cfg_int("doorSecurityCooldownSeconds", 300)
        self.last_alert = 0

    def poll(self):
        state = ha_get(self.entity_id)
        if not state:
            return
        current = state.get("state")

        if self.last_state is None:
            self.last_state = current
            log.info(f"Door security init: {current}")
            return

        opened = self.last_state == "off" and current == "on"
        self.last_state = current
        if not opened:
            return

        person = ha_get(PERSON_ENTITY)
        person_state = person.get("state") if person else "unknown"
        if person_state != "not_home":
            log.info(f"Front door opened while person_state={person_state}, no security alert")
            return

        now = time.time()
        if now - self.last_alert < self.cooldown:
            log.info("Front door security alert suppressed by cooldown")
            return

        self.last_alert = now
        bus.emit("front_door.opened_while_away", {"entity": self.entity_id})


class TemperatureSensor:
    def __init__(self):
        self.entities = list((ENTITY_CONFIG.get("climate") or {}).keys())
        self.interval = cfg_int("temperatureCheckIntervalSeconds", 600)
        self.high_c = cfg_float("temperatureHighC", 32)
        self.normal_c = cfg_float("temperatureNormalC", 30)
        self.last_check = 0
        self.alerted = {}

    def poll(self, is_home):
        now = time.time()
        if now - self.last_check < self.interval:
            return
        self.last_check = now

        if not is_home:
            self.alerted.clear()
            return

        for entity_id in self.entities:
            state = ha_get(entity_id)
            if not state:
                continue
            temp = state["attributes"].get("current_temperature")
            if temp is None:
                continue

            was_alerted = self.alerted.get(entity_id, False)
            if temp >= self.high_c and not was_alerted:
                self.alerted[entity_id] = True
                bus.emit("temperature.high", {"temp": temp, "entity": entity_id})
            elif temp < self.normal_c:
                if was_alerted:
                    bus.emit("temperature.normal", {"temp": temp, "entity": entity_id})
                self.alerted[entity_id] = False


class ACOnlineChecker:
    def __init__(self):
        self.entities = ENTITY_CONFIG.get("climate") or {}
        self.interval = cfg_int("acOfflineCheckIntervalSeconds", 600)
        self.grace = cfg_int("acOfflineGraceSeconds", 1200)
        self.last_check = 0
        self.offline = {}
        self.notified = set()

    def poll(self):
        now = time.time()
        if now - self.last_check < self.interval:
            return
        self.last_check = now

        for entity_id, name in self.entities.items():
            state = ha_get(entity_id)
            is_unavailable = not state or state.get("state") == "unavailable"

            if is_unavailable:
                if entity_id not in self.offline:
                    self.offline[entity_id] = now
                    log.warning(f"{name} offline")
                elif now - self.offline[entity_id] >= self.grace and entity_id not in self.notified:
                    self.notified.add(entity_id)
                    bus.emit("ac.offline", {"entity": entity_id, "name": name})
            else:
                if entity_id in self.offline:
                    log.info(f"{name} back online")
                    if entity_id in self.notified:
                        ha_notify(f"{name}恢复在线了。")
                self.offline.pop(entity_id, None)
                self.notified.discard(entity_id)


class LocationLogger:
    def __init__(self):
        self.interval = cfg_int("locationLogIntervalSeconds", 1800)
        self.last_log = 0
        self.last_status = None
        self.last_address = None

    def poll(self):
        now = time.time()
        if now - self.last_log < self.interval:
            return
        state = ha_get(PERSON_ENTITY)
        if not state:
            return
        attrs = state.get("attributes", {})
        lat, lng = attrs.get("latitude"), attrs.get("longitude")
        status = state.get("state", "unknown")
        if not lat or not lng:
            return

        glat, glng = wgs84_to_gcj02(lat, lng)
        address = reverse_geocode(glat, glng) or "unknown"

        if status == self.last_status and address == self.last_address:
            self.last_log = now
            log.info(f"Location unchanged: {status} | {address}, skipping")
            return

        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        weekday = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][datetime.now().weekday()]

        try:
            with LOCATION_LOG.open("a", encoding="utf-8") as f:
                f.write(f"[{ts} {weekday}] {status} | {address} ({glng:.6f},{glat:.6f})\n")
            log.info(f"Location: {status} | {address}")
            self.last_status = status
            self.last_address = address
        except Exception as exc:
            log.error(f"Location log failed: {exc}")
        self.last_log = now


class TunnelWatchdog:
    def __init__(self):
        self.tunnel_url = os.getenv("TUNNEL_URL", HA_URL.replace("/api", ""))
        self.interval = cfg_int("tunnelCheckIntervalSeconds", 300)
        self.last_check = time.time()
        self.fail_count = 0
        self.notified = False

    def poll(self):
        now = time.time()
        if now - self.last_check < self.interval:
            return
        self.last_check = now

        try:
            response = HTTP.get(self.tunnel_url, timeout=15, allow_redirects=True)
            ok = response.status_code < 500
        except Exception:
            ok = False

        if ok:
            if self.fail_count > 0:
                log.info("Tunnel recovered")
                if self.notified:
                    ha_notify("Cloudflare Tunnel 已恢复正常。")
                    self.notified = False
            self.fail_count = 0
            return

        self.fail_count += 1
        log.warning(f"Tunnel HTTP check failed (fail #{self.fail_count})")
        bus.emit("tunnel.check_failed", {"fail_count": self.fail_count})

        if self.fail_count >= 3 and not self.notified:
            self.notified = True
            ha_notify("Cloudflare Tunnel 已离线超过 15 分钟，请检查 HA 盒子。", title="Tunnel 离线")


ENTITY_NAMES = ENTITY_CONFIG.get("climate") or {}
DEVICE_CHECKS = [
    (item["entity_id"], item["name"])
    for item in ENTITY_CONFIG.get("deviceChecks", [])
    if item.get("entity_id") and item.get("name")
]


def get_time_greeting():
    hour = datetime.now().hour
    if hour < 6:
        return "深夜"
    if hour < 9:
        return "早上"
    if hour < 12:
        return "上午"
    if hour < 14:
        return "中午"
    if hour < 18:
        return "下午"
    if hour < 22:
        return "晚上"
    return "深夜"


def get_active_devices():
    active = []
    for entity_id, name in DEVICE_CHECKS:
        state = ha_get(entity_id)
        if not state:
            continue
        current_state = state["state"]
        if current_state in ("on", "cool", "heat", "dry", "fan_only", "auto"):
            extra = ""
            if entity_id.startswith("climate."):
                temp = state["attributes"].get("current_temperature")
                target = state["attributes"].get("temperature")
                if temp is not None:
                    extra = f" ({temp}°C"
                    if target is not None:
                        extra += f"→{target}°C"
                    extra += ")"
            active.append(f"{name}{extra}")
    return active


def is_quiet_hours():
    return datetime.now().hour < 7


# ── Automation Rules ────────────────────────────────────
def rule_presence_changed(data):
    if data["to"] == "home":
        period = get_time_greeting()
        active = get_active_devices()
        msg = f"{period}好，欢迎回家！"
        if active:
            msg += "\n当前开着：" + "、".join(active)
        ha_notify(msg, title="到家了")
    elif data["to"] == "not_home":
        active = get_active_devices()
        if active:
            msg = "出门了，这些设备还开着：\n" + "、".join(active)
            ha_notify(msg, title="出门提醒")
        else:
            ha_notify("出门了，所有设备已关。出行顺利！", title="出门了")


def rule_front_door_security(data):
    ha_notify("你现在不在家，但大门刚刚被打开了。", title="安全提醒")


def rule_temp_alert(data):
    if is_quiet_hours():
        return
    room = ENTITY_NAMES.get(data.get("entity"), "")
    ha_notify(f"{room}温度 {data['temp']}°C，要开空调吗？", title="室温偏高")


def rule_ac_offline(data):
    name = data.get("name", "空调")
    ha_notify(f"{name}已离线超过 20 分钟，可能 WiFi 掉了。", title="空调离线")


bus.on("presence.changed", rule_presence_changed)
bus.on("front_door.opened_while_away", rule_front_door_security)
bus.on("temperature.high", rule_temp_alert)
bus.on("ac.offline", rule_ac_offline)


def trim_log(path, max_lines, keep_header=False):
    try:
        if not path.exists():
            return
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        if len(lines) <= max_lines:
            return
        if keep_header:
            header = [line for line in lines[:5] if not line.startswith("[")]
            data = [line for line in lines if line.startswith("[")]
            path.write_text("".join(header + data[-max_lines:]), encoding="utf-8")
        else:
            path.write_text("".join(lines[-max_lines:]), encoding="utf-8")
    except Exception as exc:
        log.error(f"Trim log failed for {path}: {exc}")


def main():
    log.info("Monitor started (event-driven)")
    presence = PresenceSensor()
    door_security = DoorSecuritySensor()
    temperature = TemperatureSensor()
    ac_checker = ACOnlineChecker()
    location = LocationLogger()
    tunnel = TunnelWatchdog()
    trim_log(EVENT_LOG, 200)
    trim_log(LOCATION_LOG, 100, keep_header=True)

    loop_count = 0
    interval = cfg_int("loopIntervalSeconds", 60)
    while True:
        loop_count += 1
        try:
            presence.poll()
            door_security.poll()
            temperature.poll(is_home=(presence.confirmed == "home"))
            ac_checker.poll()
            location.poll()
            tunnel.poll()
            if loop_count <= 3 or loop_count % 10 == 0:
                log.info(f"Loop #{loop_count} ok (presence={presence.confirmed})")
        except Exception as exc:
            log.error(f"Loop #{loop_count} error: {exc}")
        time.sleep(interval)


if __name__ == "__main__":
    main()
