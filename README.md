# RZ AI

A small FastAPI service for a local personal AI assistant, with chat requests routed through OpenRouter.

## Setup

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and set `RZ_API_KEY` and `OPENROUTER_API_KEY`. Keep `.env` private; never commit it.

## Run locally

```bash
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8788
```

- Health: `http://127.0.0.1:8788/health`
- Interactive API docs: `http://127.0.0.1:8788/docs`
- Chat: `POST http://127.0.0.1:8788/v1/chat` with JSON `{"message":"Hello"}` and an `X-Api-Key` header matching `RZ_API_KEY`.

The chat request defaults to `openai/gpt-oss-20b` and sends OpenRouter provider preferences with Zero Data Retention enabled and provider fallbacks disabled. Provider-side data handling remains subject to OpenRouter's terms and the selected provider's policies.

The service binds to loopback in the example command so it is not exposed to the network by default.


## Optional task-aware model routing

The hosted default remains `OPENROUTER_MODEL`. You can optionally set
`OPENROUTER_CODING_MODEL` and/or `OPENROUTER_ANALYSIS_MODEL` to model IDs that
you have verified are eligible under your OpenRouter ZDR policy. When configured,
simple keyword-based routing selects those models for coding/debugging or
analysis/research requests. An explicit `model` in a chat request takes
precedence. If either optional variable is empty, requests in that category
continue to use the default model.

Every OpenRouter request continues to send `provider.zdr: true` and
`allow_fallbacks: false`; routing does not weaken those privacy controls.
Do not set either optional model until its available endpoint has been checked
against the account's provider allowlist and ZDR requirements.

When an OpenRouter key is configured, the app will not fall back to Hermes after
an OpenRouter error, because Hermes' endpoint has not been verified for this
ZDR requirement. It returns a clear failure instead. Hermes can be used as the
primary provider only when OpenRouter is not configured.


## Runtime capabilities and session cleanup

Authenticated clients can query `GET /v1/capabilities` to inspect the active provider,
default model, whether optional task-specific routes are configured, privacy request
flags, and session limits. This endpoint never returns API keys, prompts, or chat content.

Idle in-memory conversations are removed after `SESSION_TTL_SECONDS` (default: one hour)
when sessions are accessed or listed. This is memory cleanup, not durable storage:
conversation history still does not survive a process restart.


## Model Context Protocol (MCP) tools

RZ Agent exposes an authenticated MCP JSON-RPC endpoint at `POST /mcp` using the
Streamable HTTP request shape. It reuses the existing RZ Agent authentication: send
`X-API-Key: <RZ_API_KEY>` in local API-key mode, or use the signed-in account session
when running in account-auth mode. Do not put API keys in tool arguments or commit them
to client configuration files.

Available tools:

- `get_runtime_status`: safe provider/model routing and privacy-flag summary; no secrets or chat content.
- `list_conversations`: active conversation IDs for the authenticated user only.
- `chat`: send a prompt through the existing configured model and provider policy.
- `reset_conversation`: delete one of the authenticated user's active in-memory conversations.

MCP does not add web search, filesystem access, shell execution, or external app integrations.
Those should be added individually with explicit permission boundaries. The existing OpenRouter
Zero Data Retention request and disabled provider fallbacks remain unchanged.

MCP initialize example (replace the key locally; never paste a real key into shared logs):

```bash
curl -sS http://127.0.0.1:8788/mcp \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: YOUR_RZ_API_KEY' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"mcp-client","version":"1.0"}}}'
```

## Read-only GitHub MCP tools

The authenticated `/mcp` endpoint also exposes four GitHub read-only tools, restricted to repositories owned by `Amir2051`:

- `github_get_repository` — repository metadata.
- `github_list_issues` — issue summaries, excluding pull requests.
- `github_list_pull_requests` — pull-request summaries.
- `github_get_file_contents` — UTF-8 text file content (maximum 30,000 characters).

Public repositories can be read without a GitHub token. To read private repositories, configure `GITHUB_TOKEN` as a server-side environment secret with read-only repository/content permissions. Never pass the token in MCP arguments or commit it. The integration does not expose GitHub write operations, arbitrary owners, or shell access. Issue/PR list calls are limited to 20 results per request.

Example MCP tool call after authentication:

```json
{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"github_get_repository","arguments":{"repo":"rz-ai-agent"}}}
```
