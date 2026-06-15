#!/usr/bin/env python3
"""
amap_nav.py — 高德地图导航工具
用法:
  python3 amap_nav.py route --from 望京 --to 三里屯 --city 北京 [--mode driving|walking|riding|transit]
  python3 amap_nav.py geocode --address 望京 --city 北京
  python3 amap_nav.py poi --keyword 星巴克 --city 北京 [--center 116.40,39.91] [--radius 3000]
  python3 amap_nav.py uri --from-loc 116.48,39.99 --to-loc 116.45,39.93 --from-name 望京 --to-name 三里屯 [--mode car]

输出: JSON，供 Claude 解析
"""

import sys
import os
import json
import argparse
from urllib.parse import urlencode, quote

import requests

# 从 .env 读取 AMAP_KEY（如果环境变量里没有）
def load_amap_key():
    key = os.environ.get("AMAP_KEY", "")
    if not key:
        env_path = os.path.join(os.path.dirname(__file__), ".env")
        try:
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("AMAP_KEY="):
                        key = line.split("=", 1)[1].strip()
                        break
        except FileNotFoundError:
            pass
    return key

AMAP_KEY = load_amap_key()
BASE = "https://restapi.amap.com"

# ── HA 盒子代理（国内网络，通过 Cloudflare Tunnel）──
# VPS 在日本，直连高德 API 可能超时。优先通过 HA 盒子上的 amap-proxy 代理调用。
import subprocess, time

_cf_proc = None
_cf_local_port = 15004

def _ensure_tunnel():
    """确保 cloudflared tunnel 到 HA 盒子的 SSH 通道已建立"""
    global _cf_proc
    if _cf_proc and _cf_proc.poll() is None:
        return True
    try:
        _cf_proc = subprocess.Popen(
            ['cloudflared', 'access', 'tcp', '--hostname',
             os.getenv('HA_SSH_HOST', 'your-ssh-host.example.com'),
             '--url', f'localhost:{_cf_local_port}'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        time.sleep(2)
        return _cf_proc.poll() is None
    except Exception:
        return False

def _amap_via_ha(endpoint, params):
    """通过 HA 盒子代理调高德 API。返回 dict 或 None（失败时 fallback 直连）"""
    if not _ensure_tunnel():
        return None
    try:
        cmd = ['sshpass', '-p', os.getenv('HA_SSH_PASS', ''), 'ssh',
               '-o', 'StrictHostKeyChecking=no', '-o', 'ConnectTimeout=5',
               '-p', str(_cf_local_port), 'root@localhost',
               f'curl -s "http://localhost:5004{endpoint}?{urlencode(params)}"']
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout)
            if isinstance(data, dict) and 'error' not in data:
                return data
    except Exception:
        pass
    return None


def geocode(address: str, city: str = "") -> dict:
    """地名转 GCJ-02 坐标，返回 {"location": "lng,lat", "formatted_address": "...", "name": "..."}"""
    params = {"address": address, "key": AMAP_KEY, "output": "json"}
    if city:
        params["city"] = city
    # Try HA proxy first
    ha_data = _amap_via_ha("/geocode", {"address": address, "city": city or ""})
    data = ha_data if ha_data else None
    if not data:
        r = requests.get(f"{BASE}/v3/geocode/geo", params=params, timeout=8)
        r.raise_for_status()
        data = r.json()
    if data.get("status") != "1" or not data.get("geocodes"):
        return {"error": f"geocode failed: {data.get('info', 'no result')} for '{address}'"}
    g = data["geocodes"][0]
    return {
        "location": g["location"],  # "lng,lat" GCJ-02
        "formatted_address": g.get("formatted_address", address),
        "name": address,
    }


def plan_route(origin: str, destination: str, mode: str = "driving", city: str = "") -> dict:
    """
    路径规划。origin/destination 均为 "lng,lat"（GCJ-02）。
    mode: driving | walking | riding | transit
    返回摘要 + steps
    """
    mode_map = {
        "driving": ("/v3/direction/driving", {"strategy": 0}),
        "walking": ("/v3/direction/walking", {}),
        "riding":  ("/v4/direction/bicycling", {}),
        "transit": ("/v3/direction/transit/integrated", {"city": city, "cityd": city}),
    }
    if mode not in mode_map:
        return {"error": f"unsupported mode: {mode}"}
    path_url, extra = mode_map[mode]
    params = {"origin": origin, "destination": destination, "key": AMAP_KEY, "output": "json", **extra}
    if mode == "transit" and city:
        params["city"] = city
        params["cityd"] = city

    # Try HA proxy first
    ha_params = {"origin": origin, "destination": destination, "mode": mode}
    if mode == "transit" and city:
        ha_params["city"] = city
    ha_data = _amap_via_ha("/route", ha_params)
    data = ha_data if ha_data else None
    if not data:
        r = requests.get(f"{BASE}{path_url}", params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
    if data.get("status") != "1":
        return {"error": f"route failed: {data.get('info', 'unknown')}"}

    if mode == "transit":
        routes = data.get("route", {}).get("transits", [])
        if not routes:
            return {"error": "no transit route found"}
        best = routes[0]
        return {
            "mode": mode,
            "distance_m": int(data["route"].get("distance", 0)),
            "duration_s": int(best.get("duration", 0)),
            "cost": float(best.get("cost", 0)),
            "walking_distance_m": int(best.get("walking_distance", 0)),
            "segments": [
                {
                    "type": seg.get("bus", {}).get("type", "walk"),
                    "name": (seg.get("bus", {}).get("buslines") or [{}])[0].get("name", "步行"),
                    "distance_m": int(seg.get("distance", 0) or 0),
                }
                for seg in best.get("segments", [])
            ],
        }
    else:
        paths = data.get("route", {}).get("paths", [])
        if not paths:
            return {"error": "no route path found"}
        p = paths[0]
        steps = []
        for s in p.get("steps", []):
            steps.append({
                "instruction": s.get("instruction", ""),
                "road": s.get("road", ""),
                "distance_m": int(s.get("distance", 0) or 0),
            })
        return {
            "mode": mode,
            "distance_m": int(p.get("distance", 0) or 0),
            "duration_s": int(p.get("duration", 0) or 0),
            "tolls": float(p.get("tolls", 0) or 0),
            "steps": steps,
        }


def build_amap_uri(from_loc: str, to_loc: str, from_name: str, to_name: str,
                   mode: str = "car", via: str = "") -> str:
    """
    拼高德唤起链接。from_loc/to_loc 格式 "lng,lat"（GCJ-02）。
    mode: car | bus | walk | ride
    手动拼 query string，避免 urlencode 对已编码中文二次编码。
    """
    mode_map = {"driving": "car", "walking": "walk", "riding": "ride", "transit": "bus",
                "car": "car", "walk": "walk", "ride": "ride", "bus": "bus"}
    amap_mode = mode_map.get(mode, "car")

    parts = []
    if from_loc:
        parts.append(f"from={from_loc},{quote(from_name)}")
    parts.append(f"to={to_loc},{quote(to_name)}")
    if via:
        parts.append(f"via={via}")
    parts += [
        f"mode={amap_mode}",
        "policy=0",
        "src=weixin_agent",
        "coordinate=gaode",
        "callnative=1",
    ]
    return "https://uri.amap.com/navigation?" + "&".join(parts)


def search_poi(keyword: str, city: str = "", center: str = "", radius: int = 3000, page_size: int = 5) -> dict:
    """POI 搜索，返回带 marker 链接的列表"""
    endpoint = "/v3/place/around" if center else "/v3/place/text"
    params = {
        "keywords": keyword,
        "key": AMAP_KEY,
        "output": "json",
        "extensions": "base",
        "offset": page_size,
        "page": 1,
    }
    if city:
        params["city"] = city
    if center:
        params["location"] = center
        params["radius"] = radius
        params["sortrule"] = "distance"
    # Try HA proxy first
    ha_params = {"keyword": keyword, "city": city or ""}
    if center:
        ha_params["center"] = center
        ha_params["radius"] = str(radius)
    ha_data = _amap_via_ha("/poi", ha_params)
    data = ha_data if ha_data else None
    if not data:
        r = requests.get(f"{BASE}{endpoint}", params=params, timeout=8)
        r.raise_for_status()
        data = r.json()
    if data.get("status") != "1":
        return {"error": f"POI search failed: {data.get('info', 'unknown')}"}

    results = []
    for p in data.get("pois", []):
        loc = p.get("location", "")
        name = p.get("name", "")
        address = p.get("address", "")
        marker_uri = (
            f"https://uri.amap.com/marker?position={loc}"
            f"&name={quote(name)}&coordinate=gaode&callnative=1"
        )
        navi_uri = build_amap_uri("", loc, "", name, "car") if loc else ""
        results.append({
            "name": name,
            "address": address,
            "location": loc,
            "distance_m": int(p.get("distance", 0) or 0) if center else None,
            "marker_uri": marker_uri,
            "navi_uri": navi_uri,
        })
    return {"pois": results, "count": len(results)}


def format_route_summary(route: dict, from_name: str, to_name: str) -> str:
    """把路线数据格式化成可读文字摘要"""
    if "error" in route:
        return f"❌ 路线规划失败：{route['error']}"

    mode = route.get("mode", "driving")
    dist_km = route["distance_m"] / 1000
    dur_min = route["duration_s"] // 60

    icons = {"driving": "🚗", "walking": "🚶", "riding": "🚴", "transit": "🚌"}
    icon = icons.get(mode, "🗺️")

    lines = [f"{icon} {from_name} → {to_name}"]
    lines.append(f"全程约 {dist_km:.1f} km，预计 {dur_min} 分钟")

    if mode == "driving":
        tolls = route.get("tolls", 0)
        lines.append("无过路费" if tolls == 0 else f"过路费约 ¥{tolls:.0f}")
        roads = [s["road"] for s in route.get("steps", []) if s.get("road")]
        if roads:
            main_roads = list(dict.fromkeys(roads))[:4]  # 去重取前4条
            lines.append("途经：" + " → ".join(main_roads))
    elif mode == "transit":
        walk_m = route.get("walking_distance_m", 0)
        lines.append(f"步行约 {walk_m} 米")
        segs = [s["name"] for s in route.get("segments", []) if s.get("name")]
        if segs:
            lines.append("乘坐：" + " → ".join(segs))

    return "\n".join(lines)


# ── CLI 入口 ────────────────────────────────────────────────

def cmd_geocode(args):
    result = geocode(args.address, args.city)
    print(json.dumps(result, ensure_ascii=False))

def cmd_route(args):
    # 先地理编码
    from_geo = geocode(args.from_place, args.city)
    to_geo = geocode(args.to_place, args.city)
    if "error" in from_geo:
        print(json.dumps({"error": from_geo["error"]}, ensure_ascii=False)); return
    if "error" in to_geo:
        print(json.dumps({"error": to_geo["error"]}, ensure_ascii=False)); return

    via_str = ""
    if args.via:
        via_geo = geocode(args.via, args.city)
        if "error" in via_geo:
            print(json.dumps({"error": via_geo["error"]}, ensure_ascii=False)); return
        via_str = f"{via_geo['location']},{quote(args.via)}"

    route = plan_route(from_geo["location"], to_geo["location"], args.mode, args.city)
    summary = format_route_summary(route, args.from_place, args.to_place)
    uri = build_amap_uri(
        from_geo["location"], to_geo["location"],
        args.from_place, args.to_place, args.mode, via=via_str
    )
    print(json.dumps({
        "summary": summary,
        "uri": uri,
        "route": route,
        "from": from_geo,
        "to": to_geo,
    }, ensure_ascii=False))

def cmd_uri(args):
    via_str = f"{args.via_loc},{quote(args.via_name)}" if args.via_loc else ""
    uri = build_amap_uri(args.from_loc, args.to_loc, args.from_name, args.to_name, args.mode, via=via_str)
    print(json.dumps({"uri": uri}, ensure_ascii=False))

def cmd_poi(args):
    result = search_poi(args.keyword, args.city, args.center, args.radius, args.page_size)
    print(json.dumps(result, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description="高德地图导航工具")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # geocode
    p = sub.add_parser("geocode", help="地名转坐标")
    p.add_argument("--address", required=True)
    p.add_argument("--city", default="")

    # route
    p = sub.add_parser("route", help="路线规划 + 唤起链接")
    p.add_argument("--from", dest="from_place", required=True)
    p.add_argument("--to", dest="to_place", required=True)
    p.add_argument("--via", default="", help="途经点地名（可选）")
    p.add_argument("--city", default="")
    p.add_argument("--mode", default="driving",
                   choices=["driving", "walking", "riding", "transit"])

    # uri
    p = sub.add_parser("uri", help="直接拼唤起链接（已有坐标时用）")
    p.add_argument("--from-loc", default="")
    p.add_argument("--from-name", default="")
    p.add_argument("--to-loc", required=True)
    p.add_argument("--to-name", required=True)
    p.add_argument("--via-loc", default="", help="途经点坐标 lng,lat（可选）")
    p.add_argument("--via-name", default="", help="途经点名称（可选）")
    p.add_argument("--mode", default="car")

    # poi
    p = sub.add_parser("poi", help="POI 搜索")
    p.add_argument("--keyword", required=True)
    p.add_argument("--city", default="")
    p.add_argument("--center", default="", help="lng,lat，用于周边搜索")
    p.add_argument("--radius", type=int, default=3000)
    p.add_argument("--page-size", type=int, default=5)

    args = parser.parse_args()
    {"geocode": cmd_geocode, "route": cmd_route, "uri": cmd_uri, "poi": cmd_poi}[args.cmd](args)


if __name__ == "__main__":
    main()
