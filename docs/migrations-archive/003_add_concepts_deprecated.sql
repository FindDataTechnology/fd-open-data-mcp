-- Migration: Add deprecated column to concepts table
-- Date: 2026-08-06
-- Purpose: Support concept canonicalization (fix-stock-semantic-retrieval).
--
-- A deprecated concept is retained for audit but excluded from discovery
-- (semantic_search/ai_search) and dispatch (read/fetch/rank_sources/plan_crawl).
-- Used to retire the 120 entity_type='symbol' stock-domain ghost concepts
-- (PRICE_CLOSE, PS_REVENUE, FIN_ROE, ...) that duplicate the canonical
-- entity_type='stock' concepts (price.close, financials.revenue, ...).

-- Add the deprecated column (defaults to false; existing concepts stay active)
ALTER TABLE concepts ADD COLUMN IF NOT EXISTS deprecated BOOLEAN NOT NULL DEFAULT false;

-- Verify the column was added
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'concepts' AND column_name = 'deprecated'
    ) THEN
        RAISE EXCEPTION 'deprecated column was not added successfully';
    END IF;
    RAISE NOTICE 'Migration completed successfully: deprecated column added to concepts table';
END $$;
