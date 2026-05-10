#!/usr/bin/env python3
"""Create and manage location-based reminders for weixin-agent.

Geofence coordinates are stored in GCJ-02 because the check command converts
HA GPS from WGS-84 to GCJ-02 before distance checks.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import time
import fcntl
from datetime import datetime, timezone
from pathlib import Path
import math
import requests

from gps_convert import wgs84_to_gcj02

SCRIPT_DIR = Path(__file__).resolve().parent
GEOFENCES_FILE = SCRIPT_DIR / "memory" / "geofences.json"
LOCK_FILE = SCRIPT_DIR / "memory" / "geofence-check.lock"
CRON_LOG = SCRIPT_DIR / "logs" / "geofence-cron.log"
CRON_MARKER = "geofence_reminder.py check"
NODE_BIN = "/usr/bin/node"

def python_bin():
    candidate = SCRIPT_DIR / "venv" / "bin" / "python"
    return candidate if candidate.exists() else Path("/usr/bin/python3")


def geofence_cron_line():
    return (
        f"* * * * * cd {SCRIPT_DIR} && {python_bin()} "
        f"{SCRIPT_DIR / 'geofence_reminder.py'} check --quiet "
        f">> {CRON_LOG} 2>&1"
    )


def load_env_file(path):
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


load_env_file(SCRIPT_DIR / ".env")
AMAP_KEY = os.getenv("AMAP_KEY")
DEFAULT_CITY = "珠海"
DEFAULT_RADIUS_M = 400
DEFAULT_MAX_GPS_AGE_MINUTES = 180
DEFAULT_ACCURACY_PADDING_M = 100

# Coordinates are GCJ-02 lng/lat.
ALIASES = {
    "公司": {
        "name": "魅族科技大楼",
        "lng": 113.569233,
        "lat": 22.372477,
        "radius_m": 350,
    },
    "魅族": {
        "name": "魅族科技大楼",
        "lng": 113.569233,
        "lat": 22.372477,
        "radius_m": 350,
    },
    "魅族科技": {
        "name": "魅族科技大楼",
        "lng": 113.569233,
        "lat": 22.372477,
        "radius_m": 350,
    },
    "家": {
        "name": "仁恒河滨花园",
        "lng": 113.550261,
        "lat": 22.396891,
        "radius_m": 250,
    },
}


def load_geofences():
    if not GEOFENCES_FILE.exists():
        return []
    try:
        data = json.loads(GEOFENCES_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception as exc:
        raise SystemExit(f"读取 {GEOFENCES_FILE} 失败: {exc}")


def save_geofences(items):
    GEOFENCES_FILE.parent.mkdir(parents=True, exist_ok=True)
    GEOFENCES_FILE.write_text(
        json.dumps(items, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def slugify(text):
    ascii_part = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    digest = hashlib.sha1(f"{text}-{time.time()}".encode("utf-8")).hexdigest()[:8]
    return f"{ascii_part}-{digest}" if ascii_part else f"geo-{digest}"


def parse_lnglat(value):
    parts = [p.strip() for p in value.split(",")]
    if len(parts) != 2:
        raise SystemExit("坐标格式应为 lng,lat，例如 113.569233,22.372477")
    lng, lat = float(parts[0]), float(parts[1])
    return {"name": value, "lng": lng, "lat": lat, "radius_m": DEFAULT_RADIUS_M}


def amap_get(path, params):
    if not AMAP_KEY:
        raise SystemExit("缺少 AMAP_KEY，无法搜索地点。可改用 --coords lng,lat。")
    params = dict(params)
    params["key"] = AMAP_KEY
    response = requests.get(
        f"https://restapi.amap.com{path}",
        params=params,
        timeout=12,
    )
    response.raise_for_status()
    data = response.json()
    if data.get("status") != "1":
        raise SystemExit(f"高德 API 返回失败: {data}")
    return data


def resolve_place(place, city=DEFAULT_CITY):
    normalized = place.strip()
    if normalized in ALIASES:
        return dict(ALIASES[normalized])

    # Prefer POI text search because it returns stores like 盒马/商场.
    data = amap_get("/v3/place/text", {"keywords": normalized, "city": city, "offset": 5})
    pois = data.get("pois") or []
    if pois:
        poi = pois[0]
        lng, lat = [float(x) for x in poi["location"].split(",")]
        return {
            "name": poi.get("name") or normalized,
            "address": poi.get("address") or "",
            "lng": lng,
            "lat": lat,
            "radius_m": DEFAULT_RADIUS_M,
            "source": "amap_place_text",
        }

    data = amap_get("/v3/geocode/geo", {"address": normalized, "city": city})
    geocodes = data.get("geocodes") or []
    if geocodes:
        item = geocodes[0]
        lng, lat = [float(x) for x in item["location"].split(",")]
        return {
            "name": item.get("formatted_address") or normalized,
            "lng": lng,
            "lat": lat,
            "radius_m": DEFAULT_RADIUS_M,
            "source": "amap_geocode",
        }

    raise SystemExit(f"找不到地点：{place}")


def haversine_m(lat1, lng1, lat2, lng2):
    radius = 6371000
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlng / 2) ** 2
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def parse_ha_timestamp(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def current_location_info():
    ha_url = os.getenv("HA_URL")
    ha_token = os.getenv("HA_TOKEN")
    if not ha_url or not ha_token:
        return None
    headers = {"Authorization": f"Bearer {ha_token}"}
    try:
        response = requests.get(f"{ha_url}/states/person.mafuzhen", headers=headers, timeout=10)
        if response.status_code != 200:
            return None
        state = response.json()
        attrs = state.get("attributes", {})
        lat, lng = attrs.get("latitude"), attrs.get("longitude")
        if lat is None or lng is None:
            return None
        glat, glng = wgs84_to_gcj02(float(lat), float(lng))
        updated_at = parse_ha_timestamp(state.get("last_updated"))
        age_seconds = None
        if updated_at:
            age_seconds = max(0, (datetime.now(timezone.utc) - updated_at).total_seconds())
        return {
            "lat": glat,
            "lng": glng,
            "raw_lat": float(lat),
            "raw_lng": float(lng),
            "state": state.get("state"),
            "source": attrs.get("source"),
            "gps_accuracy": attrs.get("gps_accuracy"),
            "last_updated": state.get("last_updated"),
            "age_seconds": age_seconds,
        }
    except Exception:
        return None


def current_gcj_location():
    info = current_location_info()
    if not info:
        return None
    return info["lat"], info["lng"]


def simulated_location_info(value):
    parsed = parse_lnglat(value)
    return {
        "lat": parsed["lat"],
        "lng": parsed["lng"],
        "raw_lat": parsed["lat"],
        "raw_lng": parsed["lng"],
        "state": "simulated",
        "source": "--simulate-coords",
        "gps_accuracy": 0,
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "age_seconds": 0,
    }


def send_weixin(message):
    subprocess.run(
        [NODE_BIN, str(SCRIPT_DIR / "weixin-send.mjs"), message],
        cwd=SCRIPT_DIR,
        timeout=30,
        check=True,
    )


def ensure_geofence_cron():
    CRON_LOG.parent.mkdir(parents=True, exist_ok=True)
    current = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    if current.returncode not in (0, 1):
        raise SystemExit(f"读取 crontab 失败: {current.stderr.strip()}")

    existing = current.stdout.splitlines()
    kept = [line for line in existing if CRON_MARKER not in line]
    line = geofence_cron_line()
    if existing == kept + [line]:
        return False

    kept.append(line)
    new_cron = "\n".join(kept).rstrip() + "\n"
    subprocess.run(["crontab", "-"], input=new_cron, text=True, check=True)
    return True


def check(args):
    quiet = bool(getattr(args, "quiet", False))
    dry_run = bool(getattr(args, "dry_run", False))
    max_age_seconds = int(getattr(args, "max_gps_age_minutes", DEFAULT_MAX_GPS_AGE_MINUTES) * 60)
    max_padding = int(getattr(args, "max_accuracy_padding_m", DEFAULT_ACCURACY_PADDING_M))

    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("w", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if not quiet:
                print("上一次地点提醒检查还在运行，跳过。")
            return

        items = load_geofences()
        pending = [
            item for item in items
            if item.get("active") and (not item.get("triggered") or not item.get("trigger_once", True))
        ]
        if not pending:
            if not quiet:
                print("没有待检查的地点提醒。")
            return

        info = simulated_location_info(args.simulate_coords) if getattr(args, "simulate_coords", None) else current_location_info()
        if not info:
            if not quiet:
                print("无法获取当前位置，跳过地点提醒检查。")
            return

        age = info.get("age_seconds")
        if age is not None and age > max_age_seconds:
            if not quiet:
                print(f"GPS 更新时间已超过 {age/60:.0f} 分钟，超过阈值 {max_age_seconds/60:.0f} 分钟，跳过以避免误触发。")
            return

        current_lat, current_lng = info["lat"], info["lng"]
        gps_accuracy = info.get("gps_accuracy")
        try:
            accuracy_padding = min(max(int(float(gps_accuracy or 0)), 0), max_padding)
        except (TypeError, ValueError):
            accuracy_padding = 0

        if not quiet:
            age_text = "unknown" if age is None else f"{age/60:.1f}min"
            print(
                f"GPS gcj=({current_lng:.6f},{current_lat:.6f}) "
                f"state={info.get('state')} source={info.get('source')} "
                f"accuracy={gps_accuracy}m age={age_text}"
            )

        changed = False
        triggered = []
        for item in pending:
            radius = int(item.get("radius_m") or DEFAULT_RADIUS_M)
            effective_radius = radius + accuracy_padding
            dist = haversine_m(current_lat, current_lng, item["lat"], item["lng"])
            inside = dist <= effective_radius
            was_inside = bool(item.get("was_inside", False))
            if not quiet:
                print(
                    f"{item.get('id')} {item.get('name')}: dist={dist:.0f}m "
                    f"radius={radius}m effective={effective_radius}m inside={inside} was_inside={was_inside}"
                )

            if item.get("was_inside") != inside:
                item["was_inside"] = inside
                changed = True

            if inside and not was_inside:
                triggered_at = datetime.now().isoformat(timespec="seconds")
                if dry_run:
                    print(f"DRY-RUN would send: {item['message']}")
                else:
                    send_weixin(item["message"])
                item["last_triggered_at"] = triggered_at
                if item.get("trigger_once", True):
                    item["triggered"] = True
                    item["triggered_at"] = triggered_at
                    item["active"] = False
                else:
                    item["triggered"] = False
                triggered.append(item.get("id"))
                changed = True

        if changed and not dry_run:
            save_geofences(items)
        if triggered:
            prefix = "dry-run matched" if dry_run else "已触发地点提醒"
            print(prefix + ": " + ", ".join(triggered))


def status(_args):
    items = load_geofences()
    active = [item for item in items if item.get("active") and not item.get("triggered")]
    repeat = [item for item in items if item.get("active") and not item.get("trigger_once", True)]
    cron_line = geofence_cron_line()
    current = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    cron_ok = current.returncode == 0 and CRON_MARKER in current.stdout and cron_line in current.stdout
    print(f"cron={'ok' if cron_ok else 'missing-or-different'}")
    print(f"reminders active={len(active)} repeat={len(repeat)} total={len(items)}")
    info = current_location_info()
    if not info:
        print("gps=unavailable")
        return
    age = info.get("age_seconds")
    age_text = "unknown" if age is None else f"{age/60:.1f}min"
    print(
        f"gps=ok state={info.get('state')} source={info.get('source')} "
        f"accuracy={info.get('gps_accuracy')}m age={age_text} "
        f"gcj=({info['lng']:.6f},{info['lat']:.6f}) "
        f"updated={info.get('last_updated')}"
    )


def self_test(args):
    info = current_location_info()
    if not info:
        raise SystemExit("无法获取当前位置，self-test 失败。")
    item = {
        "id": "self-test",
        "name": "地点提醒自测",
        "lat": info["lat"],
        "lng": info["lng"],
        "radius_m": 20,
        "message": "地点提醒链路测试：无需处理。",
        "active": True,
        "triggered": False,
        "trigger_once": True,
        "was_inside": False,
    }
    dist = haversine_m(info["lat"], info["lng"], item["lat"], item["lng"])
    if dist > item["radius_m"]:
        raise SystemExit(f"self-test 失败：距离 {dist:.0f}m 超过半径。")
    print("self-test matched current location")
    if getattr(args, "send", False):
        send_weixin(item["message"])
        print("self-test sent weixin message")
    else:
        print(f"DRY-RUN would send: {item['message']}")


def add(args):
    if args.coords:
        resolved = parse_lnglat(args.coords)
        if args.place:
            resolved["name"] = args.place
    else:
        if not args.place:
            raise SystemExit("add 需要 --place 或 --coords")
        resolved = resolve_place(args.place, args.city)

    radius = args.radius_m or resolved.get("radius_m") or DEFAULT_RADIUS_M
    message = args.message.strip()
    if not message:
        raise SystemExit("--message 不能为空")

    name = args.name or resolved["name"]
    lat = round(float(resolved["lat"]), 6)
    lng = round(float(resolved["lng"]), 6)
    current = current_location_info()
    was_inside = False
    if current:
        age = current.get("age_seconds")
        if age is None or age <= DEFAULT_MAX_GPS_AGE_MINUTES * 60:
            was_inside = haversine_m(current["lat"], current["lng"], lat, lng) <= int(radius)
        else:
            print(f"当前位置 GPS 已 {age/60:.0f} 分钟未更新，新提醒不会假定你已在围栏内。")

    item = {
        "id": args.id or slugify(name),
        "name": name,
        "place_query": args.place or args.coords,
        "lat": lat,
        "lng": lng,
        "radius_m": int(radius),
        "message": message,
        "active": not args.inactive,
        "triggered": False,
        "trigger_once": not args.repeat,
        "was_inside": was_inside,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source": resolved.get("source", "alias_or_coords"),
    }
    if resolved.get("address"):
        item["address"] = resolved["address"]

    items = load_geofences()
    ids = {x.get("id") for x in items}
    while item["id"] in ids:
        item["id"] = slugify(name)
    items.append(item)
    save_geofences(items)
    cron_changed = ensure_geofence_cron()
    print(json.dumps(item, ensure_ascii=False, indent=2))
    if cron_changed:
        print("已安装地点提醒 cron。")


def list_items(_args):
    items = load_geofences()
    if not items:
        print("没有地理围栏提醒。")
        return
    for item in items:
        status = "active" if item.get("active") and not item.get("triggered") else "done/off"
        print(
            f"{item.get('id')} | {status} | {item.get('name')} | "
            f"{item.get('radius_m')}m | {item.get('message')}"
        )


def set_active(reminder_id, active):
    items = load_geofences()
    changed = False
    for item in items:
        if item.get("id") == reminder_id:
            item["active"] = active
            if active:
                item["triggered"] = False
                item.pop("triggered_at", None)
            changed = True
            break
    if not changed:
        raise SystemExit(f"找不到提醒 id: {reminder_id}")
    save_geofences(items)
    if active and ensure_geofence_cron():
        print("已安装地点提醒 cron。")
    print(f"{reminder_id} -> {'active' if active else 'inactive'}")


def main():
    parser = argparse.ArgumentParser(description="管理地理围栏提醒")
    sub = parser.add_subparsers(dest="cmd", required=True)

    add_parser = sub.add_parser("add", help="新增地理围栏提醒")
    add_parser.add_argument("--place", help="地点名称，例如 公司、盒马、万象汇")
    add_parser.add_argument("--coords", help="GCJ-02 坐标 lng,lat")
    add_parser.add_argument("--city", default=DEFAULT_CITY)
    add_parser.add_argument("--message", required=True, help="触发后发送的微信提醒内容")
    add_parser.add_argument("--name", help="提醒名称")
    add_parser.add_argument("--id", help="自定义 id")
    add_parser.add_argument("--radius-m", type=int, default=None)
    add_parser.add_argument("--repeat", action="store_true", help="触发后不自动关闭")
    add_parser.add_argument("--inactive", action="store_true", help="只创建，不启用")
    add_parser.set_defaults(func=add)

    list_parser = sub.add_parser("list", help="列出提醒")
    list_parser.set_defaults(func=list_items)

    check_parser = sub.add_parser("check", help="检查当前位置并触发地点提醒，供 cron 每分钟调用")
    check_parser.add_argument("--quiet", action="store_true", help="无待办时不输出日志")
    check_parser.add_argument("--dry-run", action="store_true", help="只演练匹配和触发，不发送微信、不写回状态")
    check_parser.add_argument("--simulate-coords", help="使用 GCJ-02 坐标 lng,lat 模拟当前位置")
    check_parser.add_argument("--max-gps-age-minutes", type=int, default=DEFAULT_MAX_GPS_AGE_MINUTES)
    check_parser.add_argument("--max-accuracy-padding-m", type=int, default=DEFAULT_ACCURACY_PADDING_M)
    check_parser.set_defaults(func=check)

    status_parser = sub.add_parser("status", help="查看地点提醒 cron、GPS 新鲜度和提醒数量")
    status_parser.set_defaults(func=status)

    self_test_parser = sub.add_parser("self-test", help="用当前位置做一次地点提醒触发演练")
    self_test_parser.add_argument("--send", action="store_true", help="发送一条微信测试消息")
    self_test_parser.set_defaults(func=self_test)

    ensure_parser = sub.add_parser("ensure-cron", help="确保地点提醒 cron 已安装")
    ensure_parser.set_defaults(func=lambda _args: print("installed" if ensure_geofence_cron() else "already installed"))

    enable_parser = sub.add_parser("enable", help="启用提醒")
    enable_parser.add_argument("id")
    enable_parser.set_defaults(func=lambda args: set_active(args.id, True))

    disable_parser = sub.add_parser("disable", help="停用提醒")
    disable_parser.add_argument("id")
    disable_parser.set_defaults(func=lambda args: set_active(args.id, False))

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
