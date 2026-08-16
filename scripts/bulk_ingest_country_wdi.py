"""Bulk-ingest World Bank WDI indicators into semantic_observations.

Companion to bulk_ingest_country_macro{,_g7}.py (China + G7 macro). This one
closes the World Bank WDI half of the country ontology: seeds all 217 real WB
economies as ``country`` entities and backfills the ~51 empty WDI-derived
concepts (40 ``WB_*`` + 11 generic/dotted: gdp/inflation/unemployment/…).

Source: World Bank Indicator API, called directly over HTTPS (no wbgapi pkg
needed; ``api.worldbank.org`` is reachable without a proxy).

  GET https://api.worldbank.org/v2/country/all/indicator/{WDI}?format=json
      -> [{page,pages,total}, [ {countryiso3code, date, value}, ... ]]

Aggregates (regions / income groups / "World") are filtered out client-side by
matching ``countryiso3code`` against the real-economy set from
``/v2/country?format=json`` (region.id != "NA"). The Euro area aggregate
``EMU`` is kept and mapped to the existing ``EU`` entity.

Date convention: WDI is annual -> granularity "year", date "YYYY-12-31".

Usage (scraw-fd-open-data-mcp venv; no akshare needed, stdlib only):
    /Users/chengsishi/finddata/scraw-fd-open-data-mcp/.venv/bin/python \
        scripts/bulk_ingest_country_wdi.py [--db-url URL] [--dry-run]
"""
from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import re
import sys
import time
import urllib.request

from sqlalchemy import create_engine, text
from psycopg2.extras import execute_values

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("wdi-ingest")

ENTITY_TYPE = "country"
SOURCE = "worldbank"
API = "https://api.worldbank.org/v2"

# Concept code -> WDI indicator code for the short-named concepts. The ``WB_*``
# concepts whose code already embeds the WDI code (``WB_NY.GDP.MKTP.CD``) are
# handled by stripping the ``WB_`` prefix. Generic/dotted concepts are mapped
# below by (code, measure).
SHORT_WB: dict[str, str] = {
    "WB_CO2": "EN.GHG.CO2.PC.CE.AR5",  # archived EN.ATM.CO2E.PC -> current AR5 per-capita
    "WB_EDUCATION_EXP": "SE.XPD.TOTL.GD.ZS",
    "WB_ELECTRICITY": "EG.USE.ELEC.KH.PC",
    "WB_EXPORTS": "NE.EXP.GNFS.ZS",
    "WB_FDI": "BX.KLT.DINV.WD.GD.ZS",
    "WB_FOREST": "AG.LND.FRST.ZS",
    "WB_GDP": "NY.GDP.MKTP.CD",
    "WB_GDP_GROWTH": "NY.GDP.MKTP.KD.ZG",
    "WB_GDP_PERCAPITA": "NY.GDP.PCAP.CD",
    "WB_GNI_PERCAPITA": "NY.GNP.PCAP.CD",
    "WB_HEALTH_EXP": "SH.XPD.CHEX.GD.ZS",
    "WB_IMPORTS": "NE.IMP.GNFS.ZS",
    "WB_INFLATION": "FP.CPI.TOTL.ZG",
    "WB_INTERNET": "IT.NET.USER.ZS",
    "WB_LABOR_FORCE": "SL.TLF.TOTL.IN",
    "WB_LIFE_EXPECTANCY": "SP.DYN.LE00.IN",
    "WB_LITERACY": "SE.ADT.LITR.ZS",
    "WB_POPULATION": "SP.POP.TOTL",
    "WB_POP_GROWTH": "SP.POP.GROW",
    "WB_UNEMPLOYMENT": "SL.UEM.TOTL.ZS",
}

# Generic (non-WB-prefixed) concept code -> WDI code. ``gdp`` has 3 concepts
# (nominal/per-capita/growth) distinguished by ``measure``; everything else is
# unique by code.
GENERIC: dict[str, str] = {
    "co2_emissions": "EN.GHG.CO2.MT.CE.AR5",  # archived EN.ATM.CO2E.KT -> current AR5 total
    "co2.per_capita": "EN.GHG.CO2.PC.CE.AR5",  # archived EN.ATM.CO2E.PC -> current AR5 per-capita
    "exports": "NE.EXP.GNFS.ZS",
    "imports": "NE.IMP.GNFS.ZS",
    "inflation": "FP.CPI.TOTL.ZG",
    "life_expectancy": "SP.DYN.LE00.IN",
    "population.total": "SP.POP.TOTL",
    "unemployment": "SL.UEM.TOTL.ZS",
}
GDP_BY_MEASURE: dict[str, str] = {
    "nominal_current": "NY.GDP.MKTP.CD",
    "per_capita": "NY.GDP.PCAP.CD",
    "growth": "NY.GDP.MKTP.KD.ZG",
}

_WDI_CODE_RE = re.compile(r"^WB_[A-Z]{2}\.")

# Archived WDI CO2 codes -> their current AR5 replacements, so the WB_-prefixed
# concept codes (e.g. ``WB_EN.ATM.CO2E.PC``) remap transparently too.
_ARCHIVED_REMAP: dict[str, str] = {
    "EN.ATM.CO2E.KT": "EN.GHG.CO2.MT.CE.AR5",
    "EN.ATM.CO2E.PC": "EN.GHG.CO2.PC.CE.AR5",
}


def _wdi_code(code: str, measure: str | None) -> str | None:
    """Map a country concept (code, measure) to its WDI indicator code."""
    if code in SHORT_WB:
        return SHORT_WB[code]
    if _WDI_CODE_RE.match(code):
        wdi = code[3:]  # strip "WB_"
        return _ARCHIVED_REMAP.get(wdi, wdi)
    if code == "gdp":
        return GDP_BY_MEASURE.get(measure or "")
    if code in GENERIC:
        return GENERIC[code]
    return None


def _get(url: str, timeout: int = 20, retries: int = 3) -> dict:
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "fd-open-data-mcp/0.1"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise last


def fetch_countries() -> dict[str, dict]:
    """Real WB economies (region != NA) keyed by ISO3 -> {iso2, name}."""
    data = _get(f"{API}/country?format=json&per_page=400")
    out: dict[str, dict] = {}
    for r in data[1]:
        if r.get("region", {}).get("id") == "NA":
            continue
        iso3, iso2, name = r["id"], r["iso2Code"], r["name"]
        if iso3 and iso2:
            out[iso3] = {"iso2": iso2, "name": name}
    return out


def _fmt(v) -> str | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    s = repr(f)
    if s.endswith(".0"):
        s = s[:-2]
    return s


def upsert(engine, rows: list[dict]) -> int:
    if not rows:
        return 0
    dedup: dict[tuple, dict] = {}
    for r in rows:
        key = (r["concept_id"], r["entity_type"], r["entity_id"], r["date"], r["granularity"])
        dedup[key] = r
    rows = list(dedup.values())
    cols = ["concept_id", "entity_type", "entity_id", "date", "value",
            "unit", "source_used", "fetched_at", "granularity"]
    upd = "value=EXCLUDED.value, unit=EXCLUDED.unit, source_used=EXCLUDED.source_used, fetched_at=EXCLUDED.fetched_at"
    sql = (f"INSERT INTO semantic_observations ({', '.join(cols)}) VALUES %s "
           f"ON CONFLICT (concept_id, entity_type, entity_id, date, granularity) "
           f"DO UPDATE SET {upd}")
    vals = [tuple(r[c] for c in cols) for r in rows]
    last = None
    for attempt in range(5):
        try:
            with engine.begin() as conn:
                cur = conn.connection.driver_connection.cursor()
                try:
                    execute_values(cur, sql, vals, page_size=500)
                finally:
                    cur.close()
            return len(vals)
        except Exception as e:  # noqa: BLE001
            last = e
            engine.dispose()
            time.sleep(2 * (attempt + 1))
    raise last


def seed_countries(engine, countries: dict[str, dict]) -> dict[str, int]:
    """Upsert all real economies (ISO2 code), return ISO3 -> entity_id."""
    with engine.begin() as conn:
        for iso3, meta in countries.items():
            conn.execute(text(
                "INSERT INTO entities (entity_type, code, name_en) "
                "VALUES (:et, :code, :name) "
                "ON CONFLICT (entity_type, code) DO UPDATE SET name_en=EXCLUDED.name_en"
            ), {"et": ENTITY_TYPE, "code": meta["iso2"], "name": meta["name"]})
    # EU (Euro area) is an aggregate, not a country; map EMU -> existing EU entity.
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO entities (entity_type, code, name_en) "
            "VALUES (:et, :code, :name) "
            "ON CONFLICT (entity_type, code) DO UPDATE SET name_en=EXCLUDED.name_en"
        ), {"et": ENTITY_TYPE, "code": "EU", "name": "European Union"})
    with engine.connect() as c:
        rows = c.execute(text(
            "SELECT code, id FROM entities WHERE entity_type=:et"
        ), {"et": ENTITY_TYPE}).all()
    code_to_id = {r[0]: r[1] for r in rows}
    eid: dict[str, int] = {}
    for iso3, meta in countries.items():
        cid = code_to_id.get(meta["iso2"])
        if cid is not None:
            eid[iso3] = cid
    if "EU" in code_to_id:
        eid["EMU"] = code_to_id["EU"]
    return eid


def load_concepts(engine) -> list[dict]:
    with engine.connect() as c:
        rows = c.execute(text(
            "SELECT id, code, measure, unit, frequency FROM concepts "
            "WHERE entity_type=:et AND frequency='yearly'"
        ), {"et": ENTITY_TYPE}).all()
    return [{"id": r[0], "code": r[1], "measure": r[2], "unit": r[3], "frequency": r[4]} for r in rows]


def build_concept_map(concepts: list[dict]) -> dict[str, list[dict]]:
    """WDI indicator code -> list of concepts that should hold it."""
    out: dict[str, list[dict]] = {}
    for c in concepts:
        wdi = _wdi_code(c["code"], c["measure"])
        if wdi:
            out.setdefault(wdi, []).append(c)
    return out


def ingest(engine, eid: dict[str, int], concept_map: dict[str, list[dict]]) -> tuple[int, int]:
    now = datetime.datetime.now(datetime.timezone.utc)
    total = failed = 0
    for wdi, concepts in sorted(concept_map.items()):
        try:
            data = _get(f"{API}/country/all/indicator/{wdi}?format=json&per_page=32500")
        except Exception as e:  # noqa: BLE001
            failed += 1
            log.info("%-22s FAILED: %s", wdi, e)
            continue
        # The API returns a 1-element error list (not the 2-element success list)
        # for deleted/archived indicators — skip instead of crashing on data[1].
        if not (isinstance(data, list) and len(data) >= 2 and isinstance(data[1], list)):
            failed += 1
            msg = "empty"
            if isinstance(data, list) and data and isinstance(data[0], dict):
                msg = data[0].get("message", [{}])[0].get("value", "empty")
            log.warning("%-22s SKIP (no data): %s", wdi, msg)
            continue
        rows: list[dict] = []
        for rec in data[1]:
            iso3 = rec.get("countryiso3code")
            ent = eid.get(iso3)
            if ent is None:
                continue
            year = rec.get("date")
            val = _fmt(rec.get("value"))
            if not year or val is None:
                continue
            d = f"{year}-12-31"
            for c in concepts:
                rows.append({
                    "concept_id": c["id"], "entity_type": ENTITY_TYPE, "entity_id": ent,
                    "date": d, "value": val, "unit": c["unit"],
                    "source_used": SOURCE, "fetched_at": now, "granularity": "year",
                })
        n = upsert(engine, rows)
        total += n
        log.info("%-22s %6d obs -> %d concepts", wdi, n, len(concepts))
        time.sleep(0.3)
    log.info("wdi: %d obs written, %d indicators failed", total, failed)
    return total, failed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-url",
                    default="postgresql+psycopg2://postgres:admin123@127.0.0.1:55432/postgres")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    # World Bank API is direct; ensure no proxy leaks in.
    os.environ.pop("http_proxy", None)
    os.environ.pop("https_proxy", None)
    os.environ.pop("HTTP_PROXY", None)
    os.environ.pop("HTTPS_PROXY", None)

    countries = fetch_countries()
    log.info("real WB economies: %d", len(countries))

    engine = create_engine(args.db_url)
    eid = seed_countries(engine, countries)
    log.info("entities mapped: %d (incl. EMU->EU)", len(eid))

    concepts = load_concepts(engine)
    concept_map = build_concept_map(concepts)
    log.info("concepts resolved: %d concepts over %d WDI indicators",
             sum(len(v) for v in concept_map.values()), len(concept_map))

    if args.dry_run:
        for wdi, cs in sorted(concept_map.items()):
            log.info("  %-22s -> %s", wdi, ", ".join(c["code"] for c in cs))
        return 0

    total, failed = ingest(engine, eid, concept_map)
    log.info("done: %d observations upserted (%d indicators failed)", total, failed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
