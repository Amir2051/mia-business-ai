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
