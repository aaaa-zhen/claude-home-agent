#!/usr/bin/env python3
"""A 股 / 指数实时行情查询。

数据源：腾讯财经免费接口 qt.gtimg.cn（免 API key）。
A 股交易时段返回实时数据，盘后返回收盘值。

用法：
  stockctl                      # 默认查三大指数
  stockctl 上证 深证 创业板      # 按别名查指数
  stockctl 600519 000001 sh000300
  stockctl --json 600519        # 输出 JSON
  stockctl --fund 008887 000307 # 查基金实时估值（天天基金接口）
"""
import sys
import json
import urllib.request

# 指数/常用别名
ALIASES = {
    "上证": "sh000001", "上证指数": "sh000001", "sh": "sh000001", "大盘": "sh000001",
    "深证": "sz399001", "深证成指": "sz399001", "sz": "sz399001",
    "创业板": "sz399006", "创业板指": "sz399006", "cyb": "sz399006",
    "沪深300": "sh000300", "hs300": "sh000300",
    "科创50": "sh000688", "上证50": "sh000016",
    "北证50": "bj899050",
}


def normalize(code):
    c = code.strip().lower()
    if c in ALIASES:
        return ALIASES[c]
    if c in {k.lower(): v for k, v in ALIASES.items()}:
        return {k.lower(): v for k, v in ALIASES.items()}[c]
    if c.startswith(("sh", "sz", "bj")):
        return c
    # 纯数字 6 位，自动判断市场
    if c.isdigit() and len(c) == 6:
        if c.startswith("6"):
            return "sh" + c
        if c.startswith(("0", "2", "3")):
            return "sz" + c
        if c.startswith(("4", "8")):
            return "bj" + c
    return c


def fetch(codes):
    url = "http://qt.gtimg.cn/q=" + ",".join(codes)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    raw = urllib.request.urlopen(req, timeout=10).read().decode("gbk", "ignore")
    out = []
    for line in raw.strip().split("\n"):
        if "=" not in line:
            continue
        payload = line.split("=", 1)[1].strip().strip('";')
        parts = payload.split("~")
        if len(parts) < 6 or not parts[3]:
            continue
        try:
            name = parts[1]
            code = parts[2]
            price = float(parts[3])
            prev = float(parts[4])
            change = price - prev
            pct = (change / prev * 100) if prev else 0.0
            high = float(parts[33]) if len(parts) > 33 and parts[33] else None
            low = float(parts[34]) if len(parts) > 34 and parts[34] else None
            ts = parts[30] if len(parts) > 30 else ""
            out.append({
                "name": name, "code": code, "price": price,
                "prev_close": prev, "change": round(change, 2),
                "pct": round(pct, 2), "high": high, "low": low, "time": ts,
            })
        except (ValueError, IndexError):
            continue
    return out


def fetch_funds(codes):
    out = []
    for code in codes:
        url = f"http://fundgz.1234567.com.cn/js/{code}.js"
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0",
                              "Referer": "http://fund.eastmoney.com/"})
            raw = urllib.request.urlopen(req, timeout=10).read().decode("utf-8", "ignore")
            payload = raw.replace("jsonpgz(", "").rstrip(");").strip()
            d = json.loads(payload)
            out.append({
                "code": d["fundcode"], "name": d["name"],
                "nav": float(d["dwjz"]), "est": float(d["gsz"]),
                "pct": float(d["gszzl"]), "time": d["gztime"],
            })
        except Exception:
            out.append({"code": code, "name": code, "error": True})
    return out


def render_funds(rows):
    lines = []
    last_time = ""
    for r in rows:
        if r.get("error"):
            lines.append(f"⚠️ {r['code']} 查询失败")
            continue
        arrow = "📈" if r["pct"] > 0 else ("📉" if r["pct"] < 0 else "➖")
        sign = "+" if r["pct"] >= 0 else ""
        lines.append(f"{arrow} {r['name']}（{r['code']}）　估值 {r['est']}　{sign}{r['pct']}%")
        last_time = r.get("time", last_time)
    if last_time:
        lines.append(f"\n更新 {last_time}（盘中为估值，收盘以实际净值为准）")
    return "\n".join(lines)


def fmt_time(ts):
    if len(ts) == 14:
        return f"{ts[4:6]}-{ts[6:8]} {ts[8:10]}:{ts[10:12]}"
    return ts


def render(rows):
    lines = []
    for r in rows:
        arrow = "📈" if r["change"] > 0 else ("📉" if r["change"] < 0 else "➖")
        sign = "+" if r["change"] >= 0 else ""
        line = f"{arrow} {r['name']} {r['price']}　{sign}{r['change']} ({sign}{r['pct']}%)"
        lines.append(line)
    if rows and rows[0].get("time"):
        lines.append(f"\n更新 {fmt_time(rows[0]['time'])}")
    return "\n".join(lines)


def main():
    args = sys.argv[1:]
    as_json = False
    is_fund = False
    if "--json" in args:
        as_json = True
        args.remove("--json")
    if "--fund" in args:
        is_fund = True
        args.remove("--fund")

    if is_fund:
        rows = fetch_funds(args)
        if as_json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
        else:
            print(render_funds(rows))
        return

    targets = args if args else ["上证", "深证", "创业板"]
    codes = [normalize(t) for t in targets]
    rows = fetch(codes)
    if as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        if not rows:
            print("没查到行情，检查代码是否正确。")
            sys.exit(1)
        print(render(rows))


if __name__ == "__main__":
    main()
