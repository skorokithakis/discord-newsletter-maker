#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.13"
# dependencies = [
#   "requests",
#   "beautifulsoup4",
# ]
# ///

import argparse
import json
import re
from datetime import datetime
from pathlib import Path
from html.parser import HTMLParser
from typing import Callable, Dict, Iterable, List, Optional, Sequence

import requests
from bs4 import BeautifulSoup

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


def extract_text(html: str) -> str:
    """Collect visible text content using BeautifulSoup for robustness."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text(" ", strip=True)
    return " ".join(text.split())


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

        meta_parser = MetaParser()
        try:
            meta_parser.feed(response.text)
        except Exception:
            print(f"[fetch] parse error: {url}")
            self.cache[url] = None
            return None

        try:
            description = extract_text(response.text)
        except Exception:
            print(f"[fetch] text parse error: {url}")
            description = None

        if not description:
            description = self._best_description(meta_parser.meta)
        if description:
            description = self._normalize(description)
        self.cache[url] = description or None
        print(f"[fetch] description length: {len(description) if description else 0}")
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
    def _normalize(text: str) -> str:
        return " ".join(text.split())


def message_has_link(message: dict) -> bool:
    """Return True when a message's text contains a link."""
    content = message.get("content") or ""
    return bool(LINK_RE.search(content))


def format_message(message: dict) -> dict:
    """Normalize a message structure for downstream use."""
    author = message.get("author") or {}
    username = author.get("nickname") or author.get("name") or "Unknown"
    return {
        "author": username,
        "content": message.get("content") or "",
        "timestamp": message.get("timestamp") or None,
    }


def iter_contexts(messages: Sequence[dict]) -> Iterable[List[dict]]:
    """Yield slices containing the previous 10 messages plus the link message."""
    for idx, message in enumerate(messages):
        if not message_has_link(message):
            continue
        start = max(0, idx - 10)
        yield messages[start : idx + 1]


def parse_timestamp(timestamp: Optional[str]) -> Optional[datetime]:
    if not timestamp:
        return None
    ts = timestamp.strip()
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        if ts.endswith("Z"):
            try:
                return datetime.fromisoformat(ts.removesuffix("Z") + "+00:00")
            except ValueError:
                return None
    return None


def process_json_file(
    path: Path, fetch_preview, record_bounds: Callable[[Sequence[dict]], None]
) -> List[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    messages = data.get("messages") or []
    if not messages:
        return []

    messages = sorted(messages, key=lambda m: m.get("timestamp") or "")
    record_bounds(messages)

    blocks: List[dict] = []
    for context in iter_contexts(messages):
        link_message = context[-1]
        timestamp = link_message.get("timestamp") or "unknown time"
        links = []
        seen_links: set[str] = set()
        for link in LINK_RE.findall(link_message.get("content") or ""):
            if link in seen_links:
                continue
            seen_links.add(link)
            links.append({"url": link, "description": fetch_preview(link)})

        blocks.append(
            {
                "source": path.name,
                "timestamp": timestamp,
                "messages": [format_message(message) for message in context],
                "links": links,
            }
        )

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
        default="messages_with_links.json",
        type=Path,
        help="Path to the output JSON file.",
    )
    args = parser.parse_args()

    previewer = LinkPreviewer()

    bounds: Dict[str, Optional[tuple[datetime, str]]] = {
        "earliest": None,
        "latest": None,
    }

    def update_bounds(messages: Sequence[dict]) -> None:
        for message in messages:
            timestamp = message.get("timestamp")
            parsed = parse_timestamp(timestamp)
            if not parsed:
                continue
            earliest = bounds["earliest"]
            latest = bounds["latest"]
            if earliest is None or parsed < earliest[0]:
                bounds["earliest"] = (parsed, timestamp)  # keep original string
            if latest is None or parsed > latest[0]:
                bounds["latest"] = (parsed, timestamp)

    contexts: List[dict] = []
    for path in sorted(args.out_dir.glob("*.json")):
        contexts.extend(process_json_file(path, previewer.fetch, update_bounds))

    payload = {"contexts": contexts}
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    if bounds["earliest"] is None or bounds["latest"] is None:
        print("No messages with timestamps found.")
    else:
        print(f"Earliest message timestamp: {bounds['earliest'][1]}")
        print(f"Latest message timestamp: {bounds['latest'][1]}")


if __name__ == "__main__":
    main()
