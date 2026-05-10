#!/usr/bin/env python3
"""Generate images via AiHubMix (gpt-image-2) API."""

import argparse
import base64
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv(Path(__file__).parent / ".env")

client = OpenAI(
    api_key=os.getenv("AIHUBMIX_API_KEY"),
    base_url=os.getenv("AIHUBMIX_BASE_URL"),
)

TMP_DIR = Path(__file__).parent / "tmp"
TMP_DIR.mkdir(exist_ok=True)


def generate(prompt: str, size: str = "1024x1024", quality: str = "auto") -> str:
    """Generate an image and save to tmp/. Returns the file path."""
    response = client.images.generate(
        model="gpt-image-2",
        prompt=prompt,
        n=1,
        size=size,
        quality=quality,
    )

    image_bytes = base64.b64decode(response.data[0].b64_json)
    filename = f"img_{int(time.time())}.png"
    filepath = TMP_DIR / filename
    filepath.write_bytes(image_bytes)
    print(str(filepath))
    return str(filepath)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt", help="Image generation prompt")
    parser.add_argument("--size", default="1024x1024",
                        choices=["1024x1024", "1024x1536", "1536x1024", "auto"])
    parser.add_argument("--quality", default="auto",
                        choices=["high", "medium", "low", "auto"])
    args = parser.parse_args()
    generate(args.prompt, args.size, args.quality)
