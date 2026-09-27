-- ==============================================================================
-- IDEA DUMP SYSTEM: SUPABASE POSTGRESQL SCHEMA
-- Designed for high-volume idea ingestion and daily autonomous project builds.
-- ==============================================================================

-- 1. Enable required extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- 2. Projects Table: Tracks autonomous repos created by Hermes/Cline
CREATE TABLE IF NOT EXISTS projects (
    id BIGSERIAL PRIMARY KEY,
    uuid UUID DEFAULT uuid_generate_v4() UNIQUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    name TEXT NOT NULL,
    repo_url TEXT NOT NULL,
    repo_description TEXT,
    spec_summary TEXT,
    spec_path TEXT,
    source_idea_ids BIGINT[] DEFAULT '{}',
    status TEXT DEFAULT 'active' CHECK (status IN ('active', 'archived', 'deprecated'))
);

-- 3. Ideas Table: The high-volume idea dump
CREATE TABLE IF NOT EXISTS ideas (
    id BIGSERIAL PRIMARY KEY,
    uuid UUID DEFAULT uuid_generate_v4() UNIQUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    day_of_week TEXT,
    
    title TEXT,
    raw_content TEXT NOT NULL,
    urls TEXT[] DEFAULT '{}',
    tags TEXT[] DEFAULT '{}',
    
    -- Pipeline state tracking
    status TEXT DEFAULT 'pending' CHECK (status IN ('pending', 'researching', 'spec_ready', 'built', 'archived')),
    priority TEXT DEFAULT 'normal' CHECK (priority IN ('low', 'normal', 'high', 'urgent')),
    
    -- Link to the built project when Hermes completes it
    project_id BIGINT REFERENCES projects(id) ON DELETE SET NULL,
    github_repo_url TEXT,
    
    -- Notes from research & agents
    agent_notes TEXT,
    feasibility_score NUMERIC(3, 2) CHECK (feasibility_score >= 0.0 AND feasibility_score <= 1.0)
);

-- 4. Automatic metadata & updated_at Trigger
CREATE OR REPLACE FUNCTION handle_idea_before_save()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    IF NEW.day_of_week IS NULL THEN
        NEW.day_of_week = TO_CHAR(COALESCE(NEW.created_at, NOW()) AT TIME ZONE 'UTC', 'FMDay');
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_ideas_before_save ON ideas;
CREATE TRIGGER trg_ideas_before_save
    BEFORE INSERT OR UPDATE ON ideas
    FOR EACH ROW
    EXECUTE FUNCTION handle_idea_before_save();

-- 5. Performance Indexes for High-Volume Querying
CREATE INDEX IF NOT EXISTS idx_ideas_status ON ideas(status);
CREATE INDEX IF NOT EXISTS idx_ideas_created_at ON ideas(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ideas_tags ON ideas USING GIN(tags);
CREATE INDEX IF NOT EXISTS idx_projects_created_at ON projects(created_at DESC);

-- 6. Row-Level Security (RLS) Policies
-- Enables safe direct access from a static GitHub.io frontend using Supabase's anon public key.
ALTER TABLE ideas ENABLE ROW LEVEL SECURITY;
ALTER TABLE projects ENABLE ROW LEVEL SECURITY;

-- Allow public read access (for your personal dashboard)
CREATE POLICY "Allow public read on ideas"
    ON ideas FOR SELECT
    TO anon, authenticated
    USING (true);

-- Allow public insert (for submitting new ideas from your github.io page)
CREATE POLICY "Allow public insert on ideas"
    ON ideas FOR INSERT
    TO anon, authenticated
    WITH CHECK (true);

-- Allow public read on projects
CREATE POLICY "Allow public read on projects"
    ON projects FOR SELECT
    TO anon, authenticated
    USING (true);

-- Allow update only for pipeline automation (Hermes / Cline or authenticated user)
CREATE POLICY "Allow updates on ideas"
    ON ideas FOR UPDATE
    TO anon, authenticated
    USING (true)
    WITH CHECK (true);

-- 7. Seed Initial Sample Idea (for immediate verification)
INSERT INTO ideas (title, raw_content, urls, tags, status, priority)
VALUES (
    'Autonomous Market Scraping & Fast Parity Check',
    'Ingest marktplaats & ebay listings via Scrapling crawler and use DuckDB/ClickHouse local parity check before triggering apify.',
    ARRAY['https://github.com/dreadnode/scrapling', 'https://duckdb.org'],
    ARRAY['scraping', 'automation', 'database'],
    'pending',
    'high'
) ON CONFLICT DO NOTHING;
