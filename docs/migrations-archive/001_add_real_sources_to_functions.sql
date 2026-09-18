-- Migration: Add real_sources column to functions table
-- Date: 2026-08-04
-- Purpose: Support real data source declaration for functions (e.g., eastmoney, tencent, sina)
--
-- This migration adds a JSONB column to store real data source information.
-- Each function can declare multiple real sources with priority for failover.
--
-- Example value:
-- [
--   {"name": "eastmoney", "priority": 0, "endpoint": null},
--   {"name": "tencent", "priority": 1, "endpoint": null}
-- ]

-- Add the real_sources column
ALTER TABLE functions ADD COLUMN real_sources JSONB;

-- Verify the column was added
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'functions' AND column_name = 'real_sources'
    ) THEN
        RAISE EXCEPTION 'real_sources column was not added successfully';
    END IF;
    RAISE NOTICE 'Migration completed successfully: real_sources column added to functions table';
END $$;
