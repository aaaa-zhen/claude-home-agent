#!/usr/bin/env python3
"""
weather.py — 天气查询（wttr.in，无需 API key）
用法:
  python3 weather.py now [--city 珠海]
  python3 weather.py forecast [--city 珠海] [--days 3]

输出: 可直接发给用户的文字
"""

import sys
import argparse
import json
from urllib.request import urlopen
from urllib.parse import quote

WEATHER_CODE = {
    113: "☀️ 晴", 116: "⛅ 多云", 119: "☁️ 阴", 122: "🌫️ 浓云",
    143: "🌫️ 雾", 176: "🌦️ 零星小雨", 179: "🌨️ 零星小雪",
    182: "🌧️ 冻雨", 185: "🌧️ 冻毛毛雨", 200: "⛈️ 雷阵雨",
    227: "🌨️ 飘雪", 230: "❄️ 暴雪", 248: "🌫️ 雾", 260: "🌫️ 冻雾",
    263: "🌦️ 毛毛雨", 266: "🌧️ 毛毛雨", 281: "🌧️ 冻毛毛雨",
    284: "🌧️ 冻毛毛雨", 293: "🌧️ 小雨", 296: "🌧️ 小雨",
    299: "🌧️ 中雨", 302: "🌧️ 中雨", 305: "🌧️ 大雨",
    308: "🌧️ 大雨", 311: "🌧️ 冻雨", 314: "🌧️ 冻雨",
    317: "🌧️ 小冻雨", 320: "🌨️ 小雪", 323: "🌨️ 小雪",
    326: "🌨️ 小雪", 329: "❄️ 中雪", 332: "❄️ 中雪",
    335: "❄️ 大雪", 338: "❄️ 大雪", 350: "🌧️ 冰雹",
    353: "🌦️ 小阵雨", 356: "🌧️ 中阵雨", 359: "🌧️ 大阵雨",
    362: "🌧️ 小阵冻雨", 365: "🌧️ 中阵冻雨", 368: "🌨️ 小阵雪",
    371: "❄️ 中阵雪", 374: "🌧️ 小阵冰雹", 377: "🌧️ 中阵冰雹",
    386: "⛈️ 雷阵雨", 389: "⛈️ 雷暴大雨", 392: "⛈️ 雷阵雪",
    395: "⛈️ 雷暴大雪",
}

WIND_DIR = {
    "N": "北", "NNE": "北偏东", "NE": "东北", "ENE": "东偏北",
    "E": "东", "ESE": "东偏南", "SE": "东南", "SSE": "南偏东",
    "S": "南", "SSW": "南偏西", "SW": "西南", "WSW": "西偏南",
    "W": "西", "WNW": "西偏北", "NW": "西北", "NNW": "北偏西",
}


def fetch(city: str) -> dict:
    url = f"https://wttr.in/{quote(city)}?format=j1"
    with urlopen(url, timeout=10) as resp:
        return json.load(resp)


def fmt_weather(code: int) -> str:
    return WEATHER_CODE.get(code, "🌤️ 未知")


def cmd_now(city: str) -> str:
    data = fetch(city)
    c = data["current_condition"][0]
    today = data["weather"][0]
    astronomy = today["astronomy"][0]

    temp = c["temp_C"]
    feels = c["FeelsLikeC"]
    desc = fmt_weather(int(c["weatherCode"]))
    humidity = c["humidity"]
    wind_speed = c["windspeedKmph"]
    wind_dir = WIND_DIR.get(c["winddir16Point"], c["winddir16Point"])
    low = today.get("mintempC", "?")
    high = today.get("maxtempC", "?")
    sunrise = astronomy["sunrise"]
    sunset = astronomy["sunset"]

    # 下雨概率（取今日最高时段）
    rain_chances = [int(h["chanceofrain"]) for h in today["hourly"]]
    max_rain = max(rain_chances) if rain_chances else 0
    rain_tip = f"☔ 今日最高降雨概率 {max_rain}%" if max_rain >= 30 else ""

    lines = [
        f"📍 {city} 当前天气",
        f"{desc}  {temp}°C（体感 {feels}°C）",
        f"今日 {low}°C ~ {high}°C",
        f"湿度 {humidity}%  {wind_dir}风 {wind_speed} km/h",
        f"🌅 日出 {sunrise}  🌇 日落 {sunset}",
    ]
    if rain_tip:
        lines.append(rain_tip)
    return "\n".join(lines)


def cmd_forecast(city: str, days: int = 3) -> str:
    data = fetch(city)
    lines = [f"📍 {city} {days}天预报"]
    for w in data["weather"][:days]:
        date = w["date"][5:]  # MM-DD
        low = w["mintempC"]
        high = w["maxtempC"]
        avg_code = int(w["hourly"][4]["weatherCode"])  # 正午时段
        desc = fmt_weather(avg_code)
        rain_chances = [int(h["chanceofrain"]) for h in w["hourly"]]
        max_rain = max(rain_chances)
        rain = f" ☔{max_rain}%" if max_rain >= 30 else ""
        lines.append(f"{date}  {desc}  {low}~{high}°C{rain}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="天气查询")
    parser.add_argument("cmd", choices=["now", "forecast"])
    parser.add_argument("--city", default="珠海")
    parser.add_argument("--days", type=int, default=3)
    args = parser.parse_args()

    if args.cmd == "now":
        print(cmd_now(args.city))
    else:
        print(cmd_forecast(args.city, args.days))


if __name__ == "__main__":
    main()
