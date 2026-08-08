-- Rollback: Remove real_sources column from functions table
-- Date: 2026-08-04
-- Purpose: Rollback migration 001_add_real_sources_to_functions.sql
--
-- WARNING: This will permanently delete all real_sources data!

-- Remove the real_sources column
ALTER TABLE functions DROP COLUMN IF EXISTS real_sources;

-- Verify the column was removed
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'functions' AND column_name = 'real_sources'
    ) THEN
        RAISE EXCEPTION 'real_sources column was not removed successfully';
    END IF;
    RAISE NOTICE 'Rollback completed successfully: real_sources column removed from functions table';
END $$;
