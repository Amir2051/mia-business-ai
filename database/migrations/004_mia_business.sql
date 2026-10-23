-- Dedicated MIA Business AI tables. Apply only to the mia-business-ai database.
CREATE TABLE IF NOT EXISTS business_files (
    user_id TEXT NOT NULL,
    file_id TEXT NOT NULL,
    title TEXT NOT NULL,
    format TEXT NOT NULL,
    content TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'document',
    created_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (user_id, file_id)
);
CREATE INDEX IF NOT EXISTS business_files_user_created_idx
    ON business_files (user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS business_profiles (
    user_id TEXT PRIMARY KEY,
    business_name TEXT NOT NULL DEFAULT '',
    industry TEXT NOT NULL DEFAULT '',
    target_customers TEXT NOT NULL DEFAULT '',
    offer TEXT NOT NULL DEFAULT '',
    goals TEXT NOT NULL DEFAULT '',
    brand_voice TEXT NOT NULL DEFAULT '',
    constraints TEXT NOT NULL DEFAULT '',
    updated_at DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS business_clients (
    user_id TEXT NOT NULL,
    client_id TEXT NOT NULL,
    name TEXT NOT NULL,
    company TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    stage TEXT NOT NULL,
    expected_value TEXT NOT NULL DEFAULT '0',
    next_follow_up TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (user_id, client_id)
);
CREATE INDEX IF NOT EXISTS business_clients_user_stage_idx
    ON business_clients (user_id, stage);

CREATE TABLE IF NOT EXISTS business_growth_plans (
    user_id TEXT NOT NULL,
    plan_date TEXT NOT NULL,
    primary_goal TEXT NOT NULL,
    hours_available INTEGER NOT NULL CHECK (hours_available BETWEEN 1 AND 16),
    success_metric TEXT NOT NULL DEFAULT '',
    plan_text TEXT NOT NULL,
    reflection TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'completed')),
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (user_id, plan_date)
);
CREATE INDEX IF NOT EXISTS business_growth_plans_user_date_idx
    ON business_growth_plans (user_id, plan_date DESC);
