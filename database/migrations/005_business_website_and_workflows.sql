-- Tables that were previously created only at runtime by the application.
-- Apply only to the dedicated mia-business-ai database, after 004_mia_business.sql.
-- All tables are owner-scoped by user_id; schema matches the SQLite dev fallback
-- in app/business_documents.py and app/website_intelligence.py.

-- Review-before-save website scan evidence (app/website_intelligence.py).
CREATE TABLE IF NOT EXISTS business_website_scans (
    user_id TEXT NOT NULL,
    scan_id TEXT NOT NULL,
    domain TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('review', 'approved')),
    pages_json TEXT NOT NULL,
    profile_json TEXT NOT NULL,
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (user_id, scan_id)
);
CREATE INDEX IF NOT EXISTS business_website_scans_user_updated_idx
    ON business_website_scans (user_id, updated_at DESC);

-- Client relationship activity log (app/business_documents.py).
CREATE TABLE IF NOT EXISTS business_client_activities (
    user_id TEXT NOT NULL,
    activity_id TEXT NOT NULL,
    client_id TEXT NOT NULL,
    activity_type TEXT NOT NULL,
    summary TEXT NOT NULL,
    next_step TEXT NOT NULL DEFAULT '',
    created_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (user_id, activity_id)
);
CREATE INDEX IF NOT EXISTS business_client_activities_user_client_idx
    ON business_client_activities (user_id, client_id, created_at DESC);

-- Approval-gated business workflows (app/business_documents.py).
CREATE TABLE IF NOT EXISTS business_workflows (
    user_id TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    title TEXT NOT NULL,
    goal TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    approval_status TEXT NOT NULL DEFAULT 'pending',
    steps_json TEXT NOT NULL,
    audit_json TEXT NOT NULL DEFAULT '[]',
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (user_id, workflow_id)
);
CREATE INDEX IF NOT EXISTS business_workflows_user_updated_idx
    ON business_workflows (user_id, updated_at DESC);
