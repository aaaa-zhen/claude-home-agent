# 记忆索引

本目录存储 agent 的长期记忆，按类型分文件管理。收到用户消息时，先看这个索引决定需要读哪些文件。

**标签检索**：每条记忆带 `#标签`，可用 `grep "#标签" memory/learned-facts.md` 精准查找。

| 文件 | 内容 | 何时读取 |
|------|------|----------|
| [user-profile.md](user-profile.md) | 用户画像：家庭成员、偏好、日程 | 涉及用户个人信息、偏好时 |
| [devices.md](devices.md) | 智能家居设备清单、entity_id | 查询/控制家居设备时 |
| [conversation-summary.md](conversation-summary.md) | 近期对话摘要（滚动保留 30 天） | 需要回忆之前聊过什么时 |
| [learned-facts.md](learned-facts.md) | 分类知识库：行为规则、设备操作、自动化规则 | 不确定某个信息时按标签搜索 |
| [pending-followups.md](pending-followups.md) | 待跟进事项 | **每次新对话开始时必读** |
| [recent-context.md](recent-context.md) | 短期对话缓冲：最近 10 轮对话摘要 | **每次新对话开始时必读** |
| [location-log.md](location-log.md) | 位置记录（monitor.py 自动生成） | 涉及位置/出行时 |
