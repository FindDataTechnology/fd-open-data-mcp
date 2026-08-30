"""Crawl-coverage expansion (expand-crawl-coverage).

Two halves:

- ``inventory`` — the read-only coverage gap inventory (per concept x
  entity_type: routable / ever_crawled / watermark / stale), shared by the
  ``coverage`` CLI, the ``coverage_report`` MCP tool, and the daily digest's
  coverage section.
- ``expander`` — the wave orchestrator that turns the gap set into backfill
  policies and drives them through the existing control plane
  (``launch_policy`` = the ``policy_trigger_now`` path).
"""
