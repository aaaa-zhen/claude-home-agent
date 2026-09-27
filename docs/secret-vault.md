# 密码箱（Secret Vault）方案

状态：**已上线**（2026-09-22 晚实施，实施记录与修正见文末）

## 要解决的问题

用户习惯直接在微信里发「XX网站 账号 abc 密码 123」。当前这条消息会以明文落到至少四个地方：

1. `memory/recent-context.md`
2. `memory/daily/YYYY-MM-DD.md`（recent-context 裁剪归档）
3. conversation-context 的 SQLite 流水
4. Claude 的对话 transcript

助手拒绝记录密码是正确的安全纪律，但导致用户每次都要重发 —— 用户体验差，而且**拒绝记录并不能阻止明文落盘**，消息早就进来了。

目标：用户使用习惯零改变，但明文不进模型上下文、不落盘。

## 现状摸底（2026-09-22 实测）

| 项 | 结果 |
|---|---|
| 1Password（`op` CLI / App） | **未安装** |
| macOS Keychain (`/usr/bin/security`) | 可用 |
| 现有 `redact()` | `scripts/conversation-context.mjs:12` |
| 存量疑似明文行数 | daily/ 12 行、skills/ 12 行、learned-facts 6 行、recent-context 1 行、user-profile 1 行 |

### 两个关键发现

**1. `redact()` 已存在，但没覆盖入站用户文本。**
它目前只用于助手回复（`conversation-context.mjs:130`）和后台消息（`weixin-acp-chat-bridge.mjs:36,41`）。
而 `weixin-acp-chat-bridge.mjs:75` 的 `c.receive(request.text, ...)` 写入的是**原始用户文本**。
这就是存量明文的来源。

**2. 现有正则匹配不上用户的实际打法。**
`conversation-context.mjs:17` 要求 `密码[:=：]` 带分隔符，而用户实际输入是「密码 123」（空格）。需要放宽。

**结论：不需要新建一套拦截体系，扩 `redact()` + 补上入站调用点即可。** 比原设想的改动小得多。

## 方案

### 第 1 层：入口拦截（核心）

拦截点选在 `scripts/weixin-acp-chat-bridge.mjs` 的 `enqueueChat()` 入口，**在 `c.receive()` 之前**。

不选 `scripts/prompt-inject.mjs`：该 hook 只能通过 `hookSpecificOutput.additionalContext` 追加内容（见 `prompt-inject.mjs:440-442`），无法改写用户原文。

流程：

```
微信消息
  → detectSecret(text)              // 识别「站点 + 账号? + 密码」
  → 命中：secret.add(name, value)    // 直接入 Keychain
  → text 替换为「[已存入密码箱：<name>]」
  → 下游（journal / prompt / recent-context）只见替换后的文本
```

模型从头到尾看不到明文。

### 第 2 层：保险箱本体 `scripts/secret.mjs`

底层 macOS Keychain，service 固定为 `home-agent`：

```bash
# 写入：值走 stdin，不走 argv（避免 ps 泄露）
security add-generic-password -s home-agent -a <name> -w - -U
```

命令集**故意只有四个**：

| 命令 | 行为 |
|---|---|
| `add <name>` | 从 stdin 读值写入 |
| `list` | **只输出名字**，不输出值 |
| `use <name> --into <target>` | 脚本自行注入，只回 `ok` / `failed` |
| `rm <name>` | 删除 |

**刻意不提供 `get`** —— 不留任何"打印明文到 stdout"的路径。助手即使想看也没有接口。这是设计约束，不是遗漏。

### 第 3 层：注入目标

| 场景 | 做法 |
|---|---|
| 网页填表 | 脚本内部取值，经 ego-browser CDP 直接写入输入框，值不回传 |
| curl / ssh | 注入子进程环境变量，进程退出即销 |

### 第 4 层：审计 + 洗存量

- `memory/secret-audit.log`：每次取用记一行「时间 + 名字 + 用途」，**不记值**
- 一次性脚本扫 `memory/`、`memory/daily/`、transcript，把存量明文替换为 `[已移入密码箱:<name>]`
- 含 2026-09-17 用户明确要求记下、目前仍明文存放的那条密码

## 用户侧的使用方式

**不变。** 照旧发「XX网站 账号 abc 密码 123」。
助手回「XX 存好了」。以后说「登录 XX」即可。

用户需要记住的只有一句：**照旧发**。

## 已知局限（必须如实告知用户）

1. **微信服务器那一跳擦不掉。** 消息离开手机到达本机之前，明文已经过腾讯。
2. **当次 Claude transcript 擦不掉。** 拦截发生在 bridge，早于模型，所以模型上下文是干净的；但若正则漏判，该次 transcript 仍有明文。
3. **正则不可能全准。** 漏判 = 明文照旧进来（不比现状更糟）；误判 = 存了不该存的（烦但无害）。策略：调宽松，宁可多存，用户发现误判后删除。

→ 因此**银行、主邮箱、支付类凭证不走这条路**，由用户自行手动存入 Keychain。

## 工作量

| 项 | 估计 |
|---|---|
| `secret.mjs` | ~80 行 |
| bridge 拦截 + 扩 `redact()` | ~40 行 |
| 注入适配 | ~30 行 |
| 洗存量脚本 | ~1 小时 |

合计约半天 + 1 小时。

## 与 Grok Bot / 1Password 的关系

Grok Bot 的「密码箱」并非自研，是接的 1Password（2026-09 官宣）。其安全模型的三条约束值得照搬：

1. 密钥不进模型上下文
2. 逐次审批，而非一次授权长期通行
3. 登录 / 2FA / 支付步骤把控制权交还给人

本方案对应：第 1 层保证第①条；第 2 层无 `get` 接口 + 审计日志对应第③条。
**第②条（逐次审批）本方案未实现** —— 当前是存入后助手可直接调用 `use`。
若要补，可在 `use` 时推一条微信要求确认。留作后续增强。

因本方案跑在用户自己机器上，无云端浏览器中转，链路比 Grok Bot 更短。

---

## 实施记录（2026-09-22 晚，Pro 远程完成）

**代码**：`scripts/secret-detect.mjs`（识别 + 替换）、`scripts/secret-vault.mjs`（add/list/use/rm，无 get）、
`scripts/secret-vault-migrate.mjs`（存量迁移）、`patches/patch-secret-redaction.mjs`（SDK 侧两处擦除）、
`memory/skills/secret-vault.md`（管家手册）、`tests/test_secret_vault.mjs`。桥 `weixin-acp-chat-bridge.mjs` 的 `enqueueChat()`
在 `c.receive()` 之前拦截；`conversation-context.mjs` 的 `redact()` 放宽到「密码 123」。

**对原方案的三处修正**（实测得出）：

1. 桥上拦截管不到 SDK 自己的两处落盘：`appendWeixinTurnMemory` 写 `recent-context.md` 的 `[wechat-direct]` 行用的是原始消息体，
   `[weixin-msg] start … text=` 把入站全文写进 `/tmp/openclaw/openclaw-<日期>.log`。已用 `patch-secret-redaction.mjs` 在这两处套上 `redact()`，
   `start.sh` 每次启动重打。
2. 钥匙串从 SSH 会话写不进去（`User interaction is not allowed`），只有 launchd GUI 域（管家所在）可以。所以 `add`/`use` 必须在管家进程树里跑；
   在 Pro 上调试要通过 launchd 起临时 job。值不走 argv 的正确写法是 `security -i` 从 stdin 读命令（`-w -` 不是读 stdin）。
3. 「逐次审批」用便宜的结构替代：`use` 只在桥写下的前台回合标记（`runtime/secret-vault/foreground.json`，20 分钟内）存在时可用，
   心跳、cron、后台任务一律拒绝。

**要如实说的边界**：管家有不受限的 Bash，理论上能直接调 `security` 读值。「没有 get」是接口约定 + CLAUDE.md 规则 + 审计日志，
不是对模型本身的硬隔离。要做硬隔离得给钥匙串条目设 ACL 并用独立签名的取值程序，本期没做。
另外 `.git-private` 的历史、`_migration/backups/` 里的旧副本、Claude 会话转录（`.claude-agent/projects/*.jsonl`）里的存量明文本次没有清洗。
