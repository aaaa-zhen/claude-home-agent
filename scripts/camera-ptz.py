#!/usr/bin/env python3
"""客厅摄像头云台控制(TP-LINK TL-IPC45AW,ONVIF 端口 2020)

坐标系:pan x ∈ [-1,1](左负右正),tilt y ∈ [-1,1](下负上正)。
命名机位存在 memory/camera-presets.json(用绝对坐标,不依赖设备预置位槽)。

用法:
  ./venv/bin/python scripts/camera-ptz.py status                    # 当前位置 + 已存机位
  ./venv/bin/python scripts/camera-ptz.py move --pan 0.1 --tilt 0   # 相对转动
  ./venv/bin/python scripts/camera-ptz.py goto --name 门口          # 转到命名机位
  ./venv/bin/python scripts/camera-ptz.py goto --pan 0.5 --tilt -0.3
  ./venv/bin/python scripts/camera-ptz.py save --name 门口          # 把当前位置存为机位
  ./venv/bin/python scripts/camera-ptz.py delete --name 门口
  ./venv/bin/python scripts/camera-ptz.py look --name 沙发          # 转过去 + 抓一帧,打印图片路径
  ./venv/bin/python scripts/camera-ptz.py snap                      # 原地抓一帧(主流高清)
  ./venv/bin/python scripts/camera-ptz.py patrol                    # 巡一圈所有机位,各抓一帧
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRESETS_FILE = ROOT / "memory" / "camera-presets.json"
SNAP_DIR = ROOT / "tmp" / "camera-snaps"

CAM_HOST = "192.168.1.101"
ONVIF_PORT = 2020
MOVE_SETTLE_SECONDS = 2.5   # 转动后等云台停稳再抓帧


def load_env():
    env = {}
    for line in (ROOT / ".env").read_text().splitlines():
        m = re.match(r"^([A-Z0-9_]+)=(.*)$", line)
        if m:
            env[m.group(1)] = m.group(2).strip()
    return env


def get_ptz():
    # onvif-zeep only clears lower-case http(s)_proxy.  This machine exports
    # upper-case proxy variables, which sent LAN SOAP calls through the proxy
    # and surfaced as a misleading HTTP 502 from the camera.
    for name in list(os.environ):
        if name.lower() in {"http_proxy", "https_proxy", "all_proxy"}:
            os.environ.pop(name, None)
    from onvif import ONVIFCamera
    pwd = load_env()["CAM_LIVINGROOM_PWD"]
    cam = ONVIFCamera(CAM_HOST, ONVIF_PORT, "admin", pwd)
    media = cam.create_media_service()
    profile = media.GetProfiles()[0]
    return cam.create_ptz_service(), profile.token


def get_position(ptz, tok):
    st = ptz.GetStatus({"ProfileToken": tok})
    return round(st.Position.PanTilt.x, 4), round(st.Position.PanTilt.y, 4)


def load_presets():
    if PRESETS_FILE.exists():
        try:
            return json.loads(PRESETS_FILE.read_text())
        except Exception:
            pass
    return {}


def save_presets(d):
    PRESETS_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n")


def absolute_move(ptz, tok, pan, tilt):
    pan = max(-1.0, min(1.0, pan))
    tilt = max(-1.0, min(1.0, tilt))
    ptz.AbsoluteMove({"ProfileToken": tok, "Position": {"PanTilt": {"x": pan, "y": tilt}}})
    # 大幅转动可能要 3-6 秒,轮询位置直到到位/停稳,别抓半路的糊帧
    prev = None
    for _ in range(16):
        time.sleep(0.5)
        cur = get_position(ptz, tok)
        if abs(cur[0] - pan) < 0.02 and abs(cur[1] - tilt) < 0.02:
            break
        if prev is not None and abs(cur[0] - prev[0]) < 0.005 and abs(cur[1] - prev[1]) < 0.005:
            break  # 不再动了(可能到机械限位)
        prev = cur
    time.sleep(0.5)


def relative_move(ptz, tok, dpan, dtilt):
    cur_pan, cur_tilt = get_position(ptz, tok)
    absolute_move(ptz, tok, cur_pan + dpan, cur_tilt + dtilt)


def snap(label="snap"):
    """抓一帧主流高清图,返回绝对路径。"""
    pwd = load_env()["CAM_LIVINGROOM_PWD"]
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    safe_label = re.sub(r"[^\w\-\u4e00-\u9fff]+", "-", label).strip("-") or "snap"
    dest = SNAP_DIR / f"{safe_label}-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}.jpg"
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-rtsp_transport", "tcp",
        "-i", f"rtsp://admin:{pwd}@{CAM_HOST}:554/stream1",
        "-frames:v", "1", "-q:v", "2", str(dest),
    ]
    r = subprocess.run(cmd, capture_output=True, timeout=30)
    if r.returncode != 0 or not dest.exists():
        dest.unlink(missing_ok=True)
        raise RuntimeError("抓帧失败，请检查摄像头网络和 RTSP 服务")
    return dest


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["status", "move", "goto", "save", "delete", "look", "snap", "patrol"])
    ap.add_argument("--name")
    ap.add_argument("--pan", type=float)
    ap.add_argument("--tilt", type=float)
    args = ap.parse_args()

    if args.cmd == "snap":
        print(snap())
        return

    ptz, tok = get_ptz()
    presets = load_presets()

    if args.cmd == "status":
        pan, tilt = get_position(ptz, tok)
        print(f"当前位置: pan={pan} tilt={tilt}")
        if presets:
            print("已存机位:")
            for name, p in presets.items():
                print(f"  {name}: pan={p['pan']} tilt={p['tilt']}")
        else:
            print("尚无命名机位(用 save --name XX 保存)")

    elif args.cmd == "move":
        relative_move(ptz, tok, args.pan or 0.0, args.tilt or 0.0)
        pan, tilt = get_position(ptz, tok)
        print(f"已转动,当前 pan={pan} tilt={tilt}")

    elif args.cmd == "goto":
        if args.name:
            if args.name not in presets:
                sys.exit(f"没有机位「{args.name}」,已有: {', '.join(presets) or '(无)'}")
            p = presets[args.name]
            absolute_move(ptz, tok, p["pan"], p["tilt"])
            print(f"已转到「{args.name}」(pan={p['pan']} tilt={p['tilt']})")
        elif args.pan is not None:
            absolute_move(ptz, tok, args.pan, args.tilt or 0.0)
            print(f"已转到 pan={args.pan} tilt={args.tilt or 0.0}")
        else:
            sys.exit("goto 需要 --name 或 --pan/--tilt")

    elif args.cmd == "save":
        if not args.name:
            sys.exit("save 需要 --name")
        pan, tilt = get_position(ptz, tok)
        presets[args.name] = {"pan": pan, "tilt": tilt}
        save_presets(presets)
        print(f"已保存机位「{args.name}」: pan={pan} tilt={tilt}")

    elif args.cmd == "delete":
        if not args.name or args.name not in presets:
            sys.exit(f"没有机位「{args.name}」")
        del presets[args.name]
        save_presets(presets)
        print(f"已删除机位「{args.name}」")

    elif args.cmd == "look":
        if not args.name or args.name not in presets:
            sys.exit(f"没有机位「{args.name}」,已有: {', '.join(presets) or '(无)'}")
        p = presets[args.name]
        absolute_move(ptz, tok, p["pan"], p["tilt"])
        print(snap(label=args.name))

    elif args.cmd == "patrol":
        if not presets:
            sys.exit("尚无命名机位,先 save 几个")
        for name, p in presets.items():
            absolute_move(ptz, tok, p["pan"], p["tilt"])
            print(f"{name}: {snap(label=name)}")


if __name__ == "__main__":
    main()
