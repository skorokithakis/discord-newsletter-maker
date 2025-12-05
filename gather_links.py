#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.13"
# dependencies = [
#   "requests",
#   "beautifulsoup4",
#   "openai",
# ]
# ///
import argparse
import json
import os
import re
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable
from typing import Dict
from typing import Iterable
from typing import List
from typing import Optional
from typing import Sequence

import requests
from bs4 import BeautifulSoup
from openai import APIConnectionError
from openai import APIError
from openai import APITimeoutError
from openai import AuthenticationError
from openai import OpenAI

USER_AGENT = "discord-newsletter-fetcher/0.1 (+https://example.invalid)"
LINK_RE = re.compile(r"https?://\S+")
SUMMARY_MODEL = "gpt-5-mini-2025-08-07"
FETCHER_SESSION = requests.Session()


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


def best_description(meta: Dict[str, str]) -> Optional[str]:
    """Extract the best available description from meta tags."""
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


def fetch_meta_description(url: str, *, fetcher_name: str) -> Optional[str]:
    """Download a page and pull the best meta description from its tags."""
    try:
        response = FETCHER_SESSION.get(
            url, headers={"User-Agent": USER_AGENT}, timeout=8
        )
        if not response.ok or "text/html" not in response.headers.get(
            "content-type", ""
        ):
            print(f"[{fetcher_name}] skipped (status/content-type): {url}")
            return None
    except requests.RequestException as exception:
        print(f"[{fetcher_name}] request error for {url}: {exception}")
        return None

    try:
        meta_parser = MetaParser()
        meta_parser.feed(response.text)
        meta_description = best_description(meta_parser.meta)
        if meta_description:
            return meta_description
        print(f"[{fetcher_name}] no meta description: {url}")
    except Exception as exception:
        print(f"[{fetcher_name}] parsing error for {url}: {exception}")

    return None


def youtube_fetcher(url: str) -> Optional[str]:
    """Extract video description from YouTube pages."""
    try:
        response = FETCHER_SESSION.get(
            url, headers={"User-Agent": USER_AGENT}, timeout=8
        )
        if not response.ok:
            print(f"[youtube_fetcher] failed to fetch: {url}")
            return None

        soup = BeautifulSoup(response.content, "html.parser")
        pattern = re.compile(r'(?<=shortDescription":").*(?=","isCrawlable)')
        matches = pattern.findall(str(soup))
        if matches:
            description = matches[0].replace("\\n", "\n")
            return description
        else:
            print(f"[youtube_fetcher] no description found: {url}")
            return None
    except requests.RequestException as exception:
        print(f"[youtube_fetcher] request error for {url}: {exception}")
        return None
    except Exception as exception:
        print(f"[youtube_fetcher] error for {url}: {exception}")
        return None


def github_fetcher(url: str) -> Optional[str]:
    """Extract repository description for GitHub pages via meta tags."""
    description = fetch_meta_description(url, fetcher_name="github_fetcher")
    if description:
        return description

    # Fallback to repository root for deep links if the page lacked meta tags.
    parts = url.split("/")
    if len(parts) >= 5 and parts[2] == "github.com":
        repo_url = f"https://github.com/{parts[3]}/{parts[4]}"
        if repo_url != url:
            return fetch_meta_description(repo_url, fetcher_name="github_fetcher")
    return None


def mastodon_fetcher(url: str) -> Optional[str]:
    """Extract toot preview using Mastodon's Open Graph description."""
    return fetch_meta_description(url, fetcher_name="mastodon_fetcher")


def default_fetcher(url: str) -> Optional[str]:
    """Download page and extract text using BeautifulSoup, fallback to meta description."""
    try:
        response = FETCHER_SESSION.get(
            url, headers={"User-Agent": USER_AGENT}, timeout=8
        )
        if not response.ok or "text/html" not in response.headers.get(
            "content-type", ""
        ):
            print(f"[default_fetcher] skipped (status/content-type): {url}")
            return None
    except requests.RequestException:
        print(f"[default_fetcher] request error: {url}")
        return None

    # Try to extract full text first.
    try:
        text = extract_text(response.text)
        if text:
            return text
    except Exception:
        print(f"[default_fetcher] text extraction failed: {url}")

    # Fallback to meta description.
    try:
        meta_parser = MetaParser()
        meta_parser.feed(response.text)
        meta_description = best_description(meta_parser.meta)
        if meta_description:
            return meta_description
    except Exception:
        print(f"[default_fetcher] meta parsing failed: {url}")

    return None


FETCHERS: List[tuple[re.Pattern, Callable[[str], Optional[str]]]] = [
    (re.compile(r"youtube\.com/(watch|shorts)"), youtube_fetcher),
    (re.compile(r"youtu\.be/"), youtube_fetcher),
    (
        re.compile(
            r"github\.com/[^/]+/[^/]+/(blob|tree|commit|pull|issues|actions|wiki)/"
        ),
        github_fetcher,
    ),
    (re.compile(r"https?://[^/]+/@[^/]+/\d+"), mastodon_fetcher),
    (re.compile(r".*"), default_fetcher),
]


class PageSummarizer:
    """Wrap a small LLM call to summarize page text."""

    def __init__(
        self,
        client: Optional[OpenAI],
        model: str = SUMMARY_MODEL,
    ) -> None:
        self.client = client
        self.model = model

    @classmethod
    def create(cls) -> "PageSummarizer":
        client: Optional[OpenAI]
        try:
            api_key = os.getenv("OPENAI_API_KEY")
            client = OpenAI(api_key=api_key) if api_key else None
            if client is None:
                print("[summary] OPENAI_API_KEY not set; skipping summaries.")
        except Exception as exc:
            print(f"[summary] Failed to initialize OpenAI client: {exc}")
            client = None
        return cls(client=client)

    def summarize(self, text: str) -> Optional[str]:
        if not self.client:
            return None

        clean_text = text.strip()
        if not clean_text:
            return None

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You summarize long web pages into 2-3 concise English sentences. "
                            "Focus on the main topic, key findings, and notable details "
                            "that would matter to a newsletter reader. Return the empty "
                            "string if there wasn't enough information in the text for a"
                            " summary."
                        ),
                    },
                    {"role": "user", "content": clean_text},
                ],
            )
        except (
            APIError,
            APIConnectionError,
            APITimeoutError,
            AuthenticationError,
        ) as exc:
            print(f"[summary] OpenAI API error: {exc}")
            return None
        except Exception as exc:  # defensive catch-all
            print(f"[summary] Unexpected error: {exc}")
            return None

        choice = response.choices[0]
        content = getattr(choice.message, "content", None)
        if not content:
            return None
        return content.strip()


class LinkPreviewer:
    """Fetch link metadata once and cache results."""

    def __init__(
        self,
        session: Optional[requests.Session] = None,
        summarizer: Optional[PageSummarizer] = None,
    ) -> None:
        self.session = session or requests.Session()
        self.summarizer = summarizer
        self.cache: Dict[str, Optional[str]] = {}

    def fetch(self, url: str) -> Optional[str]:
        if url in self.cache:
            return self.cache[url]

        print(f"[fetch] {url}")

        # Find matching fetcher and get description.
        description: Optional[str] = None
        for pattern, fetcher in FETCHERS:
            if pattern.search(url):
                description = fetcher(url)
                break

        print("[fetch] Done, summarizing...")

        # Try to summarize the description.
        if description and self.summarizer:
            summary = self.summarizer.summarize(description)
            if summary:
                description = summary

        # Normalize and cache.
        if description:
            description = self._normalize(description)
        self.cache[url] = description or None
        print(f"[fetch] {description}")
        return description

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


def iter_contexts(messages: Sequence[dict]) -> Iterable[tuple[List[dict], dict]]:
    """Yield slices with 10 messages before and after the link message, plus the link message."""
    for idx, message in enumerate(messages):
        if not message_has_link(message):
            continue
        start = max(0, idx - 10)
        end = min(len(messages), idx + 11)
        # Return a concrete list so downstream consumers don't have to rely on slicing semantics.
        yield list(messages[start:end]), message


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
    for context, link_message in iter_contexts(messages):
        timestamp = link_message.get("timestamp") or "unknown time"
        link_author = format_message(link_message)["author"]
        link_idx_in_context = context.index(link_message)
        links = []
        seen_links: set[str] = set()
        for link in LINK_RE.findall(link_message.get("content") or ""):
            if link in seen_links:
                continue
            seen_links.add(link)
            links.append(
                {
                    "url": link,
                    "description": fetch_preview(link),
                    "posted_by": link_author,
                }
            )

        blocks.append(
            {
                "source": path.name,
                "timestamp": timestamp,
                "link_index": link_idx_in_context,
                "messages": [format_message(message) for message in context],
                "links": links,
            }
        )

    return blocks


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Find messages containing links in exported Discord JSON files and "
            "write them with the 10 preceding and following messages."
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

    summarizer = PageSummarizer.create()
    previewer = LinkPreviewer(summarizer=summarizer)

    bounds: Dict[str, Optional[tuple[datetime, str]]] = {
        "earliest": None,
        "latest": None,
    }

    def update_bounds(messages: Sequence[dict]) -> None:
        for message in messages:
            timestamp = message.get("timestamp")
            if not isinstance(timestamp, str):
                continue
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
