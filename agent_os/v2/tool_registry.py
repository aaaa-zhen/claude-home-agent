"""Tool Registry (step 1).

把 v1 瘦 prompt 砍掉的能力重新登记成结构化清单,渲染进主脑上下文。
每个工具 = 单次确定性能力(decision: Tool 直调,不开 worker)。
新增工具只改这里,不动主脑代码。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Tool:
    name: str
    trigger: str          # 什么时候用
    command: str          # 怎么调(命令模板)
    verify: str = ""      # 验证纪律(可空)


# 起步工具集:现有 ha-fast / weather / amap / stock / bus / flight / preply / send_file。
# 顺序无所谓,渲染时按注册顺序。
REGISTRY: list[Tool] = [
    Tool(
        name="ha_status",
        trigger="查家居状态:室温、设备开没开、空调温度",
        command='node scripts/ha-fast-status.mjs --json',
        verify="读 JSON 实时状态后再回答,不要凭记忆说设备开关。",
    ),
    Tool(
        name="ha_control",
        trigger="控制家居:开关灯/空调、调温、调风、关全部设备",
        command='node scripts/ha-fast-control.mjs --json -- "清楚的中文控制指令"   # 把用户意图整理通顺,如:把书房空调调到24度;别塞不通顺/中英混杂原话(会 handled=false)',
        verify="必须 verification.confirmed=true 才算成功;handled=false 或没 confirmed 都视为没做成,如实说没设置成功、别假报已完成。",
    ),
    Tool(
        name="weather",
        trigger="天气、会不会下雨、明天穿什么",
        command='./venv/bin/python tools/info/weather.py now --city 城市   # 或 forecast --city 城市 --days 3',
        verify="输出可直接发用户。",
    ),
    Tool(
        name="amap_nav",
        trigger="导航、路线、附近的店/餐厅/咖啡",
        command=(
            './venv/bin/python tools/travel/amap_nav.py route --from "" --to "目的地" --city 珠海 --mode driving|walking|riding|transit'
            '   # POI: tools/travel/amap_nav.py poi --keyword 星巴克 --city 珠海'
        ),
        verify="回复必须裸露高德 uri(微信不渲染 markdown);起点留空用当前位置;坐标已是 GCJ-02。",
    ),
    Tool(
        name="stock",
        trigger="大盘、股票、行情、某只股票多少钱、创业板涨跌",
        command='/Users/zhen/.local/bin/stockctl [代码或别名]   # 默认三大指数;--json 结构化',
        verify="数据源腾讯财经,实时;不要用 WebSearch 也不要编工具。",
    ),
    Tool(
        name="bus_realtime",
        trigger="公交、几路车、车到哪了、附近公交站、坐什么车去 X",
        command='curl -s "http://127.0.0.1:8080/bus/stops?lat=纬度&lng=经度&cityId=242"   # WGS-84 直传',
        verify="按到站时间排序,最快的放前面;不要推荐'等待发车'的线路。",
    ),
    Tool(
        name="flight",
        trigger="X 到 X 有什么航班、几号的机票",
        command='source venv/bin/activate && python3 tools/travel/flight.py search --from 城市 --to 城市 --date YYYY-MM-DD',
        verify="返回 summary 可直接发;429 等 10 秒重试一次。",
    ),
    Tool(
        name="preply",
        trigger="Preply 查课表、给某老师发消息、列联系人",
        command='node scripts/agent-browser-preply.mjs status|list-tutors|send-message --tutor "老师名" --message "原文" [--apply]',
        verify="用已登录的浏览器环境;先 preview;用户明确授权收件人和原文后才 --apply;检查 status=sent 且 confirmed=true。",
    ),
    Tool(
        name="one_time_reminder",
        trigger="提醒我、到点叫我、定时发微信；用户只说'你提醒我'时先结合上一轮日程/截图推断事项",
        command='crontab 一次性任务调用 node scripts/send-once-reminder.mjs --id <唯一id> --message "提醒内容"',
        verify="设完必须 crontab -l 验证。若上一轮已明确事项但缺少提前量,默认提前 30 分钟；不要重新问'提醒啥'。",
    ),
    Tool(
        name="publish_preview",
        trigger='发布/预览网页或项目、"发我链接"、"发布出来"、要在手机/外网打开某个页面或项目',
        command='node scripts/publish-preview.mjs --source <html文件或构建好的dist目录> --slug 名字 --json',
        verify="只能用这个出 https://your-api-domain.example.com/preview/<slug>/?k=... 外链(走 cloudflared 隧道,手机可达)。"
               "绝不回 localhost/127.0.0.1/局域网 IP/localtunnel。React/Vite 项目先 npm run build 再发 dist 目录。",
    ),
    Tool(
        name="send_file",
        trigger="把某张图/文件发给用户",
        command='先 ./venv/bin/python core/local_file_tool.py resolve --query "用户原话" --json 找路径,再 test -f 验证',
        verify='最终回复加 `给你:[send_file:/absolute/path]`,不要贴文件内容;每条回复只发一个文件。',
    ),
    Tool(
        name="memory_recall",
        trigger='用户提到"上次/之前/我们聊过/那个…",需要回忆过去某次对话内容',
        command='./venv/bin/python -m agent_os.v2 recall "关键词"',
        verify="跨会话检索历史对话(jieba 中文检索);先用它找回上下文再回答,别说不记得。",
    ),
    Tool(
        name="memory_write",
        trigger='用户说"记下来/记住/以后…"或纠正叫法、偏好、操作习惯',
        command='写入合适的 memory/ 文件(devices.md / user-profile.md / learned-facts.md / pending-followups.md)',
        verify="先写记忆再回复;保存真实语义,不要把临时说法当固定口令。",
    ),
]


def render() -> str:
    """渲染成主脑 prompt 里的工具清单段。"""
    lines = ["可用工具(单次确定性能力,需要时直接调,调完按 verify 验证):"]
    for t in REGISTRY:
        lines.append(f"- **{t.name}** — {t.trigger}")
        lines.append(f"    调用:{t.command}")
        if t.verify:
            lines.append(f"    验证:{t.verify}")
    return "\n".join(lines)


def names() -> list[str]:
    return [t.name for t in REGISTRY]
