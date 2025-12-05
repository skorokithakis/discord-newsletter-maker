#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.13"
# dependencies = [
#   "css-inline",
#   "pydantic",
#   "requests",
# ]
# ///
"""Send a one-off Listmonk campaign from a local template."""

import argparse
import json
import os
import re
import time
from pathlib import Path
from typing import Any, List

import css_inline
import requests

from models import NewsletterPayload


def parse_args() -> argparse.Namespace:
    """Build and parse CLI arguments."""

    parser = argparse.ArgumentParser(
        description=(
            "Render a template with variables and create/start a Listmonk campaign."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("list_id", type=int, help="Target Listmonk list ID")
    parser.add_argument(
        "template",
        type=Path,
        help="Path to template HTML that contains placeholders like {{ TITLE_HERE }}",
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("LISTMONK_URL", "http://localhost:9000"),
        help="Base URL to Listmonk (no trailing slash)",
    )
    parser.add_argument(
        "--username",
        default=os.environ.get("LISTMONK_USERNAME"),
        help="Listmonk username (or set LISTMONK_USERNAME)",
    )
    parser.add_argument(
        "--password",
        default=os.environ.get("LISTMONK_PASSWORD"),
        help="Listmonk API key/password (or set LISTMONK_PASSWORD)",
    )
    parser.add_argument(
        "--subject",
        help="Email subject; defaults to a generic subject for the given list",
    )
    parser.add_argument(
        "--name",
        help="Campaign name; defaults to the subject if omitted",
    )
    parser.add_argument(
        "--template-id",
        type=int,
        default=1,
        help="Listmonk template ID to apply to the campaign",
    )
    parser.add_argument(
        "--from-email",
        default=os.environ.get("LISTMONK_FROM_EMAIL", "").strip(),
        help="Override From address for the campaign",
    )
    parser.add_argument(
        "--tag",
        action="append",
        default=["manual-send"],
        help="Tag(s) to attach to the campaign (repeatable)",
    )
    parser.add_argument(
        "--content-type",
        choices=["richtext", "html", "plain"],
        default="richtext",
        help="Content type passed to Listmonk",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="HTTP request timeout in seconds",
    )
    parser.add_argument(
        "--retry-delay",
        type=int,
        default=60,
        help="Delay between connection retries in seconds",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Create the campaign but do not start it",
    )
    parser.add_argument(
        "--show-body",
        action="store_true",
        help="Print the rendered body to stdout before sending",
    )
    return parser.parse_args()


BORDER_COLORS = ["#9B8AA5", "#E8847C", "#F5A962", "#5B9AA9"]


def load_curated_links() -> NewsletterPayload:
    """Load curated links from curated_links.json."""

    context_path = Path("curated_links.json")
    try:
        content = context_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise SystemExit(f"Curated links file not found: {context_path}")

    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON in {context_path}: {exc}") from exc

    return NewsletterPayload.model_validate(data)


def render_newsletter(payload: NewsletterPayload) -> str:
    """Render the curated links into an HTML snippet."""
    lines: List[str] = []
    border_counter = 0
    for group in payload.groups:
        if not group.links:
            continue
        lines.append(f'<h3 class="link-group-title">{group.title}</h3>')
        for link in group.links:
            border_color = BORDER_COLORS[border_counter % len(BORDER_COLORS)]
            border_counter += 1
            lines.extend(
                [
                    '<table role="presentation" class="link-card-table">',
                    "  <tr>",
                    f'    <td style="width: 4px; background-color: {border_color}; border-radius: 6px 0 0 6px;"></td>',
                    '    <td class="link-card-cell">',
                    f'      <strong class="link-card-title">{link.title}</strong>',
                    f'      <p class="link-card-description">{link.description} — <span class="link-card-poster">{link.posted_by}</span></p>',
                    f'      <a href="{link.url}" class="link-card-url">{link.url}</a>',
                    "    </td>",
                    "  </tr>",
                    "</table>",
                ]
            )
    return "\n".join(lines)


def render_template(template_path: Path, variables: dict[str, Any]) -> str:
    """Replace {{ key }} placeholders in the template with provided variables."""

    content = template_path.read_text(encoding="utf-8")

    for key, value in variables.items():
        pattern = re.compile(r"{{\s*" + re.escape(str(key)) + r"\s*}}")
        content = pattern.sub(str(value), content)

    return content


def create_campaign(
    url: str,
    username: str,
    password: str,
    list_id: int,
    body: str,
    *,
    subject: str,
    name: str,
    template_id: int,
    from_email: str,
    tags: list[str],
    content_type: str,
    timeout: int,
    retry_delay: int,
) -> int:
    """Create the Listmonk campaign and return its ID."""

    headers = {"Content-Type": "application/json;charset=utf-8"}
    json_data = {
        "name": name,
        "subject": subject,
        "lists": [list_id],
        "content_type": content_type,
        "body": body,
        "messenger": "email",
        "type": "regular",
        "tags": tags,
        "template_id": template_id,
    }

    if from_email:
        json_data["from_email"] = from_email

    while True:
        try:
            response = requests.post(
                f"{url}/api/campaigns",
                headers=headers,
                json=json_data,
                auth=(username, password),
                timeout=timeout,
            )
            break
        except requests.exceptions.ConnectionError:
            print(
                f"Failed to send campaign create request. Retrying in {retry_delay} seconds..."
            )
            time.sleep(retry_delay)

    if response.status_code != 200:
        raise SystemExit(
            f"Error creating campaign. Status: {response.status_code}, Response: {response.text}"
        )
    return response.json()["data"]["id"]


def start_campaign(
    url: str,
    username: str,
    password: str,
    campaign_id: int,
    *,
    timeout: int,
    retry_delay: int,
) -> int:
    """Start the created Listmonk campaign."""

    headers = {"Content-Type": "application/json"}
    json_data = {"status": "running"}

    while True:
        try:
            response = requests.put(
                f"{url}/api/campaigns/{campaign_id}/status",
                headers=headers,
                json=json_data,
                auth=(username, password),
                timeout=timeout,
            )
            break
        except requests.exceptions.ConnectionError:
            print(
                f"Failed to send campaign start request. Retrying in {retry_delay} seconds..."
            )
            time.sleep(retry_delay)

    return response.status_code


def main():
    args = parse_args()

    if not args.username or not args.password:
        raise SystemExit(
            "Username and password are required. Provide them via CLI or environment."
        )

    payload = load_curated_links()
    intro = (
        payload.intro.strip()
        or "Here's what we've been talking about on Discord lately:"
    )
    link_content = render_newsletter(payload)
    variables = {"LINK_CONTENT": link_content, "INTRO": intro}
    body = render_template(args.template, variables)
    body = css_inline.inline(body)

    if args.show_body:
        print(body)

    subject = args.subject or f"Manual campaign to list {args.list_id}"
    name = args.name or subject

    campaign_id = create_campaign(
        args.url,
        args.username,
        args.password,
        args.list_id,
        body,
        subject=subject,
        name=name,
        template_id=args.template_id,
        from_email=args.from_email,
        tags=args.tag,
        content_type=args.content_type,
        timeout=args.timeout,
        retry_delay=args.retry_delay,
    )
    print(f"Created campaign {campaign_id}")

    if args.dry_run:
        print("Dry run: campaign created but not started.")
        return

    status_code = start_campaign(
        args.url,
        args.username,
        args.password,
        campaign_id,
        timeout=args.timeout,
        retry_delay=args.retry_delay,
    )
    if status_code == 200:
        print("Campaign started successfully.")
    else:
        raise SystemExit(f"Failed to start campaign. Status: {status_code}")


if __name__ == "__main__":
    main()
