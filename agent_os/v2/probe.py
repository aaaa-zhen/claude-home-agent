"""Probe (step 3) —— 不是一层,是个可选工具。

decision five:主脑一眼能判断就直接定 mode;只有"看着像 interactive、但可能很久"时才 probe。
probe 用任务签名 + 实测耗时经验估 ETA(零 LLM、毫秒级),超出 interactive 预算就建议后台化。

经验值来自 v2 实测:家居/闲聊 7–15s、股票 ~26s、写短文 ~15s、导航 60–121s、调研 ~120s。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

# interactive 干等预算:超过就别让用户干瞪眼,转后台 + 完成通知。
# 30s:股票/航班(~25s)留前台等;导航/下载/调研(>=90s)转后台。
INTERACTIVE_BUDGET_SEC = int(os.getenv("V2_INTERACTIVE_BUDGET_SEC", "30"))

# (签名正则, 估计耗时秒, 置信度) —— 命中第一条为准
_SIGNATURES: list[tuple[str, int, float]] = [
    (r"导航|路线|怎么(去|走|坐)|公交|地铁|换乘", 90, 0.7),
    (r"下载|下个视频|爬|抓取", 240, 0.5),
    (r"发布|部署|上线|publish|预览.*(项目|网页|demo)|把.*(项目|网页|demo).*(发|给我)", 90, 0.6),
    (r"调研|研究一下|对比.*(分析|整理)|全球|大范围|扫描", 120, 0.6),
    (r"整理.*文件|批量", 90, 0.6),
    (r"写.{0,15}(论文|报告|网页|页面|代码|脚本)", 90, 0.6),
    (r"写.{0,15}(文章|介绍|总结|文案|攻略|稿)", 18, 0.7),
    (r"航班|机票", 25, 0.6),
    (r"股票|行情|大盘|涨跌|创业板|沪深", 26, 0.7),
    (r"天气|下雨|气温", 12, 0.8),
]


@dataclass
class ProbeResult:
    eta_sec: int
    backgroundable: bool
    confidence: float
    reason: str


def probe(message: str) -> ProbeResult:
    m = message.strip()
    for pattern, eta, conf in _SIGNATURES:
        if re.search(pattern, m):
            bg = eta > INTERACTIVE_BUDGET_SEC
            return ProbeResult(eta, bg, conf, f"签名命中『{pattern[:12]}…』，估计 {eta}s")
    # 没命中签名:默认按短任务,不后台化(低置信)
    return ProbeResult(15, False, 0.3, "无明显慢任务签名，按 interactive 处理")
