# OpenAI HomePod Voice Spec

## 目标

在不改动现有 HomePod 安全投送链路的前提下，把 OpenAI TTS 作为主要语音生成器，使中文短播报拥有更自然的语气、停顿和节奏。

## 实现决策

- 使用 OpenAI Speech API 的 `gpt-4o-mini-tts`，默认音色由用户试听后选定为 `nova`。
- 使用固定、可配置的普通话风格指令，不向日常工具开放任意系统提示。
- 继续生成 MP3，复用现有 HomePod 排队、临时 HTTP 服务、传输验证和状态恢复。
- OpenAI 请求失败、无密钥或不可用时自动回退到 `edge-tts`，并在结构化结果中返回实际提供方和警告。
- 保留旧的 `zh-CN-...Neural` voice 调用兼容性；这类音色自动走 edge。
- 不在日志、错误或返回值中包含 API 密钥。

## 验证

- 单元测试覆盖 OpenAI 成功、失败回退和旧 edge voice 兼容。
- 使用真实 API 生成一条短中文语音，经真实 HomePod 播放。
- 验证结果包含 `tts_provider=openai`，且临时文件被清理、HomePod 状态恢复。
