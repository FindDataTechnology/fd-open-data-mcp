-- Rollback: multi-source observations (restore the pre-source unique key)
-- Date: 2026-09-18
-- Purpose: Rollback 007_multi_source_observations.sql. LOSSY BY DESIGN: where
-- several sources hold the same observation point, only the highest-ranked
-- source's row survives (source_rankings per concept; unranked sources sort
-- last, alphabetically for determinism). Removed rows are counted and reported
-- — check that NOTICE before running this in anger.

DO $$
DECLARE
    removed integer;
BEGIN
    SELECT count(*) INTO removed
    FROM (
        SELECT row_number() OVER (
                   PARTITION BY o.concept_id, o.entity_type, o.entity_id, o.date, o.granularity
                   ORDER BY COALESCE(r.quality, -1) DESC,
                            COALESCE(r.accessibility, -1) DESC,
                            o.source_used ASC,
                            o.id ASC
               ) AS rn
        FROM semantic_observations o
        LEFT JOIN source_rankings r
            ON r.concept_id = o.concept_id AND r.source = o.source_used
    ) d
    WHERE d.rn > 1;
    RAISE NOTICE 'Deduplicating: % multi-source rows will be removed (highest-ranked kept)', removed;

    DELETE FROM semantic_observations
    WHERE id IN (
        SELECT o.id
        FROM (
            SELECT o2.id,
                   row_number() OVER (
                       PARTITION BY o2.concept_id, o2.entity_type, o2.entity_id, o2.date, o2.granularity
                       ORDER BY COALESCE(r.quality, -1) DESC,
                                COALESCE(r.accessibility, -1) DESC,
                                o2.source_used ASC,
                                o2.id ASC
                   ) AS rn
            FROM semantic_observations o2
            LEFT JOIN source_rankings r
                ON r.concept_id = o2.concept_id AND r.source = o2.source_used
        ) o
        WHERE o.rn > 1
    );
END $$;

-- Restore the 5-column key (cannot fail: the dedupe left at most one row per
-- 5-column point). Same dual-form drop as 005/forward.
ALTER TABLE semantic_observations DROP CONSTRAINT IF EXISTS uq_sem_obs;
DROP INDEX IF EXISTS uq_sem_obs;
CREATE UNIQUE INDEX uq_sem_obs
    ON semantic_observations (concept_id, entity_type, entity_id, date, granularity);

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_indexes WHERE indexname = 'uq_sem_obs_src'
    ) THEN
        RAISE EXCEPTION 'uq_sem_obs_src still exists';
    END IF;
    RAISE NOTICE 'Rollback completed: source-aware key removed, rows = %',
        (SELECT count(*) FROM semantic_observations);
END $$;
