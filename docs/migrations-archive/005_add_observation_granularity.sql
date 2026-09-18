-- Migration: Add observation granularity column + granularity-aware unique key
-- Date: 2026-08-10
-- Purpose: fix-observation-time-granularity (D1/D2). Previously granularity was
-- encoded in the day value (yearly->12-31, monthly->01, daily->every day) and the
-- unique key (concept_id, entity_type, entity_id, date) made a monthly period and a
-- day inside it the SAME row, so ON CONFLICT DO NOTHING silently discarded one
-- cadence (first-writer-wins, no ranking logic). The granularity column makes the
-- key honest; monthly and daily observations of the same period are distinct rows.
--
-- Backfill heuristic (design D2): '-12-31' -> year, '-<mm>-01' -> month, else day.
-- The OLD key was unique on date, so no two rows share (concept, entity, date); the
-- backfill can therefore never create a duplicate under the NEW key.

ALTER TABLE semantic_observations
    ADD COLUMN IF NOT EXISTS granularity VARCHAR(8) NOT NULL DEFAULT 'day';

-- year: canonical yearly observation date is Dec-31
UPDATE semantic_observations SET granularity = 'year' WHERE date ~ '-12-31$';
-- month: canonical monthly observation date is the first of the month
UPDATE semantic_observations SET granularity = 'month' WHERE date ~ '-[0-9]{2}-01$';

-- Rebuild the unique key with granularity included (strict superset of the old
-- leading columns, so prefix scans using the old key ordering still work).
-- NOTE: deployed schemas created uq_sem_obs as a STANDALONE unique index (not a
-- table constraint), so DROP CONSTRAINT alone is a silent no-op there and
-- ADD CONSTRAINT would then fail on the name clash. Drop both forms
-- idempotently, then recreate as a standalone unique index (matches the
-- deployed style; ON CONFLICT column-inference works against either).
ALTER TABLE semantic_observations DROP CONSTRAINT IF EXISTS uq_sem_obs;
DROP INDEX IF EXISTS uq_sem_obs;
CREATE UNIQUE INDEX uq_sem_obs
    ON semantic_observations (concept_id, entity_type, entity_id, date, granularity);

-- Report: non-daily concepts whose monthly/yearly value may have been clobbered by
-- a daily crawl under the old key. The DB cannot tell which cadence won — these
-- rows are unrecoverable without a re-crawl (spec: collided rows are flagged, not
-- silently assumed correct).
DO $$
BEGIN
    RAISE NOTICE 'Re-crawl candidates (monthly/yearly concepts, may have lost a daily overwrite): %',
        (SELECT string_agg(code || ' (' || frequency || ')', ', ')
         FROM concepts WHERE frequency IN ('monthly', 'yearly', 'quarterly'));
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'semantic_observations' AND column_name = 'granularity'
    ) THEN
        RAISE EXCEPTION 'granularity column was not created';
    END IF;
    RAISE NOTICE 'Migration completed: granularity added, % rows tagged',
        (SELECT count(*) FROM semantic_observations);
END $$;
