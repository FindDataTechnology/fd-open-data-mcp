-- Rollback: policy_runs ad-hoc origin + cancellation metadata
-- Date: 2026-09-08
-- Purpose: Rollback 006_add_policy_runs_origin_cancelled.sql.
-- Refuses to run while ad-hoc (policy_id IS NULL) or cancelled rows exist —
-- dropping NOT NULL back is only safe when every row has a policy again.

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM policy_runs WHERE policy_id IS NULL) THEN
        RAISE EXCEPTION 'ad-hoc (policy_id IS NULL) rows exist; re-attribute or delete them before rollback';
    END IF;
    IF EXISTS (SELECT 1 FROM policy_runs WHERE status = 'cancelled') THEN
        RAISE EXCEPTION 'cancelled runs exist; close them under an allowed status before rollback';
    END IF;
END $$;

ALTER TABLE policy_runs DROP COLUMN IF EXISTS cancelled_by;
ALTER TABLE policy_runs DROP COLUMN IF EXISTS origin;
ALTER TABLE policy_runs ALTER COLUMN policy_id SET NOT NULL;
