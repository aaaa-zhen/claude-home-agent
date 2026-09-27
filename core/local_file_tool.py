#!/usr/bin/env python3
"""Small local-file lookup tool for backend agents.

The tool intentionally avoids enumerating macOS privacy-protected Desktop
folders from launchd workers. It only probes plausible direct candidate paths;
the backend agent still decides what to do with the returned candidates.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any


ROOT = Path(os.getenv('WEIXIN_AGENT_ROOT') or Path(__file__).resolve().parents[1])
DESKTOP = Path.home() / "Desktop"


def extension_hints(query: str) -> list[str]:
    normalized = query.lower()
    hints: list[str] = []
    if re.search(r"(^|[\s._-])md($|[\s._-])|markdown|\.md\b", normalized):
        hints.append(".md")
    if re.search(r"html|网页|\.html?\b", normalized):
        hints.extend([".html", ".htm"])
    if re.search(r"pdf|\.pdf\b", normalized):
        hints.append(".pdf")
    if re.search(r"png|图片|截图|\.png\b", normalized):
        hints.append(".png")
    if re.search(r"jpe?g|照片|\.jpe?g\b", normalized):
        hints.extend([".jpg", ".jpeg"])
    return list(dict.fromkeys(hints))


def query_tokens(query: str) -> list[str]:
    tokens = [
        token.strip(" ._-")
        for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9._-]{2,}", query)
        if token.lower() not in {"desktop", "markdown", "html", "send", "file"}
    ]
    normalized = query.lower()
    if "home-agent-current" in normalized and "home-agent-current" not in tokens:
        tokens.append("home-agent-current")
    return list(dict.fromkeys(token for token in tokens if token))


def candidate_names(token: str, hints: list[str]) -> list[str]:
    suffixes = ["", "-architecture", "-current", "-doc", "-document", "-report"]
    names = [token]
    if hints:
        for suffix in suffixes:
            for ext in hints:
                base = token if token.lower().endswith(ext) else f"{token}{suffix}{ext}"
                names.append(base)
    return list(dict.fromkeys(names))


def resolve(query: str, *, include_root: bool = True) -> dict[str, Any]:
    hints = extension_hints(query)
    tokens = query_tokens(query)
    search_dirs = [DESKTOP]
    if include_root:
        search_dirs.append(ROOT)

    candidates: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for directory in search_dirs:
        for token in tokens:
            for name in candidate_names(token, hints):
                path = (directory / name).resolve()
                if path in seen:
                    continue
                seen.add(path)
                if path.is_file():
                    candidates.append(
                        {
                            "path": str(path),
                            "name": path.name,
                            "size": path.stat().st_size,
                            "mtime": path.stat().st_mtime,
                        }
                    )

    candidates.sort(key=lambda item: (score_name(item["name"], query, hints), item["mtime"]), reverse=True)
    return {"ok": True, "query": query, "tokens": tokens, "extensions": hints, "candidates": candidates}


def score_name(name: str, query: str, hints: list[str]) -> int:
    normalized = query.lower()
    lower_name = name.lower()
    score = 0
    for part in re.split(r"[^a-z0-9]+", lower_name):
        if part and part in normalized:
            score += len(part)
    if any(lower_name.endswith(ext) for ext in hints):
        score += 20
    return score


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Resolve likely local file paths without broad Desktop scans.")
    sub = parser.add_subparsers(dest="command", required=True)
    resolve_parser = sub.add_parser("resolve")
    resolve_parser.add_argument("--query", required=True)
    resolve_parser.add_argument("--no-root", action="store_true")
    resolve_parser.add_argument("--json", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "resolve":
        result = resolve(args.query, include_root=not args.no_root)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            for item in result["candidates"]:
                print(item["path"])
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
