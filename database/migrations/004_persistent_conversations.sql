-- Account-scoped conversation history for MIA-RZ Agent.
CREATE TABLE IF NOT EXISTS rz_agent_conversations (
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    title TEXT NOT NULL,
    history JSONB NOT NULL,
    model TEXT NOT NULL,
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (user_id, session_id)
);
CREATE INDEX IF NOT EXISTS rz_agent_conversations_user_updated_idx
    ON rz_agent_conversations (user_id, updated_at DESC);
