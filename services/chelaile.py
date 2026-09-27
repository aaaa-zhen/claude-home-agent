#!/usr/bin/env python3
"""
车来了公交实时查询 API
纯服务端解密，不需要手机
AES-256-ECB, Key: 445AA658EB0912577EE304B5D312C47F
"""

import base64
import json
import gzip
import time
import urllib.parse
import urllib.request
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from http.server import HTTPServer, BaseHTTPRequestHandler

PORT = 8080
AES_KEY = b"445AA658EB0912577EE304B5D312C47F"  # 32 bytes = AES-256
API_BASE = "https://api.chelaile.net.cn"
UA = "okhttp/4.12.0"

COMMON = {
    "s": "android", "v": "7.3.0",
    "udid": "14804847-7700-49dd-8255-ac4d3e266266",
    "paramsMakeUp": "is",
    "sign": "V4/bRPMECA0a41RdknF8yw==\n",
    "use_http_dns": "true", "ep": "1",
}


def decrypt(enc_b64):
    """AES-256-ECB 解密 encryptResult"""
    raw = base64.b64decode(enc_b64)
    c = Cipher(algorithms.AES(AES_KEY), modes.ECB())
    d = c.decryptor()
    pt = d.update(raw) + d.finalize()
    text = pt.decode("utf-8", errors="replace")
    # 去掉 PKCS padding 垃圾字节，截取有效 JSON
    end = text.rfind("}")
    if end > 0:
        return text[: end + 1]
    return text


def api_call(path, extra=None):
    """调用车来了 API 并自动解密"""
    params = dict(COMMON)
    if extra:
        params.update(extra)
    url = f"{API_BASE}{path}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = resp.read()
        try:
            data = gzip.decompress(data)
        except Exception:
            pass
    text = data.decode("utf-8", errors="replace").replace("**YGKJ", "").replace("YGKJ##", "")
    result = json.loads(text)
    enc = result.get("jsonr", {}).get("data", {}).get("encryptResult")
    if enc:
        decrypted = decrypt(enc)
        result["jsonr"]["data"] = json.loads(decrypted)
    return result


def get_realtime(line_id, station_id="", city_id="242", target_order="5"):
    """获取公交实时到站信息"""
    return api_call(
        "/bus/line!encNLineDetail.action",
        {
            "cityId": city_id,
            "lineId": line_id,
            "stationId": station_id,
            "isNewLineDetail": "1",
            "targetOrder": target_order,
            "specialTargetOrder": target_order,
            "timeStamp": str(int(time.time() * 1000)),
            "cryptoSignStr": "",
            "cryptoSign": "",
        },
    )


def get_nearby(line_stns, city_id="242"):
    """获取多条线路的实时信息
    line_stns 格式: "lineId1,stationId1,,order1;lineId2,stationId2,,order2;"
    """
    return api_call(
        "/bus/line!encryptedTsfRealInfos.action",
        {"cityId": city_id, "lineStn": line_stns, "reqSrc": "2"},
    )


def get_nearby_stops(lat, lng, city_id="242"):
    """根据经纬度获取附近公交站及线路"""
    return api_call(
        "/bus/stop!encryptedHomePage.action",
        {
            "cityId": city_id, "type": "5", "act": "1", "home_act": "1",
            "lat": str(lat), "lng": str(lng), "gpstype": "wgs",
        },
    )


def refresh_line(line_id, station_id="", city_id="242", direction="1", target_order="5"):
    """刷新线路实时信息"""
    return api_call(
        "/bus/line!encRefreshLineDetail.action",
        {
            "cityId": city_id,
            "lineId": line_id,
            "stationId": station_id,
            "isNewLineDetail": "1",
            "needAds": "false",
            "filter": "1",
            "direction": direction,
            "targetOrder": target_order,
            "specialTargetOrder": target_order,
            "timeStamp": str(int(time.time() * 1000)),
            "cryptoSignStr": "",
            "cryptoSign": "",
        },
    )


# ============ Web UI ============
INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>车来了 - 实时公交</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#f0f2f5;color:#333;min-height:100vh}
.header{background:linear-gradient(135deg,#1890ff,#096dd9);color:#fff;padding:16px 20px;text-align:center;position:sticky;top:0;z-index:10;box-shadow:0 2px 8px rgba(0,0,0,.15)}
.header h1{font-size:20px;font-weight:600}
.header p{font-size:12px;opacity:.8;margin-top:4px}
.tabs{display:flex;background:#fff;border-bottom:1px solid #e8e8e8}
.tab{flex:1;padding:12px;text-align:center;font-size:14px;cursor:pointer;border-bottom:2px solid transparent;transition:all .2s;color:#666}
.tab.active{color:#1890ff;border-bottom-color:#1890ff;font-weight:600}
.panel{display:none}
.panel.active{display:block}
.container{padding:16px;max-width:600px;margin:0 auto}
.loc-bar{padding:12px 16px;background:#fff;display:flex;align-items:center;gap:10px;box-shadow:0 1px 4px rgba(0,0,0,.06)}
.loc-bar .loc-icon{font-size:20px}
.loc-bar .loc-text{flex:1;font-size:13px;color:#666}
.loc-bar .loc-coord{font-size:11px;color:#999}
.btn{padding:10px 20px;background:#1890ff;color:#fff;border:none;border-radius:8px;font-size:14px;cursor:pointer;transition:background .2s;white-space:nowrap}
.btn:hover{background:#096dd9}
.btn:active{transform:scale(.97)}
.btn.loading{opacity:.6;pointer-events:none}
.btn-sm{padding:8px 14px;font-size:12px;border-radius:6px}
.btn-green{background:#52c41a}.btn-green:hover{background:#389e0d}
.stn-card{background:#fff;border-radius:12px;padding:16px;margin-bottom:12px;box-shadow:0 1px 4px rgba(0,0,0,.06)}
.stn-header{display:flex;align-items:center;gap:8px;margin-bottom:12px;padding-bottom:10px;border-bottom:1px solid #f0f0f0}
.stn-icon{width:36px;height:36px;background:linear-gradient(135deg,#1890ff,#096dd9);border-radius:50%;display:flex;align-items:center;justify-content:center;color:#fff;font-size:16px;flex-shrink:0}
.stn-name{font-size:16px;font-weight:600;flex:1}
.stn-dist{font-size:12px;color:#999;background:#f5f5f5;padding:3px 8px;border-radius:10px}
.line-row{display:flex;align-items:center;gap:10px;padding:10px 0;border-bottom:1px solid #f7f7f7}
.line-row:last-child{border:none}
.line-badge{background:#1890ff;color:#fff;padding:3px 10px;border-radius:14px;font-size:13px;font-weight:700;min-width:44px;text-align:center;flex-shrink:0}
.line-dest{flex:1;font-size:13px;color:#333}
.line-dest small{display:block;color:#999;font-size:11px;margin-top:2px}
.bus-tag{font-size:12px;padding:4px 8px;border-radius:6px;font-weight:600;flex-shrink:0}
.bus-tag.arriving{background:#f6ffed;color:#389e0d;border:1px solid #b7eb8f}
.bus-tag.waiting{background:#fff7e6;color:#d48806;border:1px solid #ffe58f}
.bus-tag.none{background:#f5f5f5;color:#999;border:1px solid #e8e8e8}
.search{padding:16px;background:#fff;box-shadow:0 1px 4px rgba(0,0,0,.08)}
.search-row{display:flex;gap:8px;margin-bottom:8px}
.search input{flex:1;padding:10px 12px;border:1px solid #d9d9d9;border-radius:8px;font-size:14px;outline:none}
.search input:focus{border-color:#1890ff}
.line-card{background:#fff;border-radius:12px;padding:16px;margin-bottom:12px;box-shadow:0 1px 4px rgba(0,0,0,.06)}
.line-header{display:flex;align-items:center;gap:10px;margin-bottom:12px}
.line-badge-lg{background:#1890ff;color:#fff;padding:4px 14px;border-radius:20px;font-size:16px;font-weight:700}
.line-info{flex:1}
.line-name{font-size:13px;color:#666}
.line-time{font-size:11px;color:#999}
.bus-item{display:flex;align-items:center;gap:12px;padding:10px 14px;background:#f6ffed;border:1px solid #b7eb8f;border-radius:8px;margin-bottom:6px}
.bus-icon{font-size:24px}
.bus-detail{flex:1}
.bus-stops{font-size:16px;font-weight:700;color:#389e0d}
.bus-dist{font-size:12px;color:#666}
.bus-plate{font-size:11px;color:#999}
.progress-bar{height:4px;background:#e8e8e8;border-radius:2px;margin-top:6px;overflow:hidden}
.progress-fill{height:100%;background:linear-gradient(90deg,#52c41a,#1890ff);border-radius:2px;transition:width .5s}
.line-status{padding:8px 12px;border-radius:8px;font-size:13px}
.empty{text-align:center;padding:60px 20px;color:#999}
.empty-icon{font-size:48px;margin-bottom:12px}
.auto-refresh{text-align:center;color:#999;font-size:11px;padding:8px}
.auto-tag{display:inline-block;background:#52c41a;color:#fff;font-size:10px;padding:2px 6px;border-radius:4px;animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.5}}
</style>
</head>
<body>
<div class="header">
  <h1>车来了 实时公交</h1>
  <p id="headerSub">珠海公交实时到站查询</p>
</div>

<div class="tabs">
  <div class="tab active" onclick="switchTab(0)">附近站点</div>
  <div class="tab" onclick="switchTab(1)">线路查询</div>
</div>

<!-- 附近站点 -->
<div class="panel active" id="panel0">
  <div class="loc-bar" style="flex-wrap:wrap">
    <span class="loc-icon">📍</span>
    <div style="flex:1;min-width:120px">
      <div class="loc-text" id="locText">输入位置或点击定位</div>
      <div class="loc-coord" id="locCoord"></div>
    </div>
    <button class="btn btn-sm btn-green" id="locBtn" onclick="getLocation()">自动定位</button>
  </div>
  <div style="padding:8px 16px;background:#fff;display:flex;gap:8px;border-top:1px solid #f0f0f0">
    <input id="manualLat" placeholder="纬度 如 22.378" style="flex:1;padding:8px;border:1px solid #d9d9d9;border-radius:6px;font-size:13px">
    <input id="manualLng" placeholder="经度 如 113.575" style="flex:1;padding:8px;border:1px solid #d9d9d9;border-radius:6px;font-size:13px">
    <button class="btn btn-sm" onclick="manualSearch()">查找</button>
  </div>
  <div class="container" id="nearbyResult">
    <div class="empty"><div class="empty-icon">📍</div>输入经纬度或点击「自动定位」</div>
  </div>
  <div class="auto-refresh" id="nearbyRefresh" style="display:none">
    <span class="auto-tag">自动刷新</span> <span id="nearbyTime"></span>
  </div>
</div>

<!-- 线路查询 -->
<div class="panel" id="panel1">
  <div class="search">
    <div class="search-row">
      <input id="lineId" placeholder="线路ID (如 756132487340)">
      <input id="stationId" placeholder="站点ID (如 0756-1830)">
    </div>
    <div class="search-row">
      <input id="cityId" value="242" placeholder="城市ID" style="max-width:80px">
      <input id="targetOrder" value="5" placeholder="站序" style="max-width:60px">
      <button class="btn" id="searchBtn" onclick="doLineSearch()">查询</button>
    </div>
  </div>
  <div class="container" id="lineResult">
    <div class="empty"><div class="empty-icon">🚌</div>输入线路信息查询</div>
  </div>
</div>

<script>
let nearbyTimer = null, lineTimer = null;
let curLat = null, curLng = null;

function switchTab(idx) {
  document.querySelectorAll('.tab').forEach((t,i) => t.classList.toggle('active', i===idx));
  document.querySelectorAll('.panel').forEach((p,i) => p.classList.toggle('active', i===idx));
}

// ===== 附近站点 =====
function manualSearch() {
  const lat = document.getElementById('manualLat').value.trim();
  const lng = document.getElementById('manualLng').value.trim();
  if (!lat || !lng) return alert('请输入经纬度');
  curLat = parseFloat(lat);
  curLng = parseFloat(lng);
  document.getElementById('locText').textContent = '手动定位';
  document.getElementById('locCoord').textContent = `${curLat}, ${curLng}`;
  fetchNearby();
  if (nearbyTimer) clearInterval(nearbyTimer);
  nearbyTimer = setInterval(fetchNearby, 20000);
}

function getLocation() {
  const btn = document.getElementById('locBtn');
  btn.textContent = '定位中';
  btn.classList.add('loading');
  document.getElementById('locText').textContent = '正在获取位置...';

  if (!navigator.geolocation) {
    document.getElementById('locText').textContent = '浏览器不支持定位';
    btn.textContent = '定位'; btn.classList.remove('loading');
    return;
  }

  navigator.geolocation.getCurrentPosition(
    pos => {
      curLat = pos.coords.latitude;
      curLng = pos.coords.longitude;
      document.getElementById('locText').textContent = '定位成功';
      document.getElementById('locCoord').textContent = `${curLat.toFixed(5)}, ${curLng.toFixed(5)}`;
      btn.textContent = '刷新'; btn.classList.remove('loading');
      fetchNearby();
      if (nearbyTimer) clearInterval(nearbyTimer);
      nearbyTimer = setInterval(fetchNearby, 20000);
    },
    err => {
      document.getElementById('locText').textContent = '定位失败: ' + err.message;
      btn.textContent = '重试'; btn.classList.remove('loading');
    },
    { enableHighAccuracy: true, timeout: 10000 }
  );
}

async function fetchNearby() {
  if (!curLat) return;
  try {
    const resp = await fetch(`/bus/stops?lat=${curLat}&lng=${curLng}&cityId=242`);
    const data = await resp.json();
    renderNearby(data);
    document.getElementById('nearbyRefresh').style.display = 'block';
    document.getElementById('nearbyTime').textContent = new Date().toLocaleTimeString('zh-CN');
  } catch(e) {
    document.getElementById('nearbyResult').innerHTML = `<div class="empty"><div class="empty-icon">❌</div>${e.message}</div>`;
  }
}

function renderNearby(data) {
  const stops = data?.jsonr?.data?.nearSts || [];
  if (!stops.length) {
    document.getElementById('nearbyResult').innerHTML = '<div class="empty"><div class="empty-icon">🔍</div>附近没有找到公交站</div>';
    return;
  }
  let html = '';
  for (const st of stops.slice(0, 5)) {
    html += `<div class="stn-card">`;
    html += `<div class="stn-header">`;
    html += `<div class="stn-icon">🚏</div>`;
    html += `<div class="stn-name">${st.sn || '未知站点'}</div>`;
    html += `<div class="stn-dist">${st.distance}m</div>`;
    html += `</div>`;

    for (const line of (st.lines || [])) {
      const info = line.line || {};
      const buses = line.buses || [];
      const depDesc = line.depDesc || info.desc || '';

      let tagHtml = '';
      if (buses.length) {
        const b = buses[0];
        const v = b.value || 0;
        if (v <= 1) tagHtml = `<span class="bus-tag arriving">即将到站</span>`;
        else if (v <= 3) tagHtml = `<span class="bus-tag arriving">${v}站 ${(b.distanceToTgt/1000).toFixed(1)}km</span>`;
        else tagHtml = `<span class="bus-tag waiting">${v}站 ${(b.distanceToTgt/1000).toFixed(1)}km</span>`;
      } else if (depDesc) {
        const cls = depDesc.includes('分钟') ? 'waiting' : 'none';
        tagHtml = `<span class="bus-tag ${cls}">${depDesc}</span>`;
      } else {
        tagHtml = `<span class="bus-tag none">暂无</span>`;
      }

      html += `<div class="line-row" onclick="jumpToLine('${info.lineId}','${st.sId}','${line.targetOrder||0}')">`;
      html += `<div class="line-badge">${info.name || '?'}</div>`;
      html += `<div class="line-dest">${info.destinationName || ''}<small>${info.firstTime||''}-${info.lastTime||''}</small></div>`;
      html += tagHtml;
      html += `</div>`;
    }
    html += `</div>`;
  }
  document.getElementById('nearbyResult').innerHTML = html;
}

function jumpToLine(lineId, stationId, order) {
  switchTab(1);
  document.getElementById('lineId').value = lineId;
  document.getElementById('stationId').value = stationId;
  document.getElementById('targetOrder').value = order;
  doLineSearch();
}

// ===== 线路查询 =====
async function doLineSearch() {
  const lid = document.getElementById('lineId').value.trim();
  const sid = document.getElementById('stationId').value.trim();
  const cid = document.getElementById('cityId').value.trim() || '242';
  const order = document.getElementById('targetOrder').value.trim() || '5';
  if (!lid) return alert('请输入线路ID');

  const btn = document.getElementById('searchBtn');
  btn.classList.add('loading'); btn.textContent = '查询中...';

  try {
    const stns = `${lid},${sid},,${order};`;
    const url = `/bus/nearby?lineStns=${encodeURIComponent(stns)}&cityId=${cid}`;
    const resp = await fetch(url);
    const data = await resp.json();
    renderLines(data);
    if (lineTimer) clearInterval(lineTimer);
    lineTimer = setInterval(async () => {
      const r = await (await fetch(url)).json();
      renderLines(r);
    }, 15000);
  } catch(e) {
    document.getElementById('lineResult').innerHTML = `<div class="empty"><div class="empty-icon">❌</div>${e.message}</div>`;
  } finally {
    btn.classList.remove('loading'); btn.textContent = '查询';
  }
}

function renderLines(data) {
  const lines = data?.jsonr?.data?.lines || [];
  if (!lines.length) {
    document.getElementById('lineResult').innerHTML = '<div class="empty"><div class="empty-icon">🔍</div>未找到</div>';
    return;
  }
  let html = '';
  for (const line of lines) {
    const info = line.line || {};
    const buses = line.buses || [];
    const totalStops = info.stationsNum || 1;
    const tgtOrder = line.targetOrder || 0;

    html += `<div class="line-card"><div class="line-header">`;
    html += `<div class="line-badge-lg">${info.name||'?'}</div>`;
    html += `<div class="line-info"><div class="line-name">${info.startSn||''} → ${info.destinationName||''}</div>`;
    html += `<div class="line-time">${info.firstTime||''}-${info.lastTime||''} · ${totalStops}站 · ${info.price||''}</div></div></div>`;

    if (buses.length) {
      for (const b of buses) {
        const v = b.value||0, dist = b.distanceToTgt||0, spd = b.speed||0;
        const pct = Math.max(5, Math.min(95, ((tgtOrder-v)/totalStops)*100));
        const label = v<=1 ? '即将到站' : `还有${v}站`;
        html += `<div class="bus-item"><div class="bus-icon">🚍</div><div class="bus-detail">`;
        html += `<div class="bus-stops">${label} · ${(dist/1000).toFixed(1)}km</div>`;
        html += `<div class="bus-dist">在第${b.order}站 · ${spd>0?spd+'km/h':'停靠中'}</div>`;
        html += `<div class="bus-plate">${b.busId||b.licence||''}</div>`;
        html += `<div class="progress-bar"><div class="progress-fill" style="width:${pct}%"></div></div>`;
        html += `</div></div>`;
      }
    } else {
      const desc = line.depDesc||info.desc||'暂无实时信息';
      const cls = desc.includes('分钟')||desc.includes('发车')?'waiting':'none';
      html += `<div class="line-status bus-tag ${cls}">${desc}</div>`;
    }
    html += `</div>`;
  }
  document.getElementById('lineResult').innerHTML = html;
}

// 不自动定位（HTTP 下会失败），等用户手动操作
</script>
</body>
</html>"""


# ============ HTTP API ============
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = dict(urllib.parse.parse_qsl(parsed.query))

        try:
            if path == "/":
                self._html(INDEX_HTML)

            elif path == "/api/status":
                self._ok({"status": "ok", "service": "chelaile-bus-api"})

            elif path == "/bus/realtime":
                line_id = qs.get("lineId", "")
                if not line_id:
                    self._ok({"error": "lineId required"}, 400)
                    return
                r = get_realtime(
                    line_id,
                    qs.get("stationId", ""),
                    qs.get("cityId", "242"),
                    qs.get("targetOrder", "5"),
                )
                self._ok(r)

            elif path == "/bus/stops":
                lat = qs.get("lat", "")
                lng = qs.get("lng", "")
                if not lat or not lng:
                    self._ok({"error": "lat and lng required"}, 400)
                    return
                r = get_nearby_stops(lat, lng, qs.get("cityId", "242"))
                self._ok(r)

            elif path == "/bus/nearby":
                line_stns = qs.get("lineStns", "")
                if not line_stns:
                    self._ok({"error": "lineStns required"}, 400)
                    return
                r = get_nearby(line_stns, qs.get("cityId", "242"))
                self._ok(r)

            elif path == "/bus/refresh":
                line_id = qs.get("lineId", "")
                if not line_id:
                    self._ok({"error": "lineId required"}, 400)
                    return
                r = refresh_line(
                    line_id,
                    qs.get("stationId", ""),
                    qs.get("cityId", "242"),
                    qs.get("direction", "1"),
                    qs.get("targetOrder", "5"),
                )
                self._ok(r)

            else:
                self._ok(
                    {
                        "endpoints": {
                            "/bus/realtime": "?lineId=&cityId=&stationId=&targetOrder=",
                            "/bus/nearby": "?lineStns=lineId,stationId,,order;&cityId=",
                            "/bus/refresh": "?lineId=&cityId=&stationId=&direction=&targetOrder=",
                        }
                    },
                    404,
                )
        except Exception as e:
            self._ok({"error": str(e)}, 500)

    def _html(self, html, code=200):
        body = html.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _ok(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print(f"[{time.strftime('%H:%M:%S')}] {args[0]}")


if __name__ == "__main__":
    print(f"车来了公交 API 服务 - 端口 {PORT}")
    print(f"  GET /bus/realtime?lineId=756132487340&cityId=242")
    print(f"  GET /bus/nearby?lineStns=756132487340,0756-1830,,5;&cityId=242")
    print(f"  GET /bus/refresh?lineId=756132487340&cityId=242")
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
