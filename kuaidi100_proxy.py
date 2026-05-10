#!/usr/bin/env python3
"""
快递100 价格查询代理
运行在 HA 盒子（国内网络），暴露 HTTP API 给 VPS 调用。

Usage:
  python3 kuaidi100_proxy.py          # 默认端口 5002
  curl "http://localhost:5002/price?from=广东珠海&to=北京&weight=1&man=shunfeng"
  curl "http://localhost:5002/price?from=广东珠海&to=北京&weight=1"  # 不指定快递公司则查全部
"""

from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, urlencode
import urllib.request
import json
import hashlib
import time

KEY = 'FGbegVrw4788'
SECRET = '6454c13d407340a095c8d5a80ae9db06'
API_URL = 'https://api.kuaidi100.com/label/order'


def query_price(send_addr, rec_addr, weight, kuaidicom=''):
    t = str(int(time.time() * 1000))
    param = {
        'sendAddr': send_addr,
        'recAddr': rec_addr,
        'weight': float(weight),
    }
    if kuaidicom:
        param['kuaidicom'] = kuaidicom
    param_str = json.dumps(param, ensure_ascii=False, separators=(',', ':'))
    sign = hashlib.md5((param_str + t + KEY + SECRET).encode('utf-8')).hexdigest().upper()
    post_data = urlencode({
        'method': 'price',
        'key': KEY,
        'sign': sign,
        't': t,
        'param': param_str,
    }).encode('utf-8')
    req = urllib.request.Request(API_URL, data=post_data, method='POST')
    req.add_header('Content-Type', 'application/x-www-form-urlencoded')
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode('utf-8'))


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == '/price':
            params = parse_qs(parsed.query)
            from_addr = params.get('from', [''])[0]
            to_addr = params.get('to', [''])[0]
            weight = params.get('weight', ['1'])[0]
            man = params.get('man', [''])[0]

            if not from_addr or not to_addr:
                self._json(400, {'error': 'Missing params: from, to'})
                return

            try:
                result = query_price(from_addr, to_addr, weight, man)
                self._json(200, result)
            except Exception as e:
                self._json(500, {'error': str(e)})

        elif parsed.path == '/health':
            self._json(200, {'status': 'ok'})

        else:
            self._json(404, {'error': 'Use /price?from=广东珠海&to=北京&weight=1&man=shunfeng'})

    def _json(self, code, data):
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode('utf-8'))

    def log_message(self, format, *args):
        print(f"{args[0]}")


if __name__ == '__main__':
    port = 5002
    server = HTTPServer(('0.0.0.0', port), Handler)
    print(f'快递100 proxy running on port {port}')
    server.serve_forever()
