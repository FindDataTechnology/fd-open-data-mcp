-- Migration: Add real_source column to fetch_log table
-- Date: 2026-08-04
-- Purpose: Track real data source (e.g., "eastmoney") separately from library name (e.g., "akshare")
--
-- This allows circuit breaker and ban tracking to operate at the real data source level,
-- enabling more precise failover when specific data sources are blocked.

-- Add the real_source column
ALTER TABLE fetch_log ADD COLUMN real_source VARCHAR(64);

-- Create index for efficient querying by real_source
CREATE INDEX ix_fetch_log_real_source ON fetch_log(real_source);

-- Verify the column was added
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'fetch_log' AND column_name = 'real_source'
    ) THEN
        RAISE EXCEPTION 'real_source column was not added successfully';
    END IF;
    RAISE NOTICE 'Migration completed successfully: real_source column added to fetch_log table';
END $$;
