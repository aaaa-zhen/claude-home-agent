#!/usr/bin/env python3
"""Fetch latest Stuff You Should Know episode and send via WeChat."""
import urllib.request
import xml.etree.ElementTree as ET
import subprocess
import re
import sys

RSS_URL = "https://feeds.simplecast.com/dHoohVNH"

try:
    req = urllib.request.Request(RSS_URL, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=15) as resp:
        xml_data = resp.read()
except Exception as e:
    print(f"Failed to fetch RSS: {e}", file=sys.stderr)
    sys.exit(1)

root = ET.fromstring(xml_data)
channel = root.find('channel')
item = channel.find('item')

ns = {'itunes': 'http://www.itunes.com/dtds/podcast-1.0.dtd'}

title_el = item.find('title')
title = title_el.text if title_el is not None else 'Latest Episode'
# Strip CDATA if present
title = re.sub(r'<!\[CDATA\[(.*?)\]\]>', r'\1', title, flags=re.DOTALL).strip()

link_el = item.find('link')
link = (link_el.text or '').strip() if link_el is not None else ''

# Try enclosure (direct audio URL) as fallback
if not link:
    enclosure = item.find('enclosure')
    if enclosure is not None:
        link = enclosure.get('url', '')

# Summary: prefer itunes:summary, fall back to description
summary_el = item.find('itunes:summary', ns)
if summary_el is None:
    summary_el = item.find('description')

summary = ''
if summary_el is not None and summary_el.text:
    raw = re.sub(r'<!\[CDATA\[(.*?)\]\]>', r'\1', summary_el.text, flags=re.DOTALL)
    summary = re.sub(r'<[^>]+>', '', raw).strip()
    if len(summary) > 150:
        summary = summary[:150].rsplit(' ', 1)[0] + '...'

# Duration
dur_el = item.find('itunes:duration', ns)
duration = f"  {dur_el.text}" if dur_el is not None and dur_el.text else ''

msg = (
    f"[英语播客] Conan O'Brien Needs A Friend\n\n"
    f"{title}{duration}\n\n"
    f"{summary}\n\n"
    f"{link}"
)

result = subprocess.run(
    ['/usr/bin/node', '/home/ubuntu/weixin-agent/weixin-send.mjs', '--text', msg],
    capture_output=True, text=True
)
if result.returncode != 0:
    print(f"Send failed: {result.stderr}", file=sys.stderr)
    sys.exit(1)

print("Sent successfully.")

# Also push to Telegram podcast channel
try:
    subprocess.Popen(
        ["/bin/bash", "/home/ubuntu/weixin-agent/tg-notify.sh", "podcast", msg],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
except Exception:
    pass
