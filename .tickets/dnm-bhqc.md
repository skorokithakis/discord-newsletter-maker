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
# Use Claude Code OAuth credentials in newsletter.py

Change newsletter.py so the Anthropic provider defaults to Claude Code OAuth credentials from ~/.claude/.credentials.json. Add OAUTH_HEADERS and CLAUDE_CODE_SYSTEM_PREFIX constants (copy from ,diplomacy). Add a make_anthropic_client() function that reads the OAuth token and returns an anthropic.Anthropic client with api_key=None, auth_token=token, and the OAuth headers. In run_completion, for the anthropic provider, use make_anthropic_client() instead of anthropic.Anthropic(api_key=api_key). Pass system=CLAUDE_CODE_SYSTEM_PREFIX to the messages.create call. In main(), remove the api_key requirement for anthropic: only error on missing key for openai. Remove the api_key parameter from run_completion for the anthropic path (it is no longer needed there). The --api-key flag and env var lookup can remain for the openai path. Pop ANTHROPIC_API_KEY from os.environ in make_anthropic_client() so it does not interfere, same as ,diplomacy does.

## Acceptance Criteria

Running with --provider anthropic and no --api-key or ANTHROPIC_API_KEY set should use OAuth credentials. OpenAI path unchanged.

