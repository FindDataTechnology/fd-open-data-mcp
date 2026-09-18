# Attic: the never-applied revision chain (001–009)

These nine revisions were written between 2026-08-04 and 2026-08-30 but
**executed against no database**: no environment has ever had an
`alembic_version` table, and stamping or upgrading necessarily creates one.
The live schema reached its state through `Base.metadata.create_all()` plus
manual scripts, and all nine revisions' tables are modelled — so their
effects are already inside the baseline.

`alembic/versions/0001_schema_baseline.py` supersedes the chain. These files
are kept for archaeology (they document intent and ordering of the August
changes); alembic never loads them, because the attic sits outside
`versions/`.

Do not add files here. New schema changes get fresh revisions in
`alembic/versions/`, chained onto `0001_schema_baseline`.
