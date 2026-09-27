#!/usr/bin/env python3
"""
Lightweight 12306 train query proxy.
Runs on HA box (domestic network), exposes HTTP API for VPS to call.

Usage:
  python3 train_proxy.py          # starts on port 5001
  curl "http://localhost:5001/query?from=ZHQ&to=ZZF&date=2026-04-29"
"""

from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import urllib.request
import json
import re

STATION_CACHE = {}  # name -> code

def load_stations():
    """Load station name -> code mapping from 12306."""
    global STATION_CACHE
    try:
        req = urllib.request.Request(
            "https://kyfw.12306.cn/otn/resources/js/framework/station_name.js",
            headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            text = r.read().decode("utf-8")
        for item in text.split("@")[1:]:
            parts = item.split("|")
            if len(parts) >= 3:
                STATION_CACHE[parts[1]] = parts[2]  # 中文名 -> 电报码
        print(f"Loaded {len(STATION_CACHE)} stations")
    except Exception as e:
        print(f"Failed to load stations: {e}")


def query_tickets(from_code, to_code, date):
    """Query 12306 for available tickets."""
    url = (
        f"https://kyfw.12306.cn/otn/leftTicket/queryG?"
        f"leftTicketDTO.train_date={date}&"
        f"leftTicketDTO.from_station={from_code}&"
        f"leftTicketDTO.to_station={to_code}&"
        f"purpose_codes=ADULT"
    )
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://kyfw.12306.cn/otn/leftTicket/init",
        "Accept": "application/json",
        "Cookie": "RAIL_EXPIRATION=0; RAIL_DEVICEID=0"
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        # Try alternative endpoint
        url2 = url.replace("queryG", "query")
        req2 = urllib.request.Request(url2, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://kyfw.12306.cn/otn/leftTicket/init",
        })
        with urllib.request.urlopen(req2, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8"))

    results = []
    for item in data.get("data", {}).get("result", []):
        fields = item.split("|")
        if len(fields) < 35:
            continue
        train = {
            "train_no": fields[3],         # 车次 e.g. G1234
            "from_station": fields[6],      # 出发站代码
            "to_station": fields[7],        # 到达站代码
            "depart_time": fields[8],       # 出发时间
            "arrive_time": fields[9],       # 到达时间
            "duration": fields[10],         # 历时
            "date": fields[13],             # 日期
            "business_seat": fields[32] or "-",    # 商务座
            "first_seat": fields[31] or "-",       # 一等座
            "second_seat": fields[30] or "-",      # 二等座
            "soft_sleeper": fields[23] or "-",     # 软卧
            "hard_sleeper": fields[28] or "-",     # 硬卧
            "hard_seat": fields[29] or "-",        # 硬座
            "no_seat": fields[26] or "-",          # 无座
            "can_buy": fields[1] == "Y",
        }
        results.append(train)

    # Map station codes back to names
    code_to_name = {v: k for k, v in STATION_CACHE.items()}
    station_map = data.get("data", {}).get("map", {})
    code_to_name.update(station_map)

    for t in results:
        t["from_name"] = code_to_name.get(t["from_station"], t["from_station"])
        t["to_name"] = code_to_name.get(t["to_station"], t["to_station"])

    return results


def resolve_station(name_or_code):
    """Resolve station name to code, or return as-is if already a code."""
    if re.match(r'^[A-Z]{3}$', name_or_code):
        return name_or_code
    return STATION_CACHE.get(name_or_code, name_or_code)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/query":
            params = parse_qs(parsed.query)
            from_s = params.get("from", [""])[0]
            to_s = params.get("to", [""])[0]
            date = params.get("date", [""])[0]

            if not all([from_s, to_s, date]):
                self._json(400, {"error": "Missing params: from, to, date"})
                return

            from_code = resolve_station(from_s)
            to_code = resolve_station(to_s)

            try:
                results = query_tickets(from_code, to_code, date)
                self._json(200, {"count": len(results), "trains": results})
            except Exception as e:
                self._json(500, {"error": str(e)})

        elif parsed.path == "/stations":
            query = parse_qs(parsed.query).get("q", [""])[0]
            matches = {k: v for k, v in STATION_CACHE.items() if query in k}
            self._json(200, matches)

        elif parsed.path == "/health":
            self._json(200, {"status": "ok", "stations": len(STATION_CACHE)})

        else:
            self._json(404, {"error": "Not found. Use /query?from=X&to=Y&date=YYYY-MM-DD"})

    def _json(self, code, data):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def log_message(self, format, *args):
        print(f"{args[0]}")


if __name__ == "__main__":
    load_stations()
    port = 5001
    server = HTTPServer(("0.0.0.0", port), Handler)
    print(f"Train proxy running on port {port}")
    server.serve_forever()
