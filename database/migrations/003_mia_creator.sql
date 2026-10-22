-- MIA Creator writing projects and bounded version history. Apply to the dedicated RZ Agent database.
CREATE TABLE IF NOT EXISTS mia_creator_projects (
    user_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    title TEXT NOT NULL CHECK (char_length(title) BETWEEN 1 AND 180),
    kind TEXT NOT NULL CHECK (kind IN ('book','story','article','essay','script','poem','business','research','journal','other')),
    description TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (user_id, project_id)
);
CREATE INDEX IF NOT EXISTS mia_creator_projects_user_updated_idx ON mia_creator_projects(user_id, updated_at DESC);
CREATE TABLE IF NOT EXISTS mia_creator_revisions (
    user_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    revision_id TEXT NOT NULL,
    content TEXT NOT NULL,
    note TEXT NOT NULL,
    created_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (user_id, project_id, revision_id),
    FOREIGN KEY (user_id, project_id) REFERENCES mia_creator_projects(user_id, project_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS mia_creator_revisions_project_created_idx ON mia_creator_revisions(user_id, project_id, created_at DESC);
