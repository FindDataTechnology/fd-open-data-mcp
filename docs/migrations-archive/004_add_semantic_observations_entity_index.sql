-- Migration: Add entity-first composite index on semantic_observations
-- Date: 2026-08-06
-- Purpose: fix-stock-semantic-retrieval (task 2.2).
--
-- read()/fetch() look up observations by (entity_id, concept_id, date).
-- The existing unique index uq_sem_obs leads with concept_id, so an
-- entity_id-first lookup on a concept with millions of rows (price.close:
-- 18.5M rows) scans a large slice before filtering to one entity. This
-- entity-first index lets the planner jump directly to an entity's rows,
-- and prevents multi-date read() from scanning unbounded ranges of the
-- 96M-row table (the "Connection closed" failure).

CREATE INDEX IF NOT EXISTS ix_semantic_observations_entity_concept_date
    ON semantic_observations (entity_id, concept_id, date);

-- Verify the index was created
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE tablename = 'semantic_observations'
          AND indexname = 'ix_semantic_observations_entity_concept_date'
    ) THEN
        RAISE EXCEPTION 'index ix_semantic_observations_entity_concept_date was not created successfully';
    END IF;
    RAISE NOTICE 'Migration completed successfully: entity-first composite index created on semantic_observations';
END $$;
