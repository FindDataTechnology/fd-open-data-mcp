# Tasks

## 1. Measure
- [x] 1.1 Enumerate every (row declared class, entity class) mismatch against production: 351,740 rows in 7 groups, plus 11 dangling ids
- [x] 1.2 Identify the largest group: 333,975 OHLCV rows on 865 fund-manager `person` entities (eastmoney manager ids)
- [x] 1.3 Prove the write tap is closed: zero mis-attached rows with `fetched_at > 2026-11-06` (write-side fix `3225f4d`)
- [x] 1.4 Show a blanket "entity class == concept class" rule would hide 5,661 real multi-level rows

## 2. Guard
- [x] 2.1 `entities.resolver.check_entity_identity` + `SECURITY_ENTITY_TYPES`, memoised per session, dangling ids not gated
- [x] 2.2 Wire it into `read_cache`, `read_cache_all`, `read_cache_range` (the funnel for read/fetch/read_series/gap-fill)
- [x] 2.3 `hotel_series`: row class must equal its entity's class; security concepts additionally require a stock entity
- [x] 2.4 `hotel_kpi` RevPAR/ADR/occupancy branch requires a stock entity
- [x] 2.5 `analysis.time_series` adds the same `exists()` filter for security types

## 3. Verify
- [x] 3.1 Ten new tests (`tests/test_entity_class_reads.py`, `tests/test_hotel_tools.py`); 20 passed with the touched suites
- [x] 3.2 Reverse proof: empty `SECURITY_ENTITY_TYPES` turns exactly 6 of them red
- [x] 3.3 Full suite, file by file: 151 files, only environmental failures (4 model-download timeouts, NBS live site, production-DB smoke)
- [x] 3.4 Live: person 6588/6641 and industry 4354 refused; stock 4615 returns 21 points; Beijing (BJS, id 17) city-level star-rated returns 5 points again
- [x] 3.5 Same three checks re-run inside the shipped image (`sha-16c49f8`, `version: 0.5.52`)

## 4. Release
- [x] 4.1 0.5.52 (commits `65b65ae`, `335febb`, tag `v0.5.52`), PyPI
- [x] 4.2 scraw floor `>=0.5.52` (`16c49f8`) → image `sha-16c49f8` → relay OK
- [x] 4.3 Both cronjobs + `clusters.image` id 9 + both manifests (`d637a0e`); live tick on the new image
- [x] 4.4 Ledger §15.139 续二十七, correcting 续二十六
