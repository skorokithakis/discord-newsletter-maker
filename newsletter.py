#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.13"
# dependencies = [
#   "openai",
# ]
# ///

from __future__ import annotations

import argparse
import os
import re
from pathlib import Path
from typing import List, Sequence

from openai import APIConnectionError, APIError, APITimeoutError, AuthenticationError
from openai import OpenAI

LINK_RE = re.compile(r"https?://\S+")

SYSTEM_PROMPT = """
You are a newsletter editor for the newsletter of a maker community called 'The
Makery'. Read chat excerpts that contain shared links and their descriptions.

- Decide which links are worth including (educational, insightful, noteworthy).
- Drop broken or spammy links.
- Group related links together and keep things concise. Feel free to put the
  links in whatever order makes the most sense.
- Return Markdown with a short title and bullets. Do mention a few words about
  each link, anything you can gather from its description and the messages in
  the context. Don't say things you aren't sure about, but do try to make it a
  bit less dry than just a link description.
- Give the links the following structure:
  - **Bold title with proper case**
    Description sentences.
    https://link/to/the/page

""".strip()


def extract_link_sections(text: str) -> str:
    """Keep only lines that mention links plus nearby context."""
    lines = text.splitlines()
    keep: List[str] = []
    for idx, line in enumerate(lines):
        if LINK_RE.search(line):
            window_start = max(0, idx - 2)
            window_end = min(len(lines), idx + 3)
            keep.extend(lines[window_start:window_end])
            keep.append("")  # spacer between link blocks
    return "\n".join(keep).strip()


def run_completion(
    client: OpenAI, model: str, context: str, temperature: float
) -> str:
    response = client.chat.completions.create(
        model=model,
        temperature=temperature,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "Create the newsletter from these Discord snippets:\n\n" + context
                ),
            },
        ],
    )
    choice = response.choices[0]
    return choice.message.content or ""


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Summarize Discord link dumps into a short newsletter."
    )
    parser.add_argument(
        "input",
        type=Path,
        help="Path to the text file containing the gathered messages with links.",
    )
    parser.add_argument(
        "--model",
        default="gpt-5.1",
        help="OpenAI chat model to use (default: gpt-4o-mini).",
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
        raise SystemExit("Missing OpenAI API key. Set OPENAI_API_KEY or pass --api-key.")

    raw_text = args.input.read_text(encoding="utf-8")
    context = extract_link_sections(raw_text) or raw_text

    client = OpenAI(api_key=api_key)

    try:
        newsletter = run_completion(
            client=client,
            model=args.model,
            context=context,
            temperature=args.temperature,
        ).strip()
    except (APIError, APIConnectionError, APITimeoutError, AuthenticationError) as exc:
        raise SystemExit(f"OpenAI API error: {exc}") from exc

    if not newsletter:
        raise SystemExit("Model returned an empty newsletter.")

    print(newsletter)


if __name__ == "__main__":
    main()
