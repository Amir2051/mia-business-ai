-- User-scoped persistent memory and bounded task tracking for RZ Agent.
CREATE TABLE IF NOT EXISTS rz_agent_memory (
    user_id TEXT NOT NULL,
    memory_id TEXT NOT NULL,
    content TEXT NOT NULL CHECK (char_length(content) BETWEEN 1 AND 2000),
    created_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (user_id, memory_id)
);
CREATE INDEX IF NOT EXISTS rz_agent_memory_user_created_idx
    ON rz_agent_memory (user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS rz_agent_tasks (
    user_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    title TEXT NOT NULL CHECK (char_length(title) BETWEEN 1 AND 300),
    status TEXT NOT NULL CHECK (status IN ('pending','in_progress','completed','blocked')),
    steps JSONB NOT NULL,
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (user_id, task_id)
);
CREATE INDEX IF NOT EXISTS rz_agent_tasks_user_updated_idx
    ON rz_agent_tasks (user_id, updated_at DESC);
