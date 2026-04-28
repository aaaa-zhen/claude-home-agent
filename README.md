# Claude Home Agent

用微信控制智能家居、和 AI 管家聊天 —— 基于 Claude Code + Home Assistant。

<!-- 在这里放一张效果截图 -->
<!-- ![demo](docs/demo.jpg) -->

<img width="1260" height="2736" alt="IMG_1570" src="https://github.com/user-attachments/assets/9812913d-5a3f-49ba-8862-ab44320b65dd" />
<img width="1260" height="1047" alt="IMG_2077" src="https://github.com/user-attachments/assets/1e93e39a-f5ec-440a-9b70-97c50a74eeab" />
<img alt="IMG_2884" src="IMG_2884.PNG" />

https://github.com/aaaa-zhen/claude-home-agent/raw/main/IMG_2069.mp4










## 它能做什么

- **微信控制智能家居**："把客厅空调调到 26 度"、"关灯"
- **查询设备状态**："现在室温多少"、"空调开着吗"
- **位置感知**：结合 HA 手机定位 + 高德 API，到家/离家自动提醒
- **持久记忆**：跨会话记住你的偏好、设备、对话历史
- **通用 AI 助手**：不只是智能家居，任何问题都能聊
- **语音模式**（可选）：搭配 ESP32 语音助手使用

## 工作原理

```
微信消息 → weixin-acp (Node 桥) → Claude Code CLI → Home Assistant API → 回复微信
```

核心思路：**用 `CLAUDE.md` 把 Claude Code 变成你的智能家居 Agent**。不写代码，全靠 prompt engineering。

Claude Code 本身就能读写文件、执行命令、调 API，我们只需要在 `CLAUDE.md` 里告诉它：
- 你是谁（智能家居管家）
- 怎么控制设备（HA REST API）
- 怎么记住事情（memory/ 目录）
- 行为规则（速度优先、简洁回复）

## 快速开始

### 前置条件

- 一台 Linux 服务器（VPS 或家里的机器都行，1GB 内存就够）
- Node.js 20+、Python 3.12+
- Home Assistant 实例（能通过网络访问）
- Claude Code CLI（需要 Anthropic API key）
- 微信 PC 客户端（weixin-acp 需要）

### 1. 克隆项目

```bash
git clone https://github.com/aaaa-zhen/claude-home-agent.git
cd claude-home-agent
```

### 2. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env，填入你的 HA 地址、token、高德 key 等
```

### 3. 初始化记忆系统

```bash
cp -r memory-templates/ memory/
```

### 4. 自定义 CLAUDE.md

```bash
cp CLAUDE.md.example CLAUDE.md
# 根据你的设备和偏好编辑 CLAUDE.md
```

重点要改的：
- 运行环境信息
- 在 `memory/devices.md` 填入你的设备 entity_id

### 5. 安装依赖

```bash
# Python 依赖（用于 monitor.py）
python3 -m venv venv
source venv/bin/activate
pip install requests python-dotenv

# weixin-acp
npm install -g weixin-acp
```

### 6. 启动

```bash
# 主服务（会弹出微信二维码，扫码登录）
./start.sh

# 后台监控（可选）
./start-monitor.sh

# 会话管理（可选）
./start-session-manager.sh
```

扫码登录微信后，给自己发条消息试试！

## 项目结构

```
claude-home-agent/
├── CLAUDE.md              # Agent 配置（你的核心，需要自定义）
├── CLAUDE.md.example      # CLAUDE.md 模板
├── .env.example           # 环境变量模板
├── start.sh               # 主启动脚本（守护进程，自动重启）
├── monitor.py             # 后台监控（温度/位置/到家离家检测）
├── session-manager.py     # 会话管理（定时重启 session）
├── gps_convert.py         # WGS-84 → GCJ-02 坐标转换
├── patch-send-file.sh     # weixin-acp 补丁（支持发送文件/图片）
├── architecture.md        # 架构详细说明
├── memory-templates/      # 记忆系统模板文件
│   ├── index.md
│   ├── user-profile.md
│   ├── devices.md
│   ├── learned-facts.md
│   └── ...
└── deploy/                # 部署相关
    ├── setup.sh           # 一键部署脚本
    └── *.service           # systemd 服务文件
```

## 记忆系统

这是本项目最有价值的设计之一。Claude Code 每次重启会丢失上下文，记忆系统解决了这个问题。

```
memory/
├── recent-context.md       # 最近 10 轮对话（短期记忆）
├── pending-followups.md    # 待跟进事项
├── user-profile.md         # 用户画像（自动积累）
├── devices.md              # 设备清单
├── learned-facts.md        # 经验知识库（带 #标签 检索）
├── conversation-summary.md # 重要对话摘要（长期记忆）
└── location-log.md         # 位置轨迹
```

工作方式：
- **新对话开始** → 读 recent-context + pending-followups，恢复上下文
- **每轮对话结束** → 追加摘要到 recent-context
- **学到新信息** → 立即写入对应文件
- **操作顺序** → 执行操作 → 写 memory → 回复用户（写入优先于回复！）

详见 `CLAUDE.md.example` 中的记忆系统章节。

## 后台监控 (monitor.py)

独立于 Claude 运行的 Python 脚本，负责：

- **到家/离家检测**：轮询 HA 的 person 实体
- **温度监控**：室温异常时推送通知
- **位置记录**：自动记录到 `memory/location-log.md`
- **Tunnel 看门狗**：检查网络连通性

> 定时任务不要交给 Claude！用独立脚本处理。

## 关键经验

1. **CLAUDE.md 是一切的核心** —— 写得越清晰，Agent 越好用
2. **记忆系统很重要** —— 没有它每次重启都是失忆
3. **定时任务不要交给 Claude** —— 用独立脚本，Claude 只负责对话
4. **行为规则要持续迭代** —— 用户纠正的行为记到 learned-facts.md
5. **速度优先** —— 减少 tool call，控制设备时直接执行不废话
6. **写入优先于回复** —— session 随时可能被重置，先存记忆再说话

## 自定义扩展

本项目是一个框架/参考实现，你可以根据需要扩展：

- **添加新能力**：在 CLAUDE.md 里告诉 Claude 怎么调用新 API
- **增加记忆类型**：在 memory/ 里加新文件，在 CLAUDE.md 里定义何时读写
- **接入其他服务**：天气 API、日历、外卖平台……Claude Code 能 curl 的都能接

## 常见问题

**Q: 需要什么样的服务器？**
A: 1GB 内存的 VPS 就够。Claude Code CLI 本身不吃内存，计算在 Anthropic 云端。

**Q: weixin-acp 是什么？**
A: 一个把微信消息桥接到 CLI 工具的 Node 包。它 hook 微信 PC 客户端，把收到的消息转给 Claude Code，再把回复发回微信。

**Q: 要花多少 API 费用？**
A: 取决于使用频率。日常使用（每天几十条消息）大约几美元/月。可以用 Sonnet 省钱，需要复杂推理时切 Opus。

**Q: 能控制哪些设备？**
A: 任何接入 Home Assistant 的设备都能控制。只要 HA 里有 entity_id，Claude 就能通过 REST API 操作它。

## License

MIT
