-- RZ Agent only. Apply to the dedicated RZ Agent Neon database after 001_auth.sql.
CREATE TABLE IF NOT EXISTS auth_api_tokens (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES auth_users(id) ON DELETE CASCADE,
    name text NOT NULL CHECK (length(trim(name)) BETWEEN 1 AND 80),
    token_hash text NOT NULL UNIQUE,
    scopes text[] NOT NULL CHECK (cardinality(scopes) > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    last_used_at timestamptz,
    expires_at timestamptz,
    revoked_at timestamptz
);
CREATE INDEX IF NOT EXISTS auth_api_tokens_user_id_idx ON auth_api_tokens(user_id);
CREATE INDEX IF NOT EXISTS auth_api_tokens_expiry_idx ON auth_api_tokens(expires_at) WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS auth_api_tokens_hash_idx ON auth_api_tokens(token_hash) WHERE revoked_at IS NULL;
