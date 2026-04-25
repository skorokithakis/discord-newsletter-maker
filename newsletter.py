#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.13"
# dependencies = [
#   "anthropic",
#   "instructor",
#   "pydantic",
# ]
# ///
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import List
from typing import Sequence

import anthropic
import instructor
from anthropic import APIConnectionError
from anthropic import APIError
from anthropic import APITimeoutError
from anthropic import AuthenticationError
from pydantic import BaseModel

from models import NewsletterGroup
from models import NewsletterLink
from models import NewsletterPayload

DEFAULT_ANTHROPIC_API_URL = "https://api.anthropic.com"


def make_anthropic_client(api_key: str) -> anthropic.Anthropic:
    base_url = os.getenv("ANTHROPIC_API_URL", DEFAULT_ANTHROPIC_API_URL)
    return anthropic.Anthropic(base_url=base_url, api_key=api_key)


SYSTEM_PROMPT = """
You are a newsletter editor for the newsletter of a maker community called 'The
Makery'. Read chat excerpts that contain shared links and their descriptions.
"""

RULES = """
Follow these guidelines AT ALL TIMES:

- Decide which links are worth including (educational, insightful, noteworthy).
- Drop broken or spammy links.
- Drop any links that would NOT REALLY BE OF INTEREST TO A CASUAL NEWSLETTER RECIPIENT. This includes deep links to project, internal business, a specific issue someone is having, etc. If it doesn't belong on a newsletter that would interest a random maker who has no affiliation with the Makery, DO NOT INCLUDE IT.
- Each link is labeled with a number in the context: reference links by their number in your output as `link_number`.
- Group related links under concise section titles. Titles should be "Sentence case", not "Title Case".
- Links that are similar, or talk about the same or similar things, should be added to the same group. Design the groups and order the links in them to maximize reader interest and relevance.
- Populate the structured fields: title, description, and link_number.
- Return your response as groups, each with a title and a list of links.
- Include a short intro sentence that summarizes the main themes of the links, as an intro. Expose it as the `intro` field in your structured response.
- Do not include URLs or usernames in your output; we will attach them using the link number you provide.
- Use the supplied username for context (or don't mention a username, if missing).
- Keep descriptions factual and concise; do not invent details.
- For each link's description, include not just a summary of the web page content itself, but also capture the gist of what the community is saying about the link. Incorporate any opinions, insights, reactions, or general sentiment expressed in the surrounding chat messages. This community context should enrich the description and help readers understand why the link is interesting or valuable to the community.
- If any links don't fit in any other groups, add them to a "Various" group.
- Don't include memes and jokes.
- For each link you include, provide a brief `justification` explaining why it is newsletter-worthy.
- For every link you exclude, add an entry to `excluded_links` with its `link_number` and a brief `justification` explaining why it was dropped.
""".strip()

DEFAULT_MODEL = "claude-opus-4-6"
API_KEY_ENV_VAR = "ANTHROPIC_API_KEY"


class LLMNewsletterLink(BaseModel):
    title: str
    description: str
    link_number: int
    justification: str


class LLMExcludedLink(BaseModel):
    link_number: int
    justification: str


class LLMNewsletterGroup(BaseModel):
    title: str
    links: List[LLMNewsletterLink]


class LLMNewsletterPayload(BaseModel):
    intro: str
    groups: List[LLMNewsletterGroup]
    excluded_links: List[LLMExcludedLink]


def load_contexts(path: Path) -> List[dict]:
    """Load gathered link contexts from a JSON file."""
    data = json.loads(path.read_text(encoding="utf-8"))
    contexts = data.get("contexts") if isinstance(data, dict) else None
    if contexts is None and isinstance(data, list):
        contexts = data
    if not isinstance(contexts, list):
        raise SystemExit("Input JSON must include a 'contexts' array.")
    return contexts


def render_contexts(contexts: Sequence[dict]) -> tuple[str, dict[int, dict[str, str]]]:
    """Turn structured contexts into a text prompt for the model.

    Returns the rendered context and a mapping from link number to its
    source URL/posted_by so we can reattach them after the model chooses.
    """

    def render_links_inline(links: List[dict], link_counter: int) -> int:
        """Append link/description lines and advance the counter."""
        for link in links:
            url = link.get("url") or ""
            posted_by = link.get("posted_by") or "Unknown"
            if url:
                lines.append(
                    f"    [link #{link_counter}] {url} (posted by {posted_by})"
                )
                link_lookup[link_counter] = {"url": url, "posted_by": posted_by}
                link_counter += 1
            description = link.get("description") or ""
            if description:
                lines.append(f"    [description] {description}")
        return link_counter

    lines: List[str] = []
    link_lookup: dict[int, dict[str, str]] = {}
    link_counter = 1
    for context in contexts:
        source = context.get("source") or "unknown file"
        timestamp = context.get("timestamp") or "unknown time"
        lines.append(f"=== {source} @ {timestamp} ===")

        messages = context.get("messages") or []
        links = context.get("links") or []
        link_index = context.get("link_index")

        if not isinstance(link_index, int) or not (0 <= link_index < len(messages)):
            link_index = None

        # Fallback to locate the link message by URL when older data doesn't
        # include link_index.
        if link_index is None and links:
            urls = [link.get("url") for link in links if link.get("url")]
            for idx, message in enumerate(messages):
                content = message.get("content") or ""
                if any(url and url in content for url in urls):
                    link_index = idx
                    break

        for idx, message in enumerate(messages):
            author = message.get("author") or "Unknown"
            content = message.get("content") or ""
            message_lines = content.splitlines() or [""]
            lines.append(f"{author}: {message_lines[0]}")
            for line in message_lines[1:]:
                lines.append(f"    {line}")

            if links and link_index is not None and idx == link_index:
                link_counter = render_links_inline(links, link_counter)

        # If we couldn't find a position for the links, keep the old behaviour
        # of appending them at the end so we don't drop anything.
        if links and link_index is None:
            link_counter = render_links_inline(links, link_counter)

        lines.append("")

    return "\n".join(lines).strip(), link_lookup


def run_completion(
    model: str,
    context: str,
    temperature: float,
    api_key: str,
) -> LLMNewsletterPayload:
    user_message = (
        "Create the newsletter from these Discord snippets. Links are labeled "
        "with [link #N]; refer to them by number in your output. Group related "
        "links together and give each group a concise title.\n\n" + context + RULES
    )
    patched_client = instructor.from_anthropic(make_anthropic_client(api_key))
    return patched_client.messages.create(
        model=model,
        response_model=LLMNewsletterPayload,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
        temperature=temperature,
    )


def attach_link_metadata(
    llm_payload: LLMNewsletterPayload, link_lookup: dict[int, dict[str, str]]
) -> NewsletterPayload:
    """Replace link numbers from the model with the source URL/user details."""
    groups: List[NewsletterGroup] = []
    for group in llm_payload.groups:
        resolved_links: List[NewsletterLink] = []
        for link in group.links:
            source = link_lookup.get(link.link_number)
            if source is None:
                raise SystemExit(
                    f"Model referenced unknown link number: {link.link_number}"
                )
            resolved_links.append(
                NewsletterLink(
                    title=link.title,
                    description=link.description,
                    url=source.get("url") or "",
                    posted_by=source.get("posted_by") or "Unknown",
                )
            )
        groups.append(NewsletterGroup(title=group.title, links=resolved_links))
    return NewsletterPayload(intro=llm_payload.intro, groups=groups)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Summarize Discord link dumps into a short newsletter."
    )
    parser.add_argument(
        "input",
        type=Path,
        help="Path to the JSON file containing the gathered messages with links.",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Anthropic model to use (default: {DEFAULT_MODEL}).",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.4,
        help="Sampling temperature for the model (default: 0.4).",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help=f"Anthropic API key (overrides {API_KEY_ENV_VAR}).",
    )
    args = parser.parse_args(argv)

    api_key = args.api_key or os.getenv(API_KEY_ENV_VAR)
    if not api_key:
        raise SystemExit(
            f"Missing API key. Set {API_KEY_ENV_VAR} or pass --api-key."
        )

    contexts = load_contexts(args.input)
    if not contexts:
        raise SystemExit("No link contexts found in input JSON.")
    context, link_lookup = render_contexts(contexts)

    try:
        llm_payload = run_completion(
            model=args.model,
            context=context,
            temperature=args.temperature,
            api_key=api_key,
        )
    except (
        APIError,
        APIConnectionError,
        APITimeoutError,
        AuthenticationError,
    ) as exc:
        raise SystemExit(f"API error: {exc}") from exc

    payload = attach_link_metadata(llm_payload, link_lookup)

    output = payload.model_dump_json(indent=2)
    Path("curated_links.json").write_text(output, encoding="utf-8")
    total_links = sum(len(group.links) for group in payload.groups)
    print(
        f"Wrote {total_links} links across {len(payload.groups)} groups to curated_links.json"
    )

    print("\n=== Link justifications ===")
    included_links = [link for group in llm_payload.groups for link in group.links]
    for link in sorted(included_links, key=lambda link: link.link_number):
        url = link_lookup.get(link.link_number, {}).get("url", "")
        print(f'[INCLUDED] #{link.link_number} ({url}) — "{link.justification}"')
    for excluded in sorted(
        llm_payload.excluded_links, key=lambda link: link.link_number
    ):
        url = link_lookup.get(excluded.link_number, {}).get("url", "")
        print(
            f'[EXCLUDED] #{excluded.link_number} ({url}) — "{excluded.justification}"'
        )


if __name__ == "__main__":
    main()
