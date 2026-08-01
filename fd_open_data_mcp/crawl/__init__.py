"""Crawl planning: compile a wanted-concept spec into a ``CrawlPlan`` artifact.

The ``CrawlPlan`` is the decoupled contract between the planner (in fd-open-data-mcp)
and the generic executor (``scraw-fd-open-data-mcp``) - symmetric with
``DatasourceManifest`` as the harness->mcp contract. It is lazy (design.md D6): it
carries scope + filters, not a pre-expanded ``(entity x date)`` step list; the
executor expands at crawl time from ``fd-entities-indicators``.
"""
