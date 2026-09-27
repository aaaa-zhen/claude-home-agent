#!/usr/bin/env python3
"""
flight.py — 航班查询工具（基于 RapidAPI flights-sky）
用法:
  python3 flight.py search --from 广州 --to 北京 --date 2026-05-20 [--trip round] [--cabin economy]
  python3 flight.py airports --query 广州

输出: JSON，供 Claude 解析
"""

import sys
import os
import json
import argparse
import time

import requests

# 从 .env 读取 key
def load_env():
    env = {}
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return env

_env = load_env()
RAPIDAPI_KEY = os.environ.get("RAPIDAPI_KEY") or _env.get("RAPIDAPI_KEY", "")
HOST = "flights-sky.p.rapidapi.com"
BASE = f"https://{HOST}"
HEADERS = {"x-rapidapi-key": RAPIDAPI_KEY, "x-rapidapi-host": HOST}


def search_airport(query: str) -> list:
    """搜索机场，返回 [{"name", "skyId", "entityId", "city", "country"}]"""
    r = requests.get(f"{BASE}/flights/auto-complete",
                     params={"query": query}, headers=HEADERS, timeout=10)
    r.raise_for_status()
    data = r.json()
    results = []
    for item in (data.get("data") or []):
        p = item["presentation"]
        n = item["navigation"]
        results.append({
            "name": p.get("suggestionTitle", p["title"]),
            "skyId": p["skyId"],
            "entityId": p["id"],          # base64，用于 search API
            "city": p["title"],
            "country": p.get("subtitle", ""),
        })
    return results


def find_airport(query: str) -> dict:
    """找最匹配的机场，返回单个结果，找不到抛异常"""
    results = search_airport(query)
    if not results:
        raise ValueError(f"找不到机场：{query}")
    return results[0]


def search_flights(from_query: str, to_query: str, depart_date: str,
                   return_date: str = "", cabin: str = "economy",
                   currency: str = "CNY", max_results: int = 5) -> dict:
    """
    查询航班。
    depart_date: YYYY-MM-DD
    return_date: YYYY-MM-DD，有则查往返，无则查单程
    cabin: economy | business | first
    """
    # 解析机场
    try:
        orig = find_airport(from_query)
        dest = find_airport(to_query)
    except ValueError as e:
        return {"error": str(e)}

    params = {
        "fromEntityId": orig["entityId"],
        "toEntityId": dest["entityId"],
        "fromSkyId": orig["skyId"],
        "toSkyId": dest["skyId"],
        "departDate": depart_date,
        "currency": currency,
        "cabinClass": cabin,
    }

    if return_date:
        params["returnDate"] = return_date
        endpoint = f"{BASE}/flights/search-roundtrip"
    else:
        endpoint = f"{BASE}/flights/search-one-way"

    r = requests.get(endpoint, params=params, headers=HEADERS, timeout=20)
    r.raise_for_status()
    data = r.json()

    if not data.get("status", True) and data.get("errors"):
        return {"error": str(data["errors"])}

    itineraries = (data.get("data") or {}).get("itineraries", [])
    if not itineraries:
        return {"error": "没有找到可用航班", "from": orig, "to": dest}

    flights = []
    for itin in itineraries[:max_results]:
        leg = itin["legs"][0]
        carriers = [c["name"] for c in leg.get("carriers", {}).get("marketing", [])]
        segments = leg.get("segments", [])
        seg_info = [
            {
                "flight_no": f"{s.get('marketingCarrier', {}).get('alternateId', '')}{s.get('flightNumber', '')}",
                "from": s["origin"]["displayCode"],
                "to": s["destination"]["displayCode"],
                "depart": s["departure"],
                "arrive": s["arrival"],
            }
            for s in segments
        ]
        flights.append({
            "price": itin["price"]["formatted"],
            "price_raw": itin["price"]["raw"],
            "airline": "、".join(carriers) if carriers else "未知",
            "depart": leg["departure"],
            "arrive": leg["arrival"],
            "duration_min": leg["durationInMinutes"],
            "stops": leg["stopCount"],
            "stops_label": "直飞" if leg["stopCount"] == 0 else f"{leg['stopCount']}经停",
            "segments": seg_info,
        })

    return {
        "from": f"{orig['city']} ({orig['skyId']})",
        "to": f"{dest['city']} ({dest['skyId']})",
        "date": depart_date,
        "return_date": return_date or None,
        "cabin": cabin,
        "flights": flights,
    }


def format_flight_summary(result: dict) -> str:
    """把查询结果格式化为可读文字"""
    if "error" in result:
        return f"❌ 航班查询失败：{result['error']}"

    trip_type = "往返" if result.get("return_date") else "单程"
    lines = [f"✈️ {result['from']} → {result['to']}  {result['date']}  {trip_type}"]
    lines.append("")

    for i, f in enumerate(result["flights"], 1):
        dur_h, dur_m = divmod(f["duration_min"], 60)
        depart_t = f["depart"][11:16] if len(f["depart"]) >= 16 else f["depart"]
        arrive_t = f["arrive"][11:16] if len(f["arrive"]) >= 16 else f["arrive"]
        lines.append(
            f"{i}. {f['airline']}  {depart_t}→{arrive_t}  {dur_h}h{dur_m:02d}m  {f['stops_label']}  {f['price']}"
        )
        if f["segments"]:
            fn = f["segments"][0].get("flight_no", "")
            if fn:
                lines.append(f"   航班号：{fn}")

    return "\n".join(lines)


# ── CLI ────────────────────────────────────────────────────

def cmd_airports(args):
    results = search_airport(args.query)
    print(json.dumps(results[:5], ensure_ascii=False))


def cmd_search(args):
    result = search_flights(
        from_query=args.from_place,
        to_query=args.to_place,
        depart_date=args.date,
        return_date=args.return_date or "",
        cabin=args.cabin,
        max_results=args.top,
    )
    summary = format_flight_summary(result)
    print(json.dumps({"summary": summary, "data": result}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description="航班查询工具")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("airports", help="搜索机场")
    p.add_argument("--query", required=True, help="城市或机场名（中英文均可）")

    p = sub.add_parser("search", help="查询航班")
    p.add_argument("--from", dest="from_place", required=True, help="出发地")
    p.add_argument("--to", dest="to_place", required=True, help="目的地")
    p.add_argument("--date", required=True, help="出发日期 YYYY-MM-DD")
    p.add_argument("--return-date", default="", help="返回日期（往返票用）")
    p.add_argument("--cabin", default="economy",
                   choices=["economy", "business", "first"], help="舱位")
    p.add_argument("--top", type=int, default=5, help="返回最多几条结果")

    args = parser.parse_args()
    {"airports": cmd_airports, "search": cmd_search}[args.cmd](args)


if __name__ == "__main__":
    main()
