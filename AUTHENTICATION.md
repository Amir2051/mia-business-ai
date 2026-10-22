RZ Agent authentication setup

RZ Agent account authentication is separate from SafeNestT. Use a dedicated Neon project/database and do not reuse SafeNestT database credentials.

## What is implemented

- Email/password registration and login, with Argon2 password hashes.
- Random session tokens stored only as SHA-256 hashes in PostgreSQL.
- HttpOnly, SameSite=Lax cookies; production cookies use Secure.
- Database-backed session expiration and logout revocation.
- Protected chat and conversation-list/reset endpoints.
- Scoped, revocable API tokens for MCP and approved tool endpoints; only token hashes are stored, and a new token is shown once.
- User-prefixed conversation IDs to prevent one account from using another account's session IDs.
- Local mode remains compatible with the existing RZ_API_KEY flow when DATABASE_URL is unset.

## Configure the dedicated Neon database

1. Create or select a database dedicated to RZ Agent. Never use the SafeNestT database.
2. In that database's SQL editor, run database/migrations/001_auth.sql, then database/migrations/002_api_tokens.sql.
3. Copy its PostgreSQL connection string into Render's DATABASE_URL secret. Keep TLS/SSL enabled.
4. Set APP_ORIGIN to the exact public RZ Agent origin, for example https://your-rz-agent-service.onrender.com (replace with the actual service URL).
5. Set APP_ENV=production and configure OPENROUTER_API_KEY and OPENROUTER_MODEL in Render's environment settings.
6. Deploy only after reviewing the diff and verifying the database migration has succeeded.

Do not commit connection strings, provider keys, or account passwords.

## Endpoints

- GET /v1/auth/mode: reports account when DATABASE_URL is configured, otherwise api_key.
- POST /v1/auth/register: JSON with email and a password of at least 12 characters.
- POST /v1/auth/login: same JSON shape.
- GET /v1/auth/me: validates the session cookie.
- POST /v1/auth/logout: revokes the current session.
- POST /v1/auth/tokens: create a scoped API token while signed in. JSON accepts `name`, `scopes` (`mcp:read`, `mcp:call`), and optional `expires_in_days` (1–365, or `null` for no expiry). The plaintext token is returned once.
- GET /v1/auth/tokens: list token metadata without secrets.
- DELETE /v1/auth/tokens/{token_id}: revoke one of your tokens.
- POST /mcp: authenticated MCP JSON-RPC endpoint.
- GET /v1/tools and GET /v1/capabilities: discover approved tools/capabilities. Send `Authorization: Bearer rzat_...` for external clients.
- Approved capability endpoints (`/v1/research`, `/v1/memory`, `/v1/tasks`, `/v1/documents/analyze`, `/v1/code/review`, `/v1/images/generations`) accept `mcp:call` bearer tokens where the route performs an action; read-only list endpoints accept `mcp:read`. Browser account sessions remain supported.
- GET /health: health check.

## Current limitations before public launch

- The auth and API-token SQL migrations must be applied to the dedicated RZ Agent database before account registration/login or bearer-token authentication can work.
- There is no email verification, password-reset flow, MFA, account recovery, or admin account-management screen yet.
- Rate limits currently live in process memory. Add a shared rate-limit store or edge-level controls before relying on them at scale.
- Conversation history remains in process memory, so it is not durable across service restarts and is not yet stored in PostgreSQL.
- Run an authenticated browser test against the dedicated database before treating the deployment as production-ready.


## MCP bearer tokens

Create a token while signed in through the browser. Use the minimum required scopes: `mcp:read` for discovery and read-only lists, and `mcp:call` for MCP tool calls and approved capability actions. Store the token in the MCP client's secret/credential settings as `Authorization: Bearer <token>`. Tokens are stored only as SHA-256 hashes, can expire after 1–365 days (or be configured without expiry), and can be revoked with `DELETE /v1/auth/tokens/{token_id}`. Browser account authentication remains cookie-based; this does not create a public auth bypass.
