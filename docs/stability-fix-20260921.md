# Home Agent 修复记录

2026-09-21 06:45（北京时间）已部署至 Air。

本次故障是 06:19、06:28 两轮 Claude 输出仅含 thinking，没有正文，重试亦为空。会话未卡在压缩、进程未退出；原桥接又将空结果记为 answered / 媒体回复。项目模型配置固定为 claude-opus-4-8，覆盖用户级 claude-opus-5 配置。

官方仓库有同型的用户报告：[Opus 4.8 thinking-only end_turn](https://github.com/anthropics/claude-code/issues/80793)。该报告支持规避 4.8 的判断，但不是本次故障内部根因已获厂商确认的证明。

已完成：

- 主模型改为 claude-opus-5，隔离调用和真实主会话均确认可用。没有升级 CLI/SDK 依赖或关闭模型思考。
- 新增有效回复检测。无文字且无有效媒体时抛出明确错误，记录 empty_response，交由现有微信错误通知链路反馈；不会自动重放设备操作。
- 对话状态分成 generated、accepted、rejected、delivery_unknown。微信接口确认前不再登记已回复；发送结果不明不自动重发。
- 主聊天、媒体和主动推送共享业务回执校验；修复 HTTP 200 但 ret 非零仍被当成功的问题。
- SDK HTTP 超时覆盖完整响应体读取；已接受回复不因后续本地记账出错而被降级或再发错误通知。
- 缺失媒体文件时，记下实际发送的失败说明，不再保留错误的媒体成功描述。
- 新增真实用户回复状态健康检查并接入现有故障告警。修正磁盘检查到项目数据卷。
- 保存交接后重启主进程；将今早两条经转录确认的假成功记录修正为空回复，原记录保存在备份中。

验证：

- 72 项 Node 测试 + 21 项会话管理测试，合计 93 项全部通过，包含新增 17 项故障回归。
- 真实主会话三轮测试：简短回答 4.20 秒；“主卧的”指代理解 3.07 秒；只读 date 工具调用及回答 4.05 秒。
- 新会话为 3756cca8-3855-4a41-9c07-781844d19e62，实测模型 claude-opus-5，测试后 busy=false。
- 未向微信主动发送测试消息，未重做此前的空调请求；真实微信送达尚待下一条实际消息验证。接口 accepted 也不等于用户已读。
- 健康检查仍保留最近真实消息的 empty_response；内部测试不会冒充用户消息，把历史失败抹成成功。下一条实际消息取得回执后更新健康状态。

Air 备份目录：

`/Users/zhen/home-agent/_migration/backups/stability-fix-20260921-064356`

包含修改前源文件、SDK bundle、项目模型配置、数据库快照、旧会话转录、旧交接、修正前消息记录和前后 SHA-256 清单。回退须先核对清单，避免覆盖后续新改动；不要恢复整份旧数据库，否则会覆盖新消息。

主要代码入口（Air 项目 `/Users/zhen/home-agent/weixin-agent`）：

- `scripts/chat-reply-lifecycle.mjs`：有效回复与生命周期。
- `scripts/weixin-response-validation.mjs`：统一业务回执校验。
- `patches/patch-chat-reliability.mjs`：SDK 发送与回执补丁，启动自动重应用。
- `scripts/chat-health.mjs`：真实消息回复检查。
- `tests/test_chat_reliability.mjs`：故障回归。
- `runtime/chat-reply-events.jsonl`：无正文内容的结构化阶段日志，带滚动文件。

边界：本轮完成复发问题的模型规避及第一批防漏回修复；普通回合挂起后的统一取消、持久 inbox/outbox 队列等较大改造尚未实施。三轮成功不能证明以后永不出现模型空输出，但空输出已不再走静默成功路径。
