-- Rollback: Remove entity-first composite index on semantic_observations
-- Date: 2026-08-06
-- Purpose: Rollback migration 004_add_semantic_observations_entity_index.sql

DROP INDEX IF EXISTS ix_semantic_observations_entity_concept_date;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE tablename = 'semantic_observations'
          AND indexname = 'ix_semantic_observations_entity_concept_date'
    ) THEN
        RAISE EXCEPTION 'index was not removed successfully';
    END IF;
    RAISE NOTICE 'Rollback completed successfully: entity-first composite index removed from semantic_observations';
END $$;
