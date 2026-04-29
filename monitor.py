"""
Home Assistant Monitor — Event-driven architecture
- Monitor: detects state changes, emits events
- Rules: define what to do when events fire
- Actions: notify, log, etc.
"""

import requests
import time
import logging
import os
import json
from datetime import datetime
from dotenv import load_dotenv
from gps_convert import wgs84_to_gcj02

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(message)s',
    datefmt='%H:%M:%S'
)
log = logging.getLogger(__name__)

# ── Config ──────────────────────────────────────────────
HA_URL = os.getenv("HA_URL", "http://192.168.3.6:8123/api")
HA_TOKEN = os.getenv("HA_TOKEN")
AMAP_KEY = os.getenv("AMAP_KEY")
HEADERS = {
    "Authorization": f"Bearer {HA_TOKEN}",
    "Content-Type": "application/json"
}
# Bypass proxy for local HA (only needed on LAN)
HA_HOST = HA_URL.split("//")[1].split(":")[0] if "//" in HA_URL else ""
if HA_HOST and not HA_HOST.startswith("http"):
    os.environ["NO_PROXY"] = f"{HA_HOST},localhost,127.0.0.1"
    os.environ["no_proxy"] = f"{HA_HOST},localhost,127.0.0.1"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOCATION_LOG = os.path.join(SCRIPT_DIR, "memory", "location-log.md")
EVENT_LOG = os.path.join(SCRIPT_DIR, "memory", "events.log")


# ── HA helpers ──────────────────────────────────────────
def ha_get(entity_id):
    try:
        r = requests.get(f"{HA_URL}/states/{entity_id}", headers=HEADERS, timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        log.error(f"HA get {entity_id}: {e}")
    return None


def ha_notify(message, title=None):
    try:
        payload = {"message": message}
        if title:
            payload["title"] = title
        requests.post(
            f"{HA_URL}/services/notify/mobile_app_your_phone",
            headers=HEADERS,
            json=payload,
            timeout=10
        )
        log.info(f"Notify: {title or ''} {message}")
    except Exception as e:
        log.error(f"Notify failed: {e}")


# ── GPS (imported from gps_convert.py) ─────────────────


def reverse_geocode(glat, glng):
    try:
        r = requests.get(
            f"https://restapi.amap.com/v3/geocode/regeo?key={AMAP_KEY}&location={glng},{glat}",
            timeout=10
        )
        if r.status_code == 200:
            data = r.json()
            if data.get("status") == "1":
                return data["regeocode"]["formatted_address"]
    except Exception:
        pass
    return None


# ── Event Bus ───────────────────────────────────────────
class EventBus:
    def __init__(self):
        self._handlers = {}  # event_type -> [handler_fn]

    def on(self, event_type, handler):
        self._handlers.setdefault(event_type, []).append(handler)

    def emit(self, event_type, data=None):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log.info(f"Event: {event_type} {data or ''}")
        # Write to event log
        try:
            with open(EVENT_LOG, "a", encoding="utf-8") as f:
                f.write(f"[{ts}] {event_type} {json.dumps(data or {}, ensure_ascii=False)}\n")
        except Exception:
            pass
        # Fire handlers
        for handler in self._handlers.get(event_type, []):
            try:
                handler(data)
            except Exception as e:
                log.error(f"Handler error for {event_type}: {e}")


bus = EventBus()


# ── Sensors (detect & emit) ─────────────────────────────
class PresenceSensor:
    """Polls person.your_name, emits presence.changed with 2min debounce."""
    def __init__(self):
        self.confirmed = None
        self.pending = None
        self.pending_since = 0

    def poll(self):
        state = ha_get("person.your_name")
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

        if now - self.pending_since >= 120:  # 2min debounce
            old = self.confirmed
            self.confirmed = current
            self.pending = None
            self.pending_since = 0
            bus.emit("presence.changed", {"from": old, "to": current})


class TemperatureSensor:
    """Polls climate entities every 10min, emits temperature.high / temperature.normal."""
    ENTITIES = ["climate.gree", "climate.gree_e6d9", "climate.studioroom"]

    def __init__(self):
        self.last_check = 0
        self.alerted = {}  # entity_id -> bool

    def poll(self, is_home):
        now = time.time()
        if now - self.last_check < 600:
            return
        self.last_check = now

        if not is_home:
            self.alerted.clear()
            return

        for entity_id in self.ENTITIES:
            state = ha_get(entity_id)
            if not state:
                continue
            temp = state["attributes"].get("current_temperature")
            if not temp:
                continue

            was_alerted = self.alerted.get(entity_id, False)
            if temp >= 32 and not was_alerted:
                self.alerted[entity_id] = True
                bus.emit("temperature.high", {"temp": temp, "entity": entity_id})
            elif temp < 30:
                if was_alerted:
                    bus.emit("temperature.normal", {"temp": temp, "entity": entity_id})
                self.alerted[entity_id] = False


class ACOnlineChecker:
    """Checks if AC units are online every 10min, alerts if unavailable."""
    ENTITIES = {
        "climate.gree": "客厅空调",
        "climate.gree_e6d9": "主卧空调",
        "climate.studioroom": "书房空调",
    }

    def __init__(self):
        self.last_check = 0
        self.offline = {}       # entity_id -> first_seen_offline timestamp
        self.notified = set()   # entity_ids already notified

    def poll(self):
        now = time.time()
        if now - self.last_check < 600:  # 10min
            return
        self.last_check = now

        for entity_id, name in self.ENTITIES.items():
            state = ha_get(entity_id)
            is_unavailable = (not state or state.get("state") == "unavailable")

            if is_unavailable:
                if entity_id not in self.offline:
                    self.offline[entity_id] = now
                    log.warning(f"{name} offline")
                # Notify after 20min offline, only once
                elif (now - self.offline[entity_id] >= 1200
                      and entity_id not in self.notified):
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
    """Logs location to memory/location-log.md every 30min, skips if unchanged."""
    def __init__(self):
        self.last_log = 0
        self.last_status = None
        self.last_address = None

    def poll(self):
        now = time.time()
        if now - self.last_log < 1800:
            return
        state = ha_get("person.your_name")
        if not state:
            return
        attrs = state.get("attributes", {})
        lat, lng = attrs.get("latitude"), attrs.get("longitude")
        status = state.get("state", "unknown")
        if not lat or not lng:
            return

        glat, glng = wgs84_to_gcj02(lat, lng)
        address = reverse_geocode(glat, glng) or "unknown"

        # Skip if status and address are the same as last log
        if status == self.last_status and address == self.last_address:
            self.last_log = now  # Reset timer but don't write
            log.info(f"Location unchanged: {status} | {address}, skipping")
            return

        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        weekday = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][datetime.now().weekday()]

        try:
            with open(LOCATION_LOG, "a", encoding="utf-8") as f:
                f.write(f"[{ts} {weekday}] {status} | {address} ({glng:.6f},{glat:.6f})\n")
            log.info(f"Location: {status} | {address}")
            self.last_status = status
            self.last_address = address
        except Exception as e:
            log.error(f"Location log failed: {e}")
        self.last_log = now


class RedditDaily:
    """Pushes one hot Reddit post every morning at 8:00 for English reading practice."""
    SUBREDDITS = ["todayilearned", "explainlikeimfive", "LifeProTips", "YouShouldKnow", "Showerthoughts"]

    PUSH_HOURS = [8, 22]  # 早上8点 + 晚上10点

    def __init__(self):
        self.pushed_slots = set()  # "2026-04-29-8", "2026-04-29-22"
        self._sent_urls = set()    # avoid duplicate posts between morning/evening

    def poll(self):
        now = datetime.now()
        if now.hour not in self.PUSH_HOURS:
            return
        slot = f"{now.strftime('%Y-%m-%d')}-{now.hour}"
        if slot in self.pushed_slots:
            return

        self.pushed_slots.add(slot)
        # Clean old slots (keep only today)
        today = now.strftime('%Y-%m-%d')
        self.pushed_slots = {s for s in self.pushed_slots if s.startswith(today)}
        post = self._fetch_top_post()
        if post:
            msg = post['title']
            try:
                requests.post(
                    f"{HA_URL}/services/notify/mobile_app_your_phone",
                    headers=HEADERS,
                    json={
                        "title": f"📖 r/{post['sub']}",
                        "message": msg,
                        "data": {
                            "url": post['url'],
                            "clickAction": post['url'],
                            "tag": "reddit-daily",
                            "sticky": True,
                        }
                    },
                    timeout=10
                )
            except Exception as e:
                log.error(f"Reddit notify failed: {e}")
            log.info(f"Reddit daily: {post['title'][:60]}")

    def _fetch_top_post(self):
        """Fetch top post across all subreddits, pick the one with most upvotes."""
        import re, html
        candidates = []
        for sub in self.SUBREDDITS:
            try:
                r = requests.get(
                    f"https://www.reddit.com/r/{sub}/top/.rss?t=day",
                    headers={"User-Agent": "weixin-agent/1.0"},
                    timeout=15
                )
                if r.status_code != 200:
                    continue
                entries = re.findall(r'<entry>(.*?)</entry>', r.text, re.DOTALL)
                for entry in entries[:3]:  # top 3 per sub
                    title_m = re.search(r'<title>(.*?)</title>', entry)
                    link_m = re.search(r'<link href="(.*?)"', entry)
                    # Extract upvotes from content if available
                    score_m = re.search(r'(\d+)\s*point', entry)
                    score = int(score_m.group(1)) if score_m else 0
                    if title_m and link_m:
                        candidates.append({
                            "sub": sub,
                            "title": html.unescape(title_m.group(1)),
                            "url": link_m.group(1),
                            "score": score
                        })
            except Exception as e:
                log.error(f"Reddit fetch {sub}: {e}")
        if not candidates:
            return None
        # Sort by score descending, pick top
        candidates.sort(key=lambda x: x["score"], reverse=True)
        # Avoid sending the same post twice (morning vs evening)
        for c in candidates:
            if c["url"] not in self._sent_urls:
                self._sent_urls.add(c["url"])
                return c
        return candidates[0]


class TunnelWatchdog:
    """Checks Cloudflare Tunnel health every 5min via HTTP, alerts if down."""
    TUNNEL_URL = os.getenv("TUNNEL_URL", "https://ha.mafuzhenhome.xyz")
    CHECK_INTERVAL = 300  # 5 minutes

    def __init__(self):
        self.last_check = time.time()  # Delay first check
        self.fail_count = 0
        self.notified = False

    def poll(self):
        now = time.time()
        if now - self.last_check < self.CHECK_INTERVAL:
            return
        self.last_check = now

        try:
            r = requests.get(self.TUNNEL_URL, timeout=15, allow_redirects=True)
            ok = r.status_code < 500
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

        # Notify after 3 consecutive failures (15min), only once
        if self.fail_count >= 3 and not self.notified:
            self.notified = True
            ha_notify("Cloudflare Tunnel 已离线超过 15 分钟，请检查 HA 盒子。", title="Tunnel 离线")


# ── Smart Helpers ─────────────────────────────────────

ENTITY_NAMES = {
    "climate.gree": "客厅空调",
    "climate.gree_e6d9": "主卧空调",
    "climate.studioroom": "书房空调",
}

DEVICE_CHECKS = [
    ("climate.gree", "客厅空调"),
    ("climate.gree_e6d9", "主卧空调"),
    ("climate.studioroom", "书房空调"),
    ("switch.living_room_main_light", "客厅主灯"),
    ("switch.living_room_ambient_light", "客厅氛围灯"),
    ("switch.dining_room_light", "餐厅灯"),
]


def get_time_greeting():
    """Return time-aware greeting in Chinese."""
    hour = datetime.now().hour
    if hour < 6:
        return "深夜"
    elif hour < 9:
        return "早上"
    elif hour < 12:
        return "上午"
    elif hour < 14:
        return "中午"
    elif hour < 18:
        return "下午"
    elif hour < 22:
        return "晚上"
    else:
        return "深夜"


def get_active_devices():
    """Return list of currently active device names."""
    active = []
    for entity_id, name in DEVICE_CHECKS:
        state = ha_get(entity_id)
        if not state:
            continue
        s = state["state"]
        if s in ("on", "cool", "heat", "dry", "fan_only", "auto"):
            extra = ""
            if entity_id.startswith("climate."):
                temp = state["attributes"].get("current_temperature")
                target = state["attributes"].get("temperature")
                if temp:
                    extra = f" ({temp}°C"
                    if target:
                        extra += f"→{target}°C"
                    extra += ")"
            active.append(f"{name}{extra}")
    return active


def is_quiet_hours():
    """Return True during sleeping hours (0:00-7:00)."""
    return datetime.now().hour < 7


# ── Automation Rules (react to events) ──────────────────

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


def rule_temp_alert(data):
    if is_quiet_hours():
        return  # Don't disturb during sleep
    room = ENTITY_NAMES.get(data.get("entity"), "")
    ha_notify(f"{room}温度 {data['temp']}°C，要开空调吗？", title="室温偏高")


def rule_ac_offline(data):
    name = data.get("name", "空调")
    ha_notify(f"{name}已离线超过 20 分钟，可能 WiFi 掉了。", title="空调离线")


# Register rules
bus.on("presence.changed", rule_presence_changed)
bus.on("temperature.high", rule_temp_alert)
bus.on("ac.offline", rule_ac_offline)


# ── Main Loop ───────────────────────────────────────────
def main():
    log.info("Monitor started (event-driven)")
    presence = PresenceSensor()
    temperature = TemperatureSensor()
    ac_checker = ACOnlineChecker()
    location = LocationLogger()
    tunnel = TunnelWatchdog()
    reddit = RedditDaily()

    # Trim event log on startup (keep last 200 lines)
    try:
        if os.path.exists(EVENT_LOG):
            with open(EVENT_LOG, "r", encoding="utf-8") as f:
                lines = f.readlines()
            if len(lines) > 200:
                with open(EVENT_LOG, "w", encoding="utf-8") as f:
                    f.writelines(lines[-200:])
    except Exception:
        pass

    # Trim location log on startup (keep last 100 lines)
    try:
        if os.path.exists(LOCATION_LOG):
            with open(LOCATION_LOG, "r", encoding="utf-8") as f:
                lines = f.readlines()
            if len(lines) > 150:
                # Keep header (first 5 lines) + last 100 data lines
                header = [l for l in lines[:5] if not l.startswith("[")]
                data = [l for l in lines if l.startswith("[")]
                with open(LOCATION_LOG, "w", encoding="utf-8") as f:
                    f.writelines(header)
                    f.writelines(data[-100:])
                log.info(f"Location log trimmed: {len(data)} -> 100 entries")
    except Exception:
        pass

    loop_count = 0
    while True:
        loop_count += 1
        try:
            presence.poll()
            temperature.poll(is_home=(presence.confirmed == "home"))
            ac_checker.poll()
            location.poll()
            tunnel.poll()
            reddit.poll()
            if loop_count <= 3 or loop_count % 10 == 0:
                log.info(f"Loop #{loop_count} ok (presence={presence.confirmed})")
        except Exception as e:
            log.error(f"Loop #{loop_count} error: {e}")
        time.sleep(60)


if __name__ == "__main__":
    main()
