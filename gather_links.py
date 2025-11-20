#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.13"
# dependencies = [
#   "requests",
# ]
# ///

import argparse
import json
import re
from pathlib import Path
from html.parser import HTMLParser
from typing import Dict, Iterable, List, Optional, Sequence

import requests

USER_AGENT = "discord-newsletter-fetcher/0.1 (+https://example.invalid)"
LINK_RE = re.compile(r"https?://\S+")


class MetaParser(HTMLParser):
    """Lightweight HTML parser to grab meta/title tags."""

    def __init__(self) -> None:
        super().__init__()
        self.meta: Dict[str, str] = {}
        self._in_title = False
        self._title_chunks: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[tuple[str, Optional[str]]]) -> None:
        if tag.lower() == "meta":
            attr_map = {k.lower(): v for k, v in attrs if v is not None}
            key = attr_map.get("property") or attr_map.get("name")
            content = attr_map.get("content")
            if key and content and key.lower() not in self.meta:
                self.meta[key.lower()] = content.strip()
        elif tag.lower() == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self._in_title = False
            if self._title_chunks and "title" not in self.meta:
                self.meta["title"] = "".join(self._title_chunks).strip()

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_chunks.append(data)


class LinkPreviewer:
    """Fetch link metadata once and cache results."""

    def __init__(self, session: Optional[requests.Session] = None) -> None:
        self.session = session or requests.Session()
        self.cache: Dict[str, Optional[str]] = {}

    def fetch(self, url: str) -> Optional[str]:
        if url in self.cache:
            return self.cache[url]

        print(f"[fetch] {url}")
        try:
            response = self.session.get(
                url, headers={"User-Agent": USER_AGENT}, timeout=8
            )
            if not response.ok or "text/html" not in response.headers.get(
                "content-type", ""
            ):
                print(f"[fetch] skipped (status/content-type): {url}")
                self.cache[url] = None
                return None
        except requests.RequestException:
            print(f"[fetch] error: {url}")
            self.cache[url] = None
            return None

        parser = MetaParser()
        try:
            parser.feed(response.text)
        except Exception:
            print(f"[fetch] parse error: {url}")
            self.cache[url] = None
            return None

        description = self._best_description(parser.meta)
        if description:
            description = self._trim(description)
        self.cache[url] = description
        print(f"[fetch] description: {description or 'None'}")
        return description

    @staticmethod
    def _best_description(meta: Dict[str, str]) -> Optional[str]:
        for key in (
            "og:description",
            "description",
            "twitter:description",
            "og:title",
            "title",
        ):
            value = meta.get(key)
            if value:
                return value
        return None

    @staticmethod
    def _trim(text: str, limit: int = 240) -> str:
        cleaned = " ".join(text.split())
        if len(cleaned) <= limit:
            return cleaned
        return cleaned[: limit - 1].rstrip() + "…"


def message_has_link(message: dict) -> bool:
    """Return True when a message's text contains a link."""
    content = message.get("content") or ""
    return bool(LINK_RE.search(content))


def format_message(message: dict, fetch_preview) -> List[str]:
    """Format a message as lines prefixed with the username, adding link previews."""
    author = message.get("author") or {}
    username = author.get("nickname") or author.get("name") or "Unknown"
    content = message.get("content") or ""
    lines = content.splitlines() or [""]
    formatted = [f"{username}: {lines[0]}"]
    for line in lines[1:]:
        formatted.append(f"    {line}")

    seen_links = set()
    for link in LINK_RE.findall(content):
        if link in seen_links:
            continue
        seen_links.add(link)
        description = fetch_preview(link)
        if description:
            formatted.append(f"    [preview] {description}")

    return formatted


def iter_contexts(messages: Sequence[dict]) -> Iterable[List[dict]]:
    """Yield slices containing the previous 10 messages plus the link message."""
    for idx, message in enumerate(messages):
        if not message_has_link(message):
            continue
        start = max(0, idx - 10)
        yield messages[start : idx + 1]


def process_json_file(path: Path, fetch_preview) -> List[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    messages = data.get("messages") or []
    if not messages:
        return []

    messages = sorted(messages, key=lambda m: m.get("timestamp") or "")

    blocks: List[str] = []
    for context in iter_contexts(messages):
        link_message = context[-1]
        timestamp = link_message.get("timestamp") or "unknown time"
        blocks.append(f"=== {path.name} @ {timestamp} ===")
        for message in context:
            blocks.extend(format_message(message, fetch_preview))
        blocks.append("")  # blank line between blocks

    return blocks


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Find messages containing links in exported Discord JSON files and "
            "write them with the 10 preceding messages."
        )
    )
    parser.add_argument(
        "--out-dir",
        default="out",
        type=Path,
        help="Directory containing Discord export JSON files.",
    )
    parser.add_argument(
        "--output",
        default="messages_with_links.txt",
        type=Path,
        help="Path to the output text file.",
    )
    args = parser.parse_args()

    previewer = LinkPreviewer()

    output_lines: List[str] = []
    for path in sorted(args.out_dir.glob("*.json")):
        output_lines.extend(process_json_file(path, previewer.fetch))

    args.output.write_text("\n".join(output_lines), encoding="utf-8")


if __name__ == "__main__":
    main()
