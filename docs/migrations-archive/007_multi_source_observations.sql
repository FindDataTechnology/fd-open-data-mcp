-- Migration: multi-source observations (add-multi-source-observations)
-- Date: 2026-09-18
-- Purpose: add-multi-source-observations (D1). The observation unique key gains
-- source_used, so WorldBank's China-2024 GDP and NBS's coexist as separate rows
-- instead of first-writer-wins. Reads pick the preferred source at query time
-- via source_rankings; values are never merged or overwritten across sources.
--
-- Data safety: the new key is a strict superset of the old one and source_used
-- is NOT NULL on every row, so no existing row can violate the new constraint —
-- the swap cannot fail on data and nothing is rewritten.
--
-- Run with psql (each statement autocommits; CREATE INDEX CONCURRENTLY cannot
-- run inside a transaction block). `fd-open-data-mcp migrate` performs the same
-- swap automatically (advisory-locked, re-entrant); this file is the manual
-- runbook equivalent.

-- 1) Build the new unique index online (no exclusive lock; writes continue).
--    A previous failed build can leave an INVALID index behind — drop it first.
DO $$
DECLARE
    invalid_exists boolean;
BEGIN
    SELECT EXISTS (
        SELECT 1 FROM pg_index i
        JOIN pg_class c ON c.oid = i.indexrelid
        WHERE c.relname = 'uq_sem_obs_src' AND NOT i.indisvalid
    ) INTO invalid_exists;
    IF invalid_exists THEN
        EXECUTE 'DROP INDEX CONCURRENTLY uq_sem_obs_src';
    END IF;
END $$;

CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_sem_obs_src
    ON semantic_observations (concept_id, entity_type, entity_id, date, granularity, source_used);

-- 2) Swap the constraint to the new index (dictionary-lock only, brief).
--    Deployed schemas may hold uq_sem_obs as a standalone index (005 style) or
--    as a table constraint — drop both forms idempotently first.
ALTER TABLE semantic_observations DROP CONSTRAINT IF EXISTS uq_sem_obs;
DROP INDEX IF EXISTS uq_sem_obs;
ALTER TABLE semantic_observations
    ADD CONSTRAINT uq_sem_obs UNIQUE USING INDEX uq_sem_obs_src;

-- 3) Ops note: the coordinator's dedup view (semantic_observations_read)
--    collapses rows per (concept, entity, date, granularity) and is defined
--    outside this repo. After this migration, update its definition to dedup
--    per (concept, entity, date, granularity, source_used) — otherwise
--    view-path reads see only one source per point.

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE indexname = 'uq_sem_obs_src'
    ) THEN
        RAISE EXCEPTION 'uq_sem_obs_src was not created';
    END IF;
    RAISE NOTICE 'Migration completed: % observation rows now under the source-aware key',
        (SELECT count(*) FROM semantic_observations);
    RAISE NOTICE 'REMINDER: update the semantic_observations_read view to dedup per source';
END $$;
