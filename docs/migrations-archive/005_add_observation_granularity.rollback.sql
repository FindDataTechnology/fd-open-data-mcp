-- Rollback: Remove observation granularity (restore old date-only unique key)
-- Date: 2026-08-10
-- Purpose: Rollback 005_add_observation_granularity.sql (re-opens the
-- cross-cadence silent-loss path — do not roll back while monthly and daily
-- crawls of the same concept can coexist).

ALTER TABLE semantic_observations DROP CONSTRAINT IF EXISTS uq_sem_obs;
DROP INDEX IF EXISTS uq_sem_obs;
CREATE UNIQUE INDEX uq_sem_obs
    ON semantic_observations (concept_id, entity_type, entity_id, date);
ALTER TABLE semantic_observations DROP COLUMN IF EXISTS granularity;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'semantic_observations' AND column_name = 'granularity'
    ) THEN
        RAISE EXCEPTION 'granularity column was not removed';
    END IF;
    RAISE NOTICE 'Rollback completed: granularity removed, date-only unique key restored';
END $$;
