# Projects

This directory stores small user-requested projects generated from WeChat.

- `previews/` holds published HTML/React preview builds.
- Each preview has its own slug and random access token in `previews/manifest.json`.
- Published files are served through the local API server under `/preview/<slug>/`.

Do not put long-term agent runtime code here.
