---
id: dnm-bhqc
status: closed
deps: []
links: []
created: 2026-03-30T21:34:46Z
type: task
priority: 2
assignee: Stavros Korokithakis
---
# Make Anthropic client base URL and API key configurable in newsletter.py

Change newsletter.py so the Anthropic provider uses a standard API key + configurable base URL (no OAuth, no Claude Code credentials). Add a make_anthropic_client(api_key) helper that reads ANTHROPIC_API_URL from the environment (defaulting to https://api.anthropic.com) and returns an anthropic.Anthropic client with that base_url and the supplied api_key. In run_completion, for the anthropic provider, use make_anthropic_client(api_key) and pass system=SYSTEM_PROMPT directly. In main(), require an API key for both providers via --api-key or the provider's env var.

## Acceptance Criteria

Running with --provider anthropic requires ANTHROPIC_API_KEY (or --api-key). ANTHROPIC_API_URL overrides the default endpoint. OpenAI path unchanged.

