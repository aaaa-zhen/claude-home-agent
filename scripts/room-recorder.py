#!/usr/bin/env python3
"""房间事件记录员 v3 —— 看家 + 物品变化时间线

两层逻辑(2026-07-30 与用户确认的隐私边界:记录"物"的变化,不追踪"人"的活动):

1. 动静层(仅离家生效):person.me=not_home 时,画面动静 → 存帧 + 录 10s
   短视频 + 推微信警报。在家时人走动**完全不记录**。
2. 物品层(在家/离家都生效):画面持续安静 SETTLE_TICKS 轮后视为"静场",与上一次
   静场做分块像素对比——少数区块变化 = 有东西出现/消失/挪动,记一条 change 事件
   (存变化前后两张对比图);大面积变化(开关灯/红外切换/转镜头)= 静默刷新基准,
   不算事件。回答"X 是什么时候放这的"就查 change 事件。

抓帧比对全程本地计算,不花 token;只有凌晨日报和用户主动问时才用 AI 看图。

用法:
  ./venv/bin/python scripts/room-recorder.py            # 常驻循环(launchd 托管)
  ./venv/bin/python scripts/room-recorder.py --once     # 只跑一轮(自测;强制按离家逻辑)
  ./venv/bin/python scripts/room-recorder.py --status   # 打印最近事件与配置
"""
import os
import re
import shutil
import sys
import json
import time
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
from PIL import Image
import imagehash

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "media" / "room-log"
EVENTS = LOG_DIR / "events.jsonl"
STATE = LOG_DIR / ".state.json"
STABLE_FRAME = LOG_DIR / ".stable.jpg"   # 上一次静场基准帧
TMP_FRAME = ROOT / "tmp" / "room-recorder-frame.jpg"

# —— 可调参数 ——
INTERVAL = int(os.environ.get("ROOM_INTERVAL", "20"))      # 抓帧间隔（秒）
THRESHOLD = int(os.environ.get("ROOM_THRESHOLD", "12"))    # dHash 汉明距离阈值，越大越不敏感
RETENTION_DAYS = int(os.environ.get("ROOM_RETENTION_DAYS", "14"))
CROP_TOP_FRAC = 0.08   # 裁掉顶部时间戳水印带再比对，避免每秒变化的水印误触发
CLIP_SECONDS = int(os.environ.get("ROOM_CLIP_SECONDS", "10"))          # 事件短视频时长；0=关闭录像
AWAY_PUSH = os.environ.get("ROOM_AWAY_PUSH", "1") == "1"               # 离家时事件帧推微信
AWAY_PUSH_COOLDOWN = int(os.environ.get("ROOM_AWAY_PUSH_COOLDOWN", "300"))  # 推送冷却（秒）
PERSON_ENTITY = "person.me"
WEIXIN_SEND_FILE = ROOT / "weixin-send-file.mjs"
NODE_BIN = "/opt/homebrew/bin/node"

# —— 物品变化层参数 ——
SETTLE_TICKS = int(os.environ.get("ROOM_SETTLE_TICKS", "3"))           # 连续安静几轮算"静场"（3轮≈1分钟）
TILE_GRID = (8, 6)                                                     # 分块网格（横x竖，共48块）
TILE_BLUR = float(os.environ.get("ROOM_TILE_BLUR", "1.5"))             # 比对前高斯模糊,压传感器噪点
TILE_PIX_DELTA = float(os.environ.get("ROOM_TILE_PIX_DELTA", "30"))    # 单像素灰度差超过这个算"变了"
TILE_FRAC = float(os.environ.get("ROOM_TILE_FRAC", "0.05"))            # 块内变化像素占比超过这个块被标记
TILE_GLOBAL_MAX = int(os.environ.get("ROOM_TILE_GLOBAL_MAX", "10"))    # 标记块数超过这个=全局变化（灯光/转镜头），只刷基准不记事件
# 每块自学习噪声底噪(EMA):电脑/电视屏幕、风吹植物这类"自己会动"的区域会学出高门槛,
# 静态区域保持灵敏。判定阈值 = max(TILE_FRAC, 底噪*NOISE_MULT + NOISE_ADD)
NOISE_ALPHA = 0.5        # EMA 更新权重(越大学得越快)
NOISE_MULT = 3.0
NOISE_ADD = 0.02
WARMUP_SETTLES = 2       # 冷启动前几轮静场只学底噪,不记事件


def load_env():
    env = {}
    envfile = ROOT / ".env"
    if envfile.exists():
        for line in envfile.read_text().splitlines():
            m = re.match(r"^([A-Z0-9_]+)=(.*)$", line)
            if m:
                env[m.group(1)] = m.group(2).strip()
    return env


def rtsp_url():
    pwd = load_env().get("CAM_LIVINGROOM_PWD", "")
    return f"rtsp://admin:{pwd}@192.168.1.101:554/stream2"


def grab_frame(dest: Path) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-rtsp_transport", "tcp",
        "-i", rtsp_url(),
        "-frames:v", "1", "-q:v", "3", str(dest),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=25)
        return r.returncode == 0 and dest.exists() and dest.stat().st_size > 0
    except subprocess.TimeoutExpired:
        return False


def frame_hash(path: Path):
    img = Image.open(path).convert("L")
    w, h = img.size
    img = img.crop((0, int(h * CROP_TOP_FRAC), w, h))   # 去掉顶部水印带
    return imagehash.dhash(img, hash_size=16)


def read_state():
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except Exception:
            pass
    return {}


def write_state(d):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(d, ensure_ascii=False))


def prune_old():
    cutoff = datetime.now() - timedelta(days=RETENTION_DAYS)
    for day_dir in LOG_DIR.glob("20*-*-*"):
        if not day_dir.is_dir():
            continue
        try:
            d = datetime.strptime(day_dir.name, "%Y-%m-%d")
        except ValueError:
            continue
        if d < cutoff:
            shutil.rmtree(day_dir, ignore_errors=True)
    # events.jsonl 里指向已删帧的过期行也一起清，不然越攒越多全是死路径
    if EVENTS.exists():
        keep = []
        for ln in EVENTS.read_text().splitlines():
            try:
                ts = datetime.strptime(json.loads(ln)["ts"], "%Y-%m-%d %H:%M:%S")
                if ts >= cutoff:
                    keep.append(ln)
            except Exception:
                keep.append(ln)
        EVENTS.write_text("\n".join(keep) + ("\n" if keep else ""))


# —— 离家安防推送 ——
_away_state = {"value": None, "checked": 0.0, "last_push": 0.0}


def user_is_away() -> bool:
    """查 HA person 实体判断是否离家；查不到一律当在家（宁可漏推不误报）。结果缓存 60s。"""
    now = time.time()
    if now - _away_state["checked"] < 60:
        return _away_state["value"] == "not_home"
    _away_state["checked"] = now
    try:
        import requests
        env = load_env()
        s = requests.Session()
        s.trust_env = False  # 无视 shell 里的 Clash 代理环境变量,HA 走本地隧道直连
        r = s.get(
            f"{env['HA_URL']}/states/{PERSON_ENTITY}",
            headers={"Authorization": f"Bearer {env['HA_TOKEN']}"},
            timeout=8,
        )
        _away_state["value"] = r.json().get("state") if r.ok else None
    except Exception:
        _away_state["value"] = None
    return _away_state["value"] == "not_home"


def maybe_push_away_alert(frame: Path, ts: str):
    """离家状态下把事件帧推到微信（带冷却）。后台执行，不阻塞抓帧循环。"""
    if not AWAY_PUSH or not WEIXIN_SEND_FILE.exists():
        return
    if not user_is_away():
        return
    now = time.time()
    if now - _away_state["last_push"] < AWAY_PUSH_COOLDOWN:
        return
    _away_state["last_push"] = now
    subprocess.Popen(
        [NODE_BIN, str(WEIXIN_SEND_FILE), "--file", str(frame),
         "--text", f"⚠️ 你不在家，客厅刚检测到动静（{ts}）。要不要我盯紧点或转头看看门口？"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=str(ROOT),
    )


# —— 事件短视频 ——
_clip_proc = {"p": None}


def maybe_record_clip(day_dir: Path, stem: str) -> str | None:
    """事件发生时后台录一段子流片段（-c copy 几乎零开销）。上一段还在录就跳过。"""
    if CLIP_SECONDS <= 0:
        return None
    p = _clip_proc["p"]
    if p is not None and p.poll() is None:
        return None
    dest = day_dir / f"{stem}.mp4"
    _clip_proc["p"] = subprocess.Popen(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-rtsp_transport", "tcp", "-i", rtsp_url(),
         "-t", str(CLIP_SECONDS), "-c", "copy", "-movflags", "+faststart", str(dest)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return str(dest.relative_to(ROOT))


def append_event(record: dict):
    EVENTS.parent.mkdir(parents=True, exist_ok=True)
    with EVENTS.open("a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def day_dir_for(now: datetime) -> Path:
    d = LOG_DIR / now.strftime("%Y-%m-%d")
    d.mkdir(parents=True, exist_ok=True)
    return d


def log_motion_event(now: datetime, dist: int) -> str:
    """离家动静事件：存帧 + 录短视频 + 推微信。"""
    day_dir = day_dir_for(now)
    stem = now.strftime("%H-%M-%S")
    dest = day_dir / f"{stem}.jpg"
    shutil.copyfile(TMP_FRAME, dest)
    rel = str(dest.relative_to(ROOT))
    clip_rel = maybe_record_clip(day_dir, stem)
    ts = now.strftime("%Y-%m-%d %H:%M:%S")
    record = {"ts": ts, "type": "motion", "dist": dist, "path": str(dest), "rel": rel}
    if clip_rel:
        record["clip"] = clip_rel
    append_event(record)
    maybe_push_away_alert(dest, ts)
    return rel


# —— 物品变化层 ——

def _tile_image(path: Path) -> np.ndarray:
    from PIL import ImageFilter
    img = Image.open(path).convert("RGB")
    w, h = img.size
    img = img.crop((0, int(h * CROP_TOP_FRAC), w, h)).resize((320, 192))
    if TILE_BLUR > 0:
        img = img.filter(ImageFilter.GaussianBlur(TILE_BLUR))
    return np.asarray(img, dtype=np.float32)   # (192,320,3)


def tile_fracs(path_a: Path, path_b: Path):
    """分块比较两张静场帧,返回每块的强变化像素占比(共 gw*gh 个,按行优先展开)。

    - 彩色三通道分别比,取通道最大差——亮度相近但颜色不同的物体(黄水果放浅色桌面)灰度会隐形,彩色不会
    - 每通道先做全局均值归一化,抵消自动曝光/白平衡在两次静场之间的漂移
    - 用"块内强变化像素占比"而不是块均值——芒果这种小物件只占块内一小片,
      均值会被稀释,占比不会;而光照缓变是大量像素的微小差异,占比条件筛不进来
    """
    a, b = _tile_image(path_a), _tile_image(path_b)
    a = a - a.mean(axis=(0, 1))
    b = b - b.mean(axis=(0, 1))
    changed = (np.abs(a - b).max(axis=2) > TILE_PIX_DELTA)
    gw, gh = TILE_GRID
    th, tw = changed.shape[0] // gh, changed.shape[1] // gw
    fracs = []
    for i in range(gh):
        for j in range(gw):
            fracs.append(float(changed[i * th:(i + 1) * th, j * tw:(j + 1) * tw].mean()))
    return fracs


def stable_compare(now: datetime, st: dict) -> str:
    """画面刚静下来时调用：与上一次静场比,少量区块变了 → 记 change 事件。

    每块维护 EMA 噪声底噪:屏幕/摇动植物这种常年自变的块自动学出高门槛,
    偶发一次的真实物品变化(底噪≈0 的块)照常触发。
    """
    if not STABLE_FRAME.exists():
        shutil.copyfile(TMP_FRAME, STABLE_FRAME)
        return "stable_baseline_set"
    try:
        fracs = tile_fracs(STABLE_FRAME, TMP_FRAME)
    except Exception as e:
        return f"tile_diff_failed:{e}"
    gw, gh = TILE_GRID
    noise = st.get("tile_noise") or [0.0] * (gw * gh)
    if len(noise) != len(fracs):
        noise = [0.0] * len(fracs)
    rounds = int(st.get("settle_rounds", 0)) + 1
    st["settle_rounds"] = rounds

    flagged = []
    for t, frac in enumerate(fracs):
        if frac > max(TILE_FRAC, noise[t] * NOISE_MULT + NOISE_ADD):
            flagged.append((t // gw, t % gw, round(frac, 3)))

    is_global = len(flagged) > TILE_GLOBAL_MAX
    if not is_global:
        # 全局变化(灯光/转镜头)不喂进底噪,免得一次开灯把所有块都学"聋"了
        st["tile_noise"] = [round((1 - NOISE_ALPHA) * n + NOISE_ALPHA * f, 4)
                            for n, f in zip(noise, fracs)]

    if not flagged:
        return "settled no_change"
    if is_global:
        # 留一份被替换的旧基准做取证:全局变化若频繁出现且场景没真变,就是曝光漂移误判,要调归一化
        shutil.copyfile(STABLE_FRAME, LOG_DIR / ".stable-prev.jpg")
        shutil.copyfile(TMP_FRAME, STABLE_FRAME)
        return f"settled global_change tiles={len(flagged)} (基准已刷新,不记事件)"
    if rounds <= WARMUP_SETTLES:
        shutil.copyfile(TMP_FRAME, STABLE_FRAME)
        return f"settled warmup({rounds}/{WARMUP_SETTLES}) tiles={len(flagged)} (只学底噪,不记事件)"
    day_dir = day_dir_for(now)
    stem = now.strftime("%H-%M-%S")
    before = day_dir / f"{stem}-before.jpg"
    after = day_dir / f"{stem}-change.jpg"
    shutil.copyfile(STABLE_FRAME, before)
    shutil.copyfile(TMP_FRAME, after)
    shutil.copyfile(TMP_FRAME, STABLE_FRAME)
    record = {
        "ts": now.strftime("%Y-%m-%d %H:%M:%S"),
        "type": "change",
        "tiles": len(flagged),
        "path": str(after),                      # 统一用 path 指"当前样子",日报/查询按这个走
        "rel": str(after.relative_to(ROOT)),
        "before": str(before),
        "before_rel": str(before.relative_to(ROOT)),
        "away": _away_state["value"] == "not_home",
    }
    append_event(record)
    st["last_change"] = {"ts": record["ts"], "tiles": record["tiles"], "rel": record["rel"]}
    return f"CHANGE tiles={len(flagged)} -> {record['rel']}"


def tick(force_away: bool = False) -> str:
    """跑一轮。返回状态字符串。

    在家:抓帧只做比对,人走动不留任何记录;只有静场间的物品级变化会落盘。
    离家:动静=安防事件(存帧+短视频+推送),物品层照常。
    """
    now = datetime.now()
    away = user_is_away() or force_away
    if not grab_frame(TMP_FRAME):
        return "grab_failed"
    try:
        h = frame_hash(TMP_FRAME)
    except Exception as e:
        return f"hash_failed:{e}"

    st = read_state()
    st["mode"] = "away_guard" if away else "home_objects"
    last = st.get("last_hash")
    if last is None:
        st["last_hash"] = str(h)
        st["quiet_streak"] = 0
        write_state(st)
        return "baseline_set"

    dist = int(h - imagehash.hex_to_hash(last))  # imagehash 差值是 numpy int64,不转 int 会让 json.dumps 崩掉
    st["last_hash"] = str(h)

    if dist >= THRESHOLD:
        st["quiet_streak"] = 0
        if away:
            rel = log_motion_event(now, dist)
            st["last_event"] = {"ts": now.strftime("%Y-%m-%d %H:%M:%S"), "dist": dist, "rel": rel}
            result = f"EVENT dist={dist} -> {rel}"
        else:
            result = f"motion(home,不记录) dist={dist}"
    else:
        st["quiet_streak"] = int(st.get("quiet_streak", 0)) + 1
        if st["quiet_streak"] == SETTLE_TICKS:
            result = stable_compare(now, st)
        else:
            result = f"quiet dist={dist}"

    write_state(st)
    return result


def status():
    st = read_state()
    print("== 房间事件记录员 v3 ==")
    print(f"间隔 {INTERVAL}s | 动静阈值 {THRESHOLD} | 静场 {SETTLE_TICKS} 轮 | 留存 {RETENTION_DAYS} 天")
    away = user_is_away()
    print(f"当前 {'离家(看家中:动静+物品)' if away else '在家(只记物品变化,不追踪人)'} "
          f"(person={_away_state['value']}, state.mode={st.get('mode', '?')})")
    print(f"事件日志: {EVENTS}")
    if EVENTS.exists():
        lines = EVENTS.read_text().splitlines()
        print(f"累计事件: {len(lines)} 条，最近 5 条：")
        for ln in lines[-5:]:
            try:
                e = json.loads(ln)
                kind = e.get("type", "motion")
                extra = f"dist={e['dist']}" if kind == "motion" else f"tiles={e.get('tiles')}"
                print(f"  {e['ts']}  [{kind}] {extra}  {e['rel']}  {e.get('desc', '')[:40]}")
            except Exception:
                pass
    else:
        print("暂无事件")
    print("last_event:", st.get("last_event"))
    print("last_change:", st.get("last_change"))


def main():
    if "--status" in sys.argv:
        status()
        return
    if "--once" in sys.argv:
        print(tick(force_away=True))   # 自测用,强制走离家(安防)分支
        return
    # 常驻循环
    prune_counter = 0
    while True:
        try:
            res = tick()
            if res.startswith(("EVENT", "CHANGE")) or "global_change" in res:
                print(f"[{datetime.now():%H:%M:%S}] {res}", flush=True)
        except Exception as e:
            print(f"[{datetime.now():%H:%M:%S}] loop_error: {e}", flush=True)
        prune_counter += 1
        if prune_counter >= max(1, int(3600 / INTERVAL)):  # 约每小时清一次旧帧
            try:
                prune_old()
            except Exception:
                pass
            prune_counter = 0
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
