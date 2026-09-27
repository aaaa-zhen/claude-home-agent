# Apple Bridge

Apple Bridge 是 Home Agent 在这台 Mac 上访问 Apple 生态的本地工具层。它使用稳定的
app bundle identifier 和结构化 JSON 输出，后续既能被主 Agent 直接调用，也能被 MCP
server 包装，而不需要把整段 Apple 数据塞进主对话上下文。

## 支持范围

- Calendar：列出日历、查询时间段内的日程、创建日程。
- Reminders：列出清单、查询提醒、创建提醒、标记完成。
- Notes：列出文件夹、在指定文件夹搜索、创建备忘录、追加内容。
- 初始化：在 iCloud 中创建独立的 `Home Agent` 日历、提醒清单和备忘录文件夹。

Calendar 和 Reminders 使用 EventKit；Notes 因为没有等价的公开数据框架，使用 Notes
的 Apple automation dictionary。Apple 账号和 iCloud 同步仍由 macOS/iOS 原生系统负责。

## 安全边界

- 所有写操作默认只返回预览，必须显式增加 `--apply` 才会真正写入。
- 不提供删除日程、提醒或备忘录的命令。
- 默认只向名为 `Home Agent` 的专用容器写入；读取其他容器必须显式指定。
- 输出只包含调用所需的数据，不在日志中保存完整个人内容。
- 首次访问需要用户在 macOS 系统弹窗中批准 Calendar、Reminders 和 Notes automation。

## 构建和授权

```bash
npm run apple:build
./scripts/apple-bridge.sh doctor
./scripts/apple-bridge.sh permissions request
./scripts/apple-bridge.sh initialize --apply
```

程序会构建到被 Git 忽略的 `runtime/apple-bridge/AppleBridge.app`。固定的 bundle id
`com.zhen.homeagent.applebridge` 让 macOS TCC 权限能稳定地归属于同一个本地工具。
本地构建目前使用 ad-hoc 签名；更新 Swift 源码并重新构建后，macOS 可能要求重新授权，
日常调用和健康检查不会重复构建或改变签名。

## 常用调用

```bash
# 查询未来一周日程
./scripts/apple-bridge.sh calendar events \
  --from 2026-07-11T00:00:00+08:00 \
  --to 2026-07-18T00:00:00+08:00

# 先预览，再由明确用户意图触发写入
./scripts/apple-bridge.sh calendar create \
  --title "牙医" --start 2026-07-12T15:00:00+08:00 \
  --end 2026-07-12T16:00:00+08:00
./scripts/apple-bridge.sh calendar create \
  --title "牙医" --start 2026-07-12T15:00:00+08:00 \
  --end 2026-07-12T16:00:00+08:00 --apply

./scripts/apple-bridge.sh reminders create \
  --title "带充电器" --due 2026-07-12T08:30:00+08:00 --apply

./scripts/apple-bridge.sh notes search --query "护照"
./scripts/apple-bridge.sh notes create \
  --title "旅行清单" --body "护照、充电器" --apply
```

ISO 时间必须带时区，避免 Mac 与手机显示时间不一致。

## 后续 MCP/CLI 扩展

Apple Bridge 本身保持窄接口。未来的 MCP server 只需把每个 JSON 子命令映射为工具，
并在 MCP 层增加 schema、结果裁剪和确认策略。其他第三方 CLI/MCP 也应通过同一个工具
注册表接入，主会话只拿摘要和必要结果；长结果存到任务/文件状态中，避免再次形成一个
无边界的 gateway 上下文。
