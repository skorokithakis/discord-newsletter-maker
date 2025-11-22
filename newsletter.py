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
- Group related links together and keep things concise. Feel free to put the
  links in whatever order makes the most sense.
- Each link is labeled with a number in the context: reference links by their
  number in your output as `link_number`.
- Populate the structured fields: title, description, link_number, and an optional
  group for related links.
- Do not include URLs or usernames in your output; we will attach them using the
  link number you provide.
- Use the supplied username for context (fall back to "Unknown" if missing).
- Keep descriptions factual and concise; do not invent details.

""".strip()


class LLMNewsletterLink(BaseModel):
    title: str
    description: str
    link_number: int
    group: str | None = None


class LLMNewsletterPayload(BaseModel):
    links: List[LLMNewsletterLink]


class NewsletterLink(BaseModel):
    title: str
    description: str
    url: str
    posted_by: str
    group: str | None = None


class NewsletterPayload(BaseModel):
    links: List[NewsletterLink]


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
                group=link.group,
            )
        )
    return NewsletterPayload(links=links)


def extract_json_content(content: str) -> str:
    """Pull the JSON object out of the model response (handles fenced blocks)."""
    text = content.strip()
    if "```" in text:
        for block in text.split("```"):
            candidate = block.strip()
            if candidate.startswith("{") and candidate.endswith("}"):
                return candidate
    return text


def render_newsletter(payload: NewsletterPayload) -> str:
    """Render the structured links into the final HTML snippet."""
    lines: List[str] = ['<ul class="link-list">']
    for link in payload.links:
        lines.extend(
            [
                "  <li>",
                f"    <strong>{link.title}</strong>",
                f'    <p>{link.description} — <span class="poster">{link.posted_by}</span></p>',
                f'    <a href="{link.url}">{link.url}</a>',
                "  </li>",
            ]
        )
    lines.append("</ul>")
    return "\n".join(lines)


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
    newsletter = render_newsletter(payload)

    output = json.dumps({"LINK_CONTENT": newsletter})
    Path("newsletter_context.json").write_text(output, encoding="utf-8")
    print(newsletter)


if __name__ == "__main__":
    main()
