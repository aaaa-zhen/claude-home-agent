---
name: media-library
description: Find, recall, inspect, or resend the user's previous WeChat images, videos, voice messages, screenshots, files, and generated media from the local media vault.
---

# Media Library

Use this skill when the user refers to prior media: "刚才那张图", "之前的视频", "找一下截图", "把原图发我", "这张图再发一遍", "上次那个文件".

## Sources

- New media index: `media/index.jsonl`
- Legacy media index, only when the user explicitly asks for old/imported media: `/Users/zhen/.openclaw/openclaw-weixin/media-archive/index.jsonl`
- Media vault docs: `media/README.md`

## Procedure

1. Search first, do not scan every media directory manually.
   - Recent images: `node scripts/media-search.mjs --type image --limit 10`
   - Recent videos: `node scripts/media-search.mjs --type video --limit 10`
   - Keyword search: `node scripts/media-search.mjs --q "keyword" --limit 10`
   - Legacy search, only if explicitly requested: add `--include-legacy`
2. Prefer the newest matching record unless the user gives a date, topic, caption, or other clue.
3. If the user wants the original media sent back, reply with `[send_file:/absolute/path]`.
4. If the user wants analysis, inspect the file directly when needed, then answer concisely.
5. If multiple records may match, ask one short clarification instead of guessing.

## Notes

- Do not expose the media index implementation unless the user asks how it works.
- Never reveal unrelated media filenames or paths when the user asks for one specific item.
