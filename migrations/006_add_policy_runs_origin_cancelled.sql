-- Migration: policy_runs gains ad-hoc origin + cancellation metadata
-- Date: 2026-09-08
-- Purpose: panel-ops-console. Ad-hoc one-off crawls run without a persistent
-- policy row, so policy_id becomes nullable and an explicit `origin` column
-- distinguishes policy runs from ad-hoc runs. Panel-driven cancellation adds
-- `cancelled_by` (actor identity; the terminal status itself is the existing
-- `status` column gaining the value 'cancelled' — no DDL for it).

ALTER TABLE policy_runs ALTER COLUMN policy_id DROP NOT NULL;

ALTER TABLE policy_runs
    ADD COLUMN IF NOT EXISTS origin VARCHAR(16) NOT NULL DEFAULT 'policy';

ALTER TABLE policy_runs
    ADD COLUMN IF NOT EXISTS cancelled_by VARCHAR(128);

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'policy_runs' AND column_name = 'origin'
    ) THEN
        RAISE NOTICE 'Migration completed: policy_id nullable, origin backfilled (% policy rows)',
            (SELECT count(*) FROM policy_runs WHERE origin = 'policy');
    ELSE
        RAISE EXCEPTION 'origin column was not created';
    END IF;
END $$;
