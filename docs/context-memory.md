# 对话工作记忆

主入口仍是一个连续 Claude 会话。本次没有新增模型、Word/Excel 工具或技能目录。

`runtime/conversation-context.db` 保存微信请求、助理回复和带来源的工作便笺。请求入队前落盘；只有正在执行的请求会进入提示，排队中的后续消息不会提前混入。`answered` 表示助理生成了回复，并不代表微信已送达或设备操作已验证。服务中断只标记 `interrupted`，不自动重放设备控制。

每次提示最多补入约 6800 字符：最近 4 轮真实对话、最多 2 条相关旧对话、最多 3 个相关/未完成话题，以及最近完成或取消的标题。正文保留开头和末尾。心跳、定时播报不进入用户对话记录。历史流水导入标记为 `legacy-truncated`，不是完整证据。

## 何时更新

复杂事情要跨几轮继续、用户改变目标、明确纠正、任务完成/取消/等待补充时，更新对应便笺。开灯、调温、一般问答和闲聊无需每次记便笺。已有事实和长期偏好仍使用原来的 memory 文件和 memory-apply；不再建一份长期记忆。

先读 `node scripts/conversation-context.mjs topics`。新话题用稳定的短 id，`expected_revision` 为 0；续改同一 id，带当前 revision。当前用户消息的 id 在自动上下文 `current_turn_id` 中。来源旧消息用 `node scripts/conversation-context.mjs turn ID` 查原文。

把 JSON 写到临时文件后用标准输入传入，避免 shell 转义问题：

```bash
node scripts/conversation-context.mjs update < /absolute/path/context-update.json
```

示意结构（其中 SOURCE 必须替换为真实消息 id）：

```json
{
  "id": "home-agent-context",
  "expected_revision": 0,
  "title": "Home Agent 上下文优化",
  "goal": "改善当前目标、指代、纠正和跨会话续接",
  "status": "active",
  "source_ids": ["SOURCE"],
  "facts": [{"text": "用户暂不需要办公文件能力", "kind": "user", "source_id": "SOURCE", "quote": "原消息中确实出现的逐字片段"}],
  "next_step": "验证话题切换与重启后的衔接",
  "question": "",
  "refs": []
}
```

状态只有 `active / waiting / done / cancelled`。保留真正未完成的目标；插入一个小请求不等于取消其他目标。完成和取消后必须更新对应状态，不能仅删掉下一步。`question` 只放真实未解问题，不能因为交付文件就自动造一个“请确认收到”。

事实 `kind` 区分 `user`（用户明说）、`assistant_report`（此前助理说法，仍可能错）、`hypothesis`（待验证假设）。前两种需要原消息里可定位的逐字引用；脚本会拒绝伪造来源、旧版本覆盖新版本。操作是否成功按实际证据判断，不把 HTTP 成功/文件访问当最终完成。

`refs` 存必要的文件路径、媒体 id 或来源链接。链接含密钥会被脱敏，需要访问时回到原始文件或媒体索引查找；禁止把凭据写进便笺。

## 查询与恢复

- `node scripts/conversation-context.mjs context "查询主题"`：查看实际会注入的候选上下文。
- `node scripts/conversation-context.mjs topics`：所有话题与 revision。
- `node scripts/conversation-context.mjs turn ID`：查询来源消息。
- 会话压缩前的 checkpoint 同时保存工作便笺；模型摘要失败时真实对话仍在数据库中。
- 重启后出现 interrupted：先查结果和设备状态，再续接。用户已取消的事不要继续。

备份 `conversation-context.db` 必须使用 SQLite backup（Node 的 `backup()`），不能只拷贝 WAL 模式的主文件。`scripts/backup-conversation-context.mjs` 会写出一致快照；backup-memory.sh 已接入。

这些是上下文支持，不是事实判定器。语义检索目前用中文双字/英文词匹配；模糊指代仍需主模型综合判断。便笺在主模型实际调用 update 时才更新，普通对话记录则自动保存。
