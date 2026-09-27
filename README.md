# Claude Home Agent

**Talk to your AI butler over WeChat — control smart home, hail rides, order food, navigate, check tickets, and more.**

Built on Claude Code + Home Assistant. One chat handles everything.

<p align="center">
  <img width="240" alt="demo 1" src="https://github.com/user-attachments/assets/9812913d-5a3f-49ba-8862-ab44320b65dd" />
  <img width="240" alt="demo 2" src="https://github.com/user-attachments/assets/1e93e39a-f5ec-440a-9b70-97c50a74eeab" />
  <img width="240" alt="demo 3" src="IMG_2884.PNG" />
</p>

https://github.com/user-attachments/assets/0f30d916-d256-4519-9beb-451c7c89673f

> 📱 Want a generative-UI iOS frontend (Siri-style cards) for the same agent?
> See **[siri-agent-app](https://github.com/aaaa-zhen/siri-agent-app)**.

---

## Highlights

The core idea: a single `CLAUDE.md` turns Claude Code into your personal butler.
No heavy code — it's mostly prompt engineering. The agent doesn't just reply, it
**acts on its own** and **remembers across sessions**.

| Area | What it does |
|---|---|
| 🤖 **Proactive automation** | Watches room temp while you sleep & auto-runs AC; detects arrive/leave home; reminds you of devices left on after you leave; 24/7 background process |
| 🏠 **Smart home** | AC (on/off/temp/mode, multi-unit), lights (per-room), HomePod music, curtains, live status queries |
| 🚗 **Navigation** | Routes from your **live GPS**; searches nearby restaurants/cafes/malls with one-tap Amap deep links; driving / walking / cycling |
| 📍 **Geofence reminders** | "Remind me to buy milk at the supermarket" — checks GPS every minute, pings WeChat on arrival |
| 🚕 **Ride hailing** | Search destination, quote, book a DiDi, track the driver |
| ✈️ **Travel** | Flights (price/time), high-speed rail (live seats via 12306), weather |
| 📦 **Shipping** | Compare courier quotes (SF/ZTO/YTO/Yunda), book SF pickup |
| 📹 **Media & files** | Send a link → auto-download (X / YouTube / Douyin); built-in FFmpeg for clip/convert/compress |
| ⏰ **Reminders** | "Remind me at 8am tomorrow" — writes to crontab, self-clears after firing |
| 📖 **Daily English reading** | Auto-fetches articles (Reddit/BBC/Guardian), AI-generates B2 reading cards |
| 🎨 **Image generation** | "Draw me a…" via gpt-image, sent back to WeChat |
| 🧠 **Persistent memory** | Remembers preferences, devices, history; builds a user profile; learns from corrections |
| 📷 **Image recognition** | Send a photo (receipt/menu/doc/product) — native multimodal understanding |
| 🌐 **Browser use** | Drives a dedicated logged-in Chrome profile: read pages, screenshot, fill forms, post messages. Page content is always handled as untrusted data |
| 🔐 **Password vault** | Credentials sent over WeChat are intercepted *before* the model sees them and stored in the macOS Keychain; the agent injects them into a login form or a subprocess env without ever reading the plaintext |
| 🔀 **Model switching** | "Switch to Opus / Sonnet" in one message |

## How it works

```
WeChat message → weixin-acp → Claude Code CLI → Home Assistant / various APIs → reply to WeChat
```

## Two pieces worth a closer look

**Password vault** — `scripts/secret-vault.mjs`, design notes in
[`docs/secret-vault.md`](docs/secret-vault.md). People type passwords into chat whether
you want them to or not, and telling the agent "never record passwords" does not stop the
plaintext from already being on disk. So the vault intercepts the message at the bridge:
the secret goes into the macOS Keychain and the model only ever sees
`[stored in vault: <name>]`. Injection happens in a child process (value via stdin, never
argv), every use is written to an audit log that records the name and purpose but not the
value, and background jobs are refused — only a turn that is answering the user can unlock
anything.

**Browser use** — `scripts/browser-bridge.mjs`, notes in
[`docs/browser-bridge.md`](docs/browser-bridge.md). A dedicated Chrome profile keeps your
logins, so the agent can act on sites that have no API. You log in yourself in a visible
window; the agent never asks for the password. Read paths strip query strings and mark the
text `untrusted_content`, because instructions found inside a web page are data, not orders.

## Quick start

**Prerequisites:** a Linux server (1GB VPS is fine), Node.js 20+, Python 3.12+,
a Home Assistant instance, Claude Code CLI (Anthropic API key), and the WeChat PC client.

```bash
git clone https://github.com/aaaa-zhen/claude-home-agent.git
cd claude-home-agent

cp .env.example .env          # fill your keys
cp -r memory-templates/ memory/   # init memory
cp CLAUDE.md.example CLAUDE.md     # customize your agent

python3 -m venv venv && source venv/bin/activate
pip install requests python-dotenv
npm install -g weixin-acp

./start.sh                    # scan the QR to log in WeChat, then message yourself
```

## Roadmap

- **Cars** — Lynk&Co / BYD / Tesla via HA: check battery & location, pre-cool/heat, push nav to the car
- **More platforms (MCP/CLI)** — Meituan/Ele.me food, JD/Taobao shopping, Apple Health, calendar sync, bills
- **Smart home** — Zigbee/Matter devices, smart locks, security cams with motion push, energy monitoring
- **Multi-user** — per-member WeChat binding, role-based access, elder/kid modes
- **Voice** — ESP32-S3 / Raspberry Pi wake-word entry with local ASR

## License

MIT

---

<details>
<summary>中文说明</summary>

**用微信跟 AI 管家对话，控制智能家居、叫车、点餐、导航、查票……** 基于 Claude Code + Home Assistant，一条微信搞定所有事。

> 想要同一个 agent 的生成式 UI iOS 前端（Siri 风格卡片）？见 **[siri-agent-app](https://github.com/aaaa-zhen/siri-agent-app)**。

核心思路：用一个 `CLAUDE.md` 把 Claude Code 变成你的私人管家，几乎不写代码、全靠 prompt engineering。Agent 不只被动回复，还会**主动出击**、**跨会话记忆**。

| 能力 | 说明 |
|---|---|
| 🤖 **主动自动化** | 睡觉时守护室温自动开空调；到家/离家感知；离家忘关设备提醒；7×24 后台运行 |
| 🏠 **智能家居** | 空调（开关/调温/模式/多台）、灯光（分区）、HomePod 音乐、窗帘、状态查询 |
| 🚗 **位置感知导航** | 以**当前 GPS** 为起点规划；搜周边餐厅/咖啡/商场，带高德一键导航；驾车/步行/骑行 |
| 📍 **地理围栏提醒** | "到超市提醒我买牛奶" —— 每分钟查 GPS，到达发微信 |
| 🚕 **叫车** | 搜目的地、查报价、下单滴滴、实时查司机位置 |
| ✈️ **出行查询** | 航班（价格/时刻）、高铁（12306 实时余票）、天气 |
| 📦 **快递** | 多家比价（顺丰/中通/圆通/韵达），顺丰下单寄件 |
| 📹 **视频/文件** | 发链接自动下载（X/YouTube/抖音）；内置 FFmpeg 剪辑转换压缩 |
| ⏰ **定时提醒** | "明早 8 点提醒我开会" —— 写 crontab，触发后自动清除 |
| 📖 **每日英语阅读** | 自动抓 Reddit/BBC/Guardian，AI 生成 B2 阅读卡片 |
| 🎨 **AI 图片生成** | "帮我画一张……" 直接发回微信 |
| 🧠 **持久记忆** | 跨会话记住偏好/设备/历史，积累用户画像，从纠正中学习 |
| 📷 **图片识别** | 发图（快递单/菜单/文件/商品）原生多模态识别 |
| 🌐 **浏览器操作** | 驱动一个带登录态的专属 Chrome：读网页、截图、填表、发消息；网页正文一律当作不可信数据处理 |
| 🔐 **密码箱** | 微信里发来的账号密码在**进模型之前**就被拦截并存进 macOS 钥匙串；登录时注入网页表单或子进程环境变量，助手全程读不到明文 |
| 🔀 **模型切换** | "切 Opus / 切 Sonnet" 一句话搞定 |

**工作原理**：`微信消息 → weixin-acp → Claude Code CLI → Home Assistant / 各类 API → 回复微信`

**快速开始**：`git clone` 后 `cp .env.example .env`（填密钥）→ `cp -r memory-templates/ memory/` → `cp CLAUDE.md.example CLAUDE.md` → 装依赖 → `./start.sh` 扫码登录微信。

</details>
