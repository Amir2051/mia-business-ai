RZ Agent authentication setup

RZ Agent account authentication is separate from SafeNestT. Use a dedicated Neon project/database and do not reuse SafeNestT database credentials.

## What is implemented

- Email/password registration and login, with Argon2 password hashes.
- Random session tokens stored only as SHA-256 hashes in PostgreSQL.
- HttpOnly, SameSite=Lax cookies; production cookies use Secure.
- Database-backed session expiration and logout revocation.
- Protected chat and conversation-list/reset endpoints.
- User-prefixed conversation IDs to prevent one account from using another account's session IDs.
- Local mode remains compatible with the existing RZ_API_KEY flow when DATABASE_URL is unset.

## Configure the dedicated Neon database

1. Create or select a database dedicated to RZ Agent. Never use the SafeNestT database.
2. In that database's SQL editor, run database/migrations/001_auth.sql.
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
- GET /health: health check.

## Current limitations before public launch

- The SQL migration must be applied manually before account registration/login can work.
- There is no email verification, password-reset flow, MFA, account recovery, or admin account-management screen yet.
- Rate limits currently live in process memory. Add a shared rate-limit store or edge-level controls before relying on them at scale.
- Conversation history remains in process memory, so it is not durable across service restarts and is not yet stored in PostgreSQL.
- Run an authenticated browser test against the dedicated database before treating the deployment as production-ready.
