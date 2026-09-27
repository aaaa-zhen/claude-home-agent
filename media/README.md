# Media Library

This directory is the local media vault for the WeChat agent.

## Layout

- `inbox/images/YYYY-MM-DD/` — inbound WeChat images and screenshots
- `inbox/videos/YYYY-MM-DD/` — inbound WeChat videos
- `inbox/audio/YYYY-MM-DD/` — inbound voice/audio messages
- `inbox/files/YYYY-MM-DD/` — inbound files and unknown attachments
- `generated/images/` — AI-generated images ready to send back
- `outbox/` — files prepared for outbound WeChat delivery
- `derived/thumbs/` — thumbnails and previews
- `derived/ocr/` — OCR text extracted from images
- `derived/transcripts/` — audio/video transcripts
- `index.jsonl` — append-only media index

## Index Record

Each line in `index.jsonl` is JSON with fields such as:

```json
{
  "id": "2026-06-25-images-ab12cd34ef56",
  "schemaVersion": 2,
  "savedAt": "2026-06-25T00:00:00.000Z",
  "direction": "inbound",
  "source": "weixin",
  "type": "image",
  "bucket": "images",
  "mimeType": "image/jpeg",
  "filePath": "/Users/zhen/home-agent/weixin-agent/media/inbox/images/2026-06-25/example.jpg",
  "mediaUri": "media://2026-06-25-images-ab12cd34ef56",
  "sizeBytes": 123456,
  "sha256": "...",
  "caption": "",
  "tags": []
}
```

Use `[send_file:/absolute/path]` to send a stored file back through WeChat.

## Retention

- Inbound media under `inbox/` and source assets under `generated/` are durable.
- Video delivery copies under `outbox/` are disposable after seven days.
- `scripts/cleanup-downloaded-videos.sh` removes old videos from `outbox/` and
  `tmp/` once a week; it never touches `inbox/` or `generated/`.
