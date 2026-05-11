# Claude Home Agent

**用微信跟 AI 管家对话，控制智能家居、叫车、点餐、导航、查票……**

基于 Claude Code + Home Assistant，一条微信搞定所有事。

<img width="360" alt="IMG_1570" src="https://github.com/user-attachments/assets/9812913d-5a3f-49ba-8862-ab44320b65dd" />

<img width="360" alt="IMG_2077" src="https://github.com/user-attachments/assets/1e93e39a-f5ec-440a-9b70-97c50a74eeab" />

<img width="360" alt="IMG_2884" src="IMG_2884.PNG" />

<video src="https://github.com/user-attachments/assets/0f30d916-d256-4519-9beb-451c7c89673f" controls width="360"></video>

---

## 功能展示

### 🏠 智能家居控制
> "把客厅空调调到 26 度" / "开客厅灯" / "空调都关了"

- 空调：开关、调温、模式（支持多台同时控制）
- 灯光：客厅氛围灯 / 主灯 / 餐厅灯，分区精确控制
- 音乐：HomePod mini 播放、暂停、调音量
- 电动窗帘：开 / 停 / 关
- 查询状态："现在室温多少" / "空调开着吗"

### 🚗 位置感知导航
> "导航去吃饭" / "附近有什么川菜馆"

- 自动读取 HA 实时 GPS，以**当前位置**为起点规划路线
- 搜索周边餐厅、咖啡店、商场，每个结果附带高德导航链接
- 点一下链接，直接在手机高德 App 里导航

### 📍 地理围栏提醒
> "到盒马提醒我买牛奶" / "路过公司提醒我打卡"

- 每分钟检查 GPS，到达指定地点自动发微信
- 支持一次性或每次经过都提醒

### 🔔 到家 / 离家通知
- 自动检测到家 / 离家，推送中文通知
- 晚上到家自动开客厅灯
- 离家后开门触发安全提醒

### 🚕 叫车
> "打车去万象城"

- 搜索目的地、查询车型报价、直接下单叫滴滴
- 实时查询司机位置

### 🍔 外卖点餐
> 支持麦当劳点餐下单

### ✈️ 出行查询
> "广州到北京明天有什么机票" / "广州南到珠海今天还有高铁"

- 航班：查价格、时刻、经停
- 高铁：实时余票 + 时刻（通过 12306 国内网络）
- 天气："今天会下雨吗" / "明天适合出门吗"

### 📦 快递
> "从珠海寄 1kg 到北京多少钱"

- 对比顺丰、中通、圆通、韵达等多家报价
- 支持顺丰直接下单寄件

### 📹 视频 / 文件下载
> 直接发链接，自动下载发回微信

- 支持 X（Twitter）、YouTube、抖音等平台
- 图片、视频、文件均可发送

### ⏰ 定时提醒
> "明天早上 8 点提醒我开会"

- 写入 crontab，时间到主动发微信
- 一次性提醒触发后自动清除

### 📖 每日英语阅读推送
- 每天定时从 Reddit / BBC / The Guardian 抓取文章
- AI 生成 B2 级英语阅读卡片，自动推送
- 支持随时手动触发"再发一篇"

### 🎨 AI 图片生成
> "帮我画一张……"

- 调用 gpt-image-2 生成图片，直接发回微信
- 支持竖版 / 横版 / 方形，可选画质

### 🧠 持久记忆
- 跨会话记住偏好、设备、对话历史
- 自动积累用户画像（常去地点、饮食偏好等）
- 用户纠正行为后，下次自动照做

### 🔀 模型实时切换
> "切 Opus" / "切 Sonnet"

- 一句话切换 Claude 模型，自动重启生效

---

## 工作原理

```
微信消息 → weixin-acp → Claude Code CLI → Home Assistant / 各类 API → 回复微信
```

核心思路：用 `CLAUDE.md` 把 Claude Code 变成你的私人 AI 管家。不写复杂代码，全靠 prompt engineering。

---

## 快速开始

### 前置条件

- Linux 服务器（1GB 内存 VPS 即可）
- Node.js 20+、Python 3.12+
- Home Assistant 实例
- Claude Code CLI（需要 Anthropic API key）
- 微信 PC 客户端

### 部署

```bash
git clone https://github.com/aaaa-zhen/claude-home-agent.git
cd claude-home-agent

# 配置环境变量
cp .env.example .env

# 初始化记忆系统
cp -r memory-templates/ memory/

# 自定义你的 Agent
cp CLAUDE.md.example CLAUDE.md

# 安装依赖
python3 -m venv venv && source venv/bin/activate
pip install requests python-dotenv
npm install -g weixin-acp

# 启动
./start.sh
```

扫码登录微信后，给自己发条消息试试！

---

## License

MIT
