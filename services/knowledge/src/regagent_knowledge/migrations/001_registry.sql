-- Реестр документов и задач индексации (dev_docs/03-knowledge-service.md, модуль registry).
CREATE TABLE IF NOT EXISTS documents (
    doc_id      text PRIMARY KEY,
    version     integer,
    sha256      text,
    level       text,
    areas       text[] NOT NULL DEFAULT '{}',
    title       text,
    status      text NOT NULL,
    error       text,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id      text PRIMARY KEY,
    op          text NOT NULL,
    doc_id      text NOT NULL,
    version     integer NOT NULL,
    sha256      text,
    path        text,
    status      text NOT NULL,
    attempts    integer NOT NULL DEFAULT 0,
    error       text,
    steps       jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS jobs_status_idx ON jobs (status);
CREATE INDEX IF NOT EXISTS jobs_doc_idx ON jobs (doc_id);
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS current_step text;
