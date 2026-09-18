-- Rollback: Remove deprecated column from concepts table
-- Date: 2026-08-06
-- Purpose: Rollback migration 003_add_concepts_deprecated.sql
--
-- WARNING: This will forget all deprecation state; ghost concepts become active again.

ALTER TABLE concepts DROP COLUMN IF EXISTS deprecated;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'concepts' AND column_name = 'deprecated'
    ) THEN
        RAISE EXCEPTION 'deprecated column was not removed successfully';
    END IF;
    RAISE NOTICE 'Rollback completed successfully: deprecated column removed from concepts table';
END $$;
