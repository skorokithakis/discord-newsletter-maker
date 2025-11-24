#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.13"
# dependencies = [
#   "openai",
#   "pydantic",
# ]
# ///
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import List, Sequence

from pydantic import BaseModel, ValidationError

from models import NewsletterLink, NewsletterPayload
from openai import APIConnectionError
from openai import APIError
from openai import APITimeoutError
from openai import AuthenticationError
from openai import OpenAI

SYSTEM_PROMPT = """
You are a newsletter editor for the newsletter of a maker community called 'The
Makery'. Read chat excerpts that contain shared links and their descriptions.

- Decide which links are worth including (educational, insightful, noteworthy).
- Drop broken or spammy links.
- Drop links that are just us talking to each other or updating each other. That
  includes anything that would not really be of interest to a casual newsletter
  recipient.
- Keep things concise. Feel free to put the links in whatever order makes the
  most sense.
- Each link is labeled with a number in the context: reference links by their
  number in your output as `link_number`.
- Links that are similar, or talk about the same or similar things, should be
  ordered next to each other. Order the links to maximize reader interest and
  relevance.
- Populate the structured fields: title, description, and link_number.
- Do not include URLs or usernames in your output; we will attach them using the
  link number you provide.
- Use the supplied username for context (fall back to "Unknown" if missing).
- Keep descriptions factual and concise; do not invent details.

""".strip()


class LLMNewsletterLink(BaseModel):
    title: str
    description: str
    link_number: int


class LLMNewsletterPayload(BaseModel):
    links: List[LLMNewsletterLink]


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
    lines: List[str] = []
    link_lookup: dict[int, dict[str, str]] = {}
    link_counter = 1
    for context in contexts:
        source = context.get("source") or "unknown file"
        timestamp = context.get("timestamp") or "unknown time"
        lines.append(f"=== {source} @ {timestamp} ===")

        for message in context.get("messages") or []:
            author = message.get("author") or "Unknown"
            content = message.get("content") or ""
            message_lines = content.splitlines() or [""]
            lines.append(f"{author}: {message_lines[0]}")
            for line in message_lines[1:]:
                lines.append(f"    {line}")

        for link in context.get("links") or []:
            url = link.get("url") or ""
            posted_by = link.get("posted_by") or "Unknown"
            if url:
                lines.append(f"    [link #{link_counter}] {url} (posted by {posted_by})")
                link_lookup[link_counter] = {"url": url, "posted_by": posted_by}
                link_counter += 1
            description = link.get("description") or ""
            if description:
                lines.append(f"    [description] {description}")

        lines.append("")

    return "\n".join(lines).strip(), link_lookup


def run_completion(
    client: OpenAI, model: str, context: str, temperature: float
) -> LLMNewsletterPayload:
    response = client.chat.completions.parse(
        model=model,
        temperature=temperature,
        response_format=LLMNewsletterPayload,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "Create the newsletter from these Discord snippets. Links are labeled "
                    "with [link #N]; refer to them by number in your output.\n\n" + context
                ),
            },
        ],
    )
    choice = response.choices[0]
    parsed = getattr(choice.message, "parsed", None)
    if parsed is None:
        raise SystemExit("Model did not return parsed content.")
    if isinstance(parsed, LLMNewsletterPayload):
        return parsed
    # Defensive fallback if the SDK returns a dict.
    try:
        return LLMNewsletterPayload.model_validate(parsed)
    except ValidationError as exc:
        raise SystemExit(f"Model output failed validation: {exc}") from exc


def attach_link_metadata(
    llm_payload: LLMNewsletterPayload, link_lookup: dict[int, dict[str, str]]
) -> NewsletterPayload:
    """Replace link numbers from the model with the source URL/user details."""
    links: List[NewsletterLink] = []
    for link in llm_payload.links:
        source = link_lookup.get(link.link_number)
        if source is None:
            raise SystemExit(f"Model referenced unknown link number: {link.link_number}")
        links.append(
            NewsletterLink(
                title=link.title,
                description=link.description,
                url=source.get("url") or "",
                posted_by=source.get("posted_by") or "Unknown",
            )
        )
    return NewsletterPayload(links=links)


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
        default="gpt-5.1",
        help="OpenAI chat model to use (default: gpt-5.1).",
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
        help="OpenAI API key (defaults to OPENAI_API_KEY env var).",
    )
    args = parser.parse_args(argv)

    api_key = args.api_key or os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit(
            "Missing OpenAI API key. Set OPENAI_API_KEY or pass --api-key."
        )

    contexts = load_contexts(args.input)
    if not contexts:
        raise SystemExit("No link contexts found in input JSON.")
    context, link_lookup = render_contexts(contexts)

    client = OpenAI(api_key=api_key)

    try:
        llm_payload = run_completion(
            client=client,
            model=args.model,
            context=context,
            temperature=args.temperature,
        )
    except (APIError, APIConnectionError, APITimeoutError, AuthenticationError) as exc:
        raise SystemExit(f"OpenAI API error: {exc}") from exc

    payload = attach_link_metadata(llm_payload, link_lookup)

    output = payload.model_dump_json(indent=2)
    Path("curated_links.json").write_text(output, encoding="utf-8")
    print(f"Wrote {len(payload.links)} links to curated_links.json")


if __name__ == "__main__":
    main()
