# weixin-agent 架构说明

## 一句话概括

微信消息 → weixin-acp（Node 桥） → Claude Code CLI → 执行工具/API → 回复微信

## 整体架构

```
┌─────────────┐     ┌──────────────┐     ┌──────────────────┐
│  微信客户端   │────▶│  weixin-acp   │────▶│  Claude Code CLI  │
│  (手机/PC)   │◀────│  (Node 桥)    │◀────│  (AI Agent)       │
└─────────────┘     └──────────────┘     └────────┬─────────┘
                                                   │
                                    ┌──────────────┼──────────────┐
                                    ▼              ▼              ▼
                              ┌──────────┐  ┌──────────┐  ┌──────────┐
                              │ Home      │  │ 高德地图  │  │ 记忆系统 │
                              │ Assistant │  │ API      │  │ memory/  │
                              │ REST API  │  │          │  │          │
                              └──────────┘  └──────────┘  └──────────┘
```


## 自动化分工

- **HA automation**：只做本地设备动作，不发日常通知；例如 18:00-07:00 到家开客厅氛围灯。
- **monitor.py**：负责后台状态观察和中文通知；包括到家/离家、温度、空调离线、Tunnel、离家开门安全提醒。
- **ubuntu cron + weixin-send.mjs**：负责具体时间提醒和主动微信消息。
- **geofence_reminder.py + ubuntu cron**：负责用户显式创建的地点触发提醒；`geofence_reminder.py add/enable` 确保 cron 每分钟执行 `geofence_reminder.py check --quiet`，例如到公司提醒打卡、到盒马提醒买东西；`status` 暴露 GPS 更新时间/精度/来源，`self-test` 验证触发链路。
- **Claude agent**：负责对话、临时指令、复杂查询、配置修正和排障。

## 核心组件

### 1. weixin-acp（微信桥）

- **作用**：把微信消息转发给 Claude Code，把 Claude 的回复发回微信
- **安装**：`npm install -g weixin-acp`
- **启动**：`npx weixin-acp claude-code`
- **原理**：Hook 微信 PC 客户端，监听收到的消息，转成 stdin 喂给 Claude Code CLI，再把 stdout 回复发回微信

### 2. Claude Code CLI（AI 大脑）

- **作用**：接收用户消息，理解意图，调用工具执行，返回结果
- **能力**：读写文件、执行命令、调 API、搜索网页——什么都能干
- **配置**：通过 `CLAUDE.md` 文件定义人设、行为规则、工具用法
- **模型**：支持 sonnet/opus 动态切换（读 `model.txt`）

### 3. CLAUDE.md（Agent 的灵魂）

这是最核心的文件，定义了 Agent 的一切行为：

```
CLAUDE.md
├── 你是谁（人设定义）
├── Home Assistant API（怎么控制智能家居）
├── MCP/脚本工具（HA、滴滴、高德、天气、航班、定时提醒）
├── GPS 坐标转换（高德 API 配合）
├── 发送媒体文件（微信发图/视频）
├── 记忆系统（跨会话记忆机制）
│   ├── 会话启动流程
│   ├── 记忆检索规则
│   ├── 记忆写入规则
│   └── 短期上下文管理
└── 行为准则（速度优先、禁止事项等）
```

### 4. 记忆系统（memory/）

Claude Code 每次重启会丢失上下文，记忆系统解决这个问题：

```
memory/
├── recent-context.md      # 最近 10 轮对话摘要（短期记忆）
├── pending-followups.md   # 待跟进事项
├── user-profile.md        # 用户画像
├── devices.md             # 智能设备清单
├── learned-facts.md       # 积累的知识和规则
├── conversation-summary.md # 重要对话摘要（长期记忆）
├── location-log.md        # 位置记录
├── zhuhai-guide.md        # 本地生活指南
└── session-state.json     # 会话状态
```

**工作原理**：
- 每次新对话开始 → 读 `recent-context.md` + `pending-followups.md` 恢复上下文
- 每轮对话结束 → 把关键信息追加到 `recent-context.md`
- 学到新知识 → 写入 `learned-facts.md`
- 用户纠正行为 → 记录到 `learned-facts.md` 的行为规则

### 5. monitor.py（后台监控）

独立运行的 Python 脚本，不依赖 Claude：

- **存在检测**：轮询 HA 的 `person.your_name`，检测到家/离家
- **温度监控**：室温 ≥ 32°C 时推送通知
- **位置记录**：每 30 分钟记录位置到 `location-log.md`
- **Tunnel 看门狗**：每 5 分钟检查 Cloudflare Tunnel，连续异常时推送告警

### 6. session-manager.py（会话生命周期）

管理 Claude Code 的 session 生命周期：

- **凌晨 4 点后重置**：每天最多一次，且只在真正空闲时重启
- **空闲 2 小时重置**：最近 30 分钟有消息时绝不重置，空闲 2 小时后才重启
- **杀进程后 systemd 自动重启主服务**

## 文件结构

```
weixin-agent/
├── CLAUDE.md              # Agent 配置（人设+规则+工具用法）
├── .env                   # 密钥（HA_TOKEN, AMAP_KEY 等）
├── model.txt              # 当前使用的模型（sonnet/opus）
├── start.sh               # 主启动脚本（由 systemd 托管）
├── monitor.py             # 后台监控脚本
├── session-manager.py     # 会话生命周期管理
├── gps_convert.py         # WGS-84 → GCJ-02 坐标转换
├── deploy/                # systemd service 文件
├── start-monitor.sh       # monitor 历史启动脚本，当前由 systemd 托管
├── start-session-manager.sh  # session-manager 历史启动脚本，当前由 systemd 托管
└── memory/                # 记忆系统（见上）
```

## 启动方式

开机启动 3 个 systemd 服务：

```bash
systemctl status weixin-agent weixin-monitor weixin-session-manager
sudo systemctl restart weixin-agent
journalctl -u weixin-agent -n 80 --no-pager
```

## 给同事的快速上手指南

### 最小可用版本（只需 3 步）

1. **安装 weixin-acp**：
   ```bash
   npm install -g weixin-acp
   ```

2. **写 CLAUDE.md**：定义 Agent 的人设和能力（参考本项目的 CLAUDE.md）

3. **启动**：
   ```bash
   npx weixin-acp claude-code
   ```

扫码登录微信即可。收到微信消息 → Claude 处理 → 自动回复。

### 进阶功能

| 需求 | 方案 |
|------|------|
| 跨会话记忆 | 在 CLAUDE.md 里定义记忆系统规则 + memory/ 目录 |
| 智能家居控制 | Home Assistant + REST API |
| 定时监控 | 独立 Python 脚本或 ubuntu crontab；用户要求提醒时必须创建 cron |
| 会话管理 | session-manager.py 定时重启 |
| 位置服务 | HA 手机 app 定位 + 高德 API |
| 发文件/图片 | 回复中加 `[send_file:路径]` 标记 |

> 注：飞书 CLI、Windows `.bat` 启动方式和旧语音/安卓 relay 都是历史内容，不属于当前主链路。

### 关键经验

1. **CLAUDE.md 是一切的核心**——写得越清晰，Agent 越好用
2. **记忆系统很重要**——没有它每次重启都是失忆
3. **提醒必须落到 cron**——Claude 可以生成脚本和安装 crontab，但不能只写记忆或只口头承诺
4. **行为规则要持续迭代**——用户纠正的行为记到 learned-facts.md
5. **Windows 上 curl 发中文会乱码**——用 Python urllib 代替
