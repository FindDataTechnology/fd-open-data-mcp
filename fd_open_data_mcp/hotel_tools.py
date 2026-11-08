"""Hotel-vertical read tools (add-hotel-vertical, task 5.5).

Five read-only entry points over the hotel universe and the lanes that already
feed the observation store / crawl event plane:

* ``hotel_universe``        — the 33-company universe with per-company lane
                              coverage and explicit unavailability
* ``hotel_series``          — one curated series (star-rated quarterly, tourism
                              demand, EU accommodation) with source_used/as_of
* ``hotel_kpi``             — RevPAR / ADR / occupancy readings with caliber,
                              period count and ``level_only`` declaration
* ``hotel_company_events``  — HKEX title events / SEC EDGAR event face
* ``hotel_benchmark``       — industry benchmark: **always** an explicit
                              unavailable block (STR/CoStar is paid, not wired)

Read-only discipline: every tool opens a session, runs SELECTs, closes. No tool
writes a row, warms a cache, or dispatches upstream.

Unavailability is data, never an empty list:

1. **STR / CoStar industry benchmark** — paid, not connected. ``hotel_benchmark``
   exists precisely so callers get that sentence instead of a zero.
2. **European lanes (Accor / Whitbread, Euronext/LSE)** — no lane registered;
   their numbers are unavailable and must not be substituted with peers.
3. **XBRL segment / geography dimensions** — ``companyfacts`` returns
   dimension-less facts only; segmented RevPAR cannot be derived from it.
4. **Japanese monthly operating data** — scattered across each company's IR
   site; not wired in phase one.
"""
from __future__ import annotations

from typing import Any

from fastmcp import FastMCP
from sqlalchemy import text

from fd_open_data_mcp.annotations import annotations_for
from fd_open_data_mcp.db import get_database

UNIVERSE_SOURCE = "hotel-universe"

# ─── reasons (verbatim from the change's unavailable-data contract) ─────────
R_STR = "行业基准不可得：STR / CoStar 为付费数据，未接入"
R_EU_LANE = "无车道·不可得：欧股（泛欧/伦交所）未接财务与经营车道"
R_XBRL_SEGMENT = "XBRL 分部与地区维度不可得：companyfacts 只返回无维度事实"
R_JP_MONTHLY = "日本月次经营数据不可得：载体分散于各社 IR，一期不做"
R_TWSE = "台股仅月营收快照（当期、无历史），利润表与披露事件不可得"
R_HK_PDF = "仅在公告 PDF 原文，未结构化"
R_A_ASHARE_STOPPED = ("A 股经营数据简报不可得：仅锦江 2023 年两份简报属可得历史，"
                      "2024 年起停发且其余公司从未单独披露")
R_OTA_NO_KPI = "OTA 平台无酒店经营 KPI（RevPAR/出租率/ADR 不适用）"

#: region → 该 region 的不可得原因（无车道/停发/载体限制）
REGION_UNAVAILABLE = {
    "EU": ("no-lane", R_EU_LANE),
    "TW": ("snapshot-only", R_TWSE),
    "JP": ("no-lane", R_JP_MONTHLY),
}
SEGMENT_UNAVAILABLE = {
    "OTA-platform": ("not-applicable", R_OTA_NO_KPI),
}

#: 星级饭店口径的定谳（板面与工具面同源，逐字）
CALIBER_STAR_ONLY = (
    "星级旅游饭店口径（文旅部）：与全行业酒店（中国饭店协会 37.5 万家）不是同一宇宙，"
    "不得据此外推全行业景气"
)
CALIBER_50CITY = "50 城表的「全国」行是 50 城小计（2,209 家），不是全国（6,129 家）"
CALIBER_DISCONTINUED = "该系列最新公开发布期为 2024Q3，其后无新期（历史基线，非当期景气）"


def _session():
    return get_database().get_session()


def _unavailable(reason: str, detail: str | None = None) -> dict:
    out: dict[str, Any] = {"status": "unavailable", "reason": reason}
    if detail:
        out["detail"] = detail
    return out


def _session_failure(exc: Exception) -> dict:
    return {
        "status": "error",
        "error": exc.__class__.__name__,
        "detail": f"数据面读取失败：{exc}",
        "note": "这是读取失败，不是「无观测」；不要把本结果当成真实缺测",
    }


def _today() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def register_hotel_tools(mcp: FastMCP) -> None:
    """Attach the hotel read tools to the given FastMCP instance."""

    @mcp.tool(annotations=annotations_for("hotel_universe"))
    def hotel_universe(
        region: str | None = None,
        segment: str | None = None,
    ) -> dict:
        """List the 33-company hotel universe with per-company lane coverage.

        Read-only. Each company carries its region, segment, ticker and the
        lanes that actually feed it; a company whose market has no lane (EU) or
        whose carrier is limited (TW snapshot-only, JP no lane) carries an
        explicit ``unavailable`` entry with the reason — never an empty list.

        Args:
            region: Optional filter (CN / HK / US / TW / JP / EU).
            segment: Optional filter (luxury-upscale / midscale / economy-budget /
                serviced-apartment / OTA-platform).
        """
        s = None
        try:
            s = _session()
            rows = s.execute(
                text(
                    """
                    SELECT code, name_zh, name_en, entity_type, metadata_json
                    FROM entities
                    WHERE metadata_json->>'registry' = 'hotel-universe'
                    ORDER BY code
                    """
                )
            ).mappings().all()
            companies: list[dict] = []
            for row in rows:
                meta = row["metadata_json"] or {}
                if isinstance(meta, str):
                    import json as _json

                    meta = _json.loads(meta)
                item = {
                    "code": row["code"],
                    "name_zh": row["name_zh"],
                    "name_en": row["name_en"],
                    "region": meta.get("region"),
                    "segment": meta.get("segment"),
                    "lanes": meta.get("lanes") or [],
                    "verified": meta.get("verified"),
                }
                if region and item["region"] != region:
                    continue
                if segment and item["segment"] != segment:
                    continue
                gaps = []
                reg_gap = REGION_UNAVAILABLE.get(str(item["region"]))
                if reg_gap:
                    gaps.append(_unavailable(reg_gap[1], f"region={item['region']}"))
                seg_gap = SEGMENT_UNAVAILABLE.get(str(item["segment"]))
                if seg_gap:
                    gaps.append(_unavailable(seg_gap[1], f"segment={item['segment']}"))
                if not item["lanes"] and not gaps:
                    gaps.append(_unavailable(R_EU_LANE, "lanes 为空"))
                if gaps:
                    item["unavailable"] = gaps
                companies.append(item)
            out: dict[str, Any] = {
                "source_used": UNIVERSE_SOURCE,
                "count": len(companies),
                "companies": companies,
                "as_of": _today(),
                "notes": [
                    "宇宙注册表口径：33 家（A股 6 / 港股 5 / 美股 10 / 台股 4 / 日股 2 / 欧股 2 + 平台侧 4）",
                    R_XBRL_SEGMENT,
                ],
            }
            if not companies:
                out["unavailable"] = [_unavailable(
                    "宇宙为空", f"region={region!r} segment={segment!r} 命中 0 家")]
            return out
        except Exception as exc:  # noqa: BLE001
            return _session_failure(exc)
        finally:
            if s is not None:
                s.close()

    @mcp.tool(annotations=annotations_for("hotel_series"))
    def hotel_series(
        code: str,
        source_used: str | None = None,
        entity_id: int | None = None,
    ) -> dict:
        """Read one hotel series by concept code (read-only).

        Covers the star-rated quarterly survey (``hotel.cn.star_rated.*``),
        domestic tourism demand (``hotel.cn.tourism.*``) and EU accommodation
        (``hotel.eu.*``). Every point carries ``source_used``; the payload
        carries ``as_of``, the caliber caveats and the period count. A concept
        with no rows returns an explicit ``unavailable`` block, never an empty
        list pretending to be a coverage gap.

        Args:
            code: Concept code, e.g. ``hotel.cn.star_rated.occupancy.all.quarterly``.
            source_used: Optional source pin (``mct-star-hotel`` /
                ``mct-tourism-demand`` / ``eurostat-api``).
            entity_id: Optional entity id (star-rated series exist per province,
                city and the country row simultaneously — pass it to disambiguate;
                without it the country row is not silently assumed).
        """
        s = None
        try:
            s = _session()
            concept = s.execute(
                text(
                    """
                    SELECT id, code, name_zh, name_en, unit, frequency, entity_type,
                           category
                    FROM concepts WHERE code = :code ORDER BY id LIMIT 1
                    """
                ),
                {"code": code.strip()},
            ).mappings().first()
            if concept is None:
                return {"code": code, "unavailable": [_unavailable(
                    "该概念不存在", f"concepts.code={code!r} 未在册")]}
            params: dict[str, Any] = {"cid": concept["id"]}
            # A series belongs to entities of the concept's own type. Without this
            # the read mixes classes: rows typed 'stock' sit on some industry
            # entity ids (8,500 of them, measured 2026-11-08), so an industry id
            # could surface a company's price/financials.
            where = ["o.concept_id = :cid", "o.entity_type = :cet"]
            params["cet"] = concept["entity_type"]
            if source_used:
                where.append("o.source_used = :src")
                params["src"] = source_used
            if entity_id is not None:
                where.append("o.entity_id = :eid")
                params["eid"] = entity_id
            rows = s.execute(
                text(
                    "SELECT o.date, o.value::float8 AS value, o.unit,"
                    " o.entity_type, o.entity_id, o.granularity, o.source_used,"
                    " o.fetched_at"
                    " FROM semantic_observations o"
                    f" WHERE {' AND '.join(where)} ORDER BY o.date"
                ),
                params,
            ).mappings().all()
            if not rows:
                return {
                    "code": code,
                    "concept": dict(concept),
                    "unavailable": [_unavailable(
                        "无观测",
                        f"source_used={source_used!r} entity_id={entity_id!r} 命中 0 行；"
                        "这是覆盖事实，不是零值")],
                }
            points = [
                {"date": r["date"], "value": r["value"], "unit": r["unit"],
                 "granularity": r["granularity"], "source_used": r["source_used"]}
                for r in rows
            ]
            caveats = []
            if str(concept["code"]).startswith("hotel.cn.star_rated"):
                caveats.extend([CALIBER_STAR_ONLY, CALIBER_50CITY, CALIBER_DISCONTINUED])
            return {
                "code": concept["code"],
                "concept": dict(concept),
                "sources": sorted({r["source_used"] for r in rows if r["source_used"]}),
                "count": len(points),
                "start": points[0]["date"],
                "end": points[-1]["date"],
                "as_of": points[-1]["date"],
                "points": points,
                "caveats": caveats,
                "notes": [
                    "期数 <24 的序列只作水平对照，不得画趋势线",
                    "分省/按星级/城市/全国是不同实体；未传 entity_id 时本返回可能混合多实体",
                ],
            }
        except Exception as exc:  # noqa: BLE001
            return _session_failure(exc)
        finally:
            if s is not None:
                s.close()

    @mcp.tool(annotations=annotations_for("hotel_kpi"))
    def hotel_kpi(
        company_code: str,
        metric: str | None = None,
    ) -> dict:
        """Read one company's RevPAR / ADR / occupancy readings (read-only).

        Returns every stored point for the company with ``source_used``,
        ``granularity``, ``as_of`` and the caliber note. Companies whose market
        has no lane (EU) or whose carrier is limited return an explicit
        ``unavailable`` block naming the reason — never another company's
        numbers.

        Args:
            company_code: Universe code, e.g. ``MAR`` or ``00045.HK``.
            metric: Optional filter — ``revpar`` / ``adr`` / ``occupancy``.
        """
        s = None
        try:
            s = _session()
            entity = s.execute(
                text(
                    "SELECT id, code, name_zh, metadata_json FROM entities"
                    " WHERE metadata_json->>'registry' = 'hotel-universe'"
                    " AND code = :code LIMIT 1"
                ),
                {"code": company_code.strip()},
            ).mappings().first()
            if entity is None:
                return {"company": company_code, "unavailable": [_unavailable(
                    "该实体不在酒店宇宙", f"code={company_code!r} 未在册")]}
            meta = entity["metadata_json"] or {}
            if isinstance(meta, str):
                import json as _json

                meta = _json.loads(meta)
            reg_gap = REGION_UNAVAILABLE.get(str(meta.get("region")))
            if reg_gap:
                return {
                    "company": entity["code"],
                    "name_zh": entity["name_zh"],
                    "unavailable": [_unavailable(reg_gap[1], f"region={meta.get('region')}")],
                    "alternatives": ["该市场无车道；可查阅发行人有价证券公告原文，本面不代取"],
                }
            if str(meta.get("segment")) == "OTA-platform":
                return {
                    "company": entity["code"],
                    "name_zh": entity["name_zh"],
                    "unavailable": [_unavailable(R_OTA_NO_KPI, "segment=OTA-platform")],
                }
            rows = s.execute(
                text(
                    """
                    SELECT c.code AS concept, c.name_zh, c.unit, o.date,
                           o.value::float8 AS value, o.granularity, o.source_used,
                           o.entity_type
                    FROM semantic_observations o
                    JOIN concepts c ON c.id = o.concept_id
                    WHERE o.entity_id = :eid AND o.entity_type = 'stock'
                      AND (c.code LIKE '%revpar%' OR c.code LIKE '%adr%'
                           OR c.code LIKE '%occupancy%' OR c.code LIKE '%.occ.%')
                    ORDER BY c.code, o.date
                    """
                ),
                {"eid": entity["id"]},
            ).mappings().all()
            if metric:
                rows = [r for r in rows if metric.lower() in str(r["concept"]).lower()]
            if not rows:
                return {
                    "company": entity["code"],
                    "name_zh": entity["name_zh"],
                    "unavailable": [_unavailable(
                        "该家无经营 KPI 观测",
                        R_A_ASHARE_STOPPED + "；港股仅 00045 有独立季度营运数据公告；"
                        "美股市值 5 家有 8-K exhibit")],
                }
            series: dict[str, list[dict]] = {}
            for r in rows:
                series.setdefault(str(r["concept"]), []).append({
                    "date": r["date"], "value": r["value"], "unit": r["unit"],
                    "source_used": r["source_used"], "granularity": r["granularity"],
                })
            periods = {k: len(v) for k, v in series.items()}
            return {
                "company": entity["code"],
                "name_zh": entity["name_zh"],
                "region": meta.get("region"),
                "segment": meta.get("segment"),
                "series": series,
                "periods": periods,
                "as_of": max(p["date"] for v in series.values() for p in v),
                "caliber": [
                    "逐家口径不同（可比口径/固定汇率/as-reported），跨公司不可直接比绝对值",
                    "期数 <12 只作水平对照（level_only），不得画趋势",
                    R_XBRL_SEGMENT,
                ],
            }
        except Exception as exc:  # noqa: BLE001
            return _session_failure(exc)
        finally:
            if s is not None:
                s.close()

    @mcp.tool(annotations=annotations_for("hotel_company_events"))
    def hotel_company_events(company_code: str, limit: int = 50) -> dict:
        """Read one company's disclosure events (read-only, event plane only).

        HKEX title events and SEC EDGAR filing events. No headline numbers are
        extracted from announcement bodies (they are structure-less PDFs), so
        this tool never invents revenue or profit from a title.

        Args:
            company_code: Universe code, e.g. ``00045.HK`` / ``MAR``.
            limit: Max events to return (default 50).
        """
        s = None
        try:
            s = _session()
            entity = s.execute(
                text(
                    "SELECT id, code, name_zh, metadata_json FROM entities"
                    " WHERE metadata_json->>'registry' = 'hotel-universe'"
                    " AND code = :code LIMIT 1"
                ),
                {"code": company_code.strip()},
            ).mappings().first()
            if entity is None:
                return {"company": company_code, "unavailable": [_unavailable(
                    "该实体不在酒店宇宙", f"code={company_code!r} 未在册")]}
            meta = entity["metadata_json"] or {}
            if isinstance(meta, str):
                import json as _json

                meta = _json.loads(meta)
            reg_gap = REGION_UNAVAILABLE.get(str(meta.get("region")))
            if reg_gap:
                return {
                    "company": entity["code"],
                    "unavailable": [_unavailable(reg_gap[1], f"region={meta.get('region')}")],
                    "alternatives": ["发行人官网/交易所公告原文可查；本面未接该市场车道，不代取"],
                }
            stock = str(entity["code"]).split(".")[0]
            rows = s.execute(
                text(
                    """
                    SELECT ci.payload
                    FROM crawl_items ci JOIN crawl_runs r ON r.id = ci.run_id
                    WHERE r.status = 'success'
                      AND r.source IN ('hkex-disclosure', 'sec-edgar-8k-operating')
                      AND (ci.payload->>'stock_code' = :stock
                           OR ci.payload->>'ticker' = :stock)
                    ORDER BY ci.idx DESC LIMIT :lim
                    """
                ),
                {"stock": stock, "lim": max(1, min(int(limit), 200))},
            ).scalars().all()
            events = []
            for payload in rows:
                p = payload or {}
                events.append({
                    "title": p.get("title"),
                    "date": p.get("release_date") or p.get("filed_at") or p.get("event_date"),
                    "category": p.get("ann_type") or p.get("form"),
                    "url": p.get("doc_url") or p.get("filing_url"),
                })
            out: dict[str, Any] = {
                "company": entity["code"],
                "name_zh": entity["name_zh"],
                "count": len(events),
                "events": events,
                "as_of": events[0]["date"] if events else _today(),
                "notes": [R_HK_PDF, "事件面只报公告元数据，不反推营收或利润"],
            }
            if not events:
                out["unavailable"] = [_unavailable(
                    "该家无公告事件", "事件面 0 条；不是零值序列，也不代表公司无披露")]
            return out
        except Exception as exc:  # noqa: BLE001
            return _session_failure(exc)
        finally:
            if s is not None:
                s.close()

    @mcp.tool(annotations=annotations_for("hotel_benchmark"))
    def hotel_benchmark(metric: str = "revpar", region: str | None = None) -> dict:
        """Industry benchmark side of a hotel comparison — **always unavailable**.

        This tool exists so callers receive a sentence instead of zeros: the
        only industry benchmark sources (STR / CoStar) are paid and are not
        wired into this platform. It never returns a peer average as a
        substitute, and never returns 0.

        Args:
            metric: Benchmark metric requested (revpar / adr / occupancy).
            region: Optional region scope the caller wanted (CN / EU / US …).
        """
        return {
            "metric": metric,
            "region": region,
            "benchmark": _unavailable(R_STR, "STR/CoStar 未接入；不提供同业均值替代"),
            "as_of": _today(),
            "notes": [
                "本工具恒返回不可得：基准侧付费未接，不是数据暂时缺失",
                "禁止以 0、空数组或省略字段表达该缺口",
                R_STR,
            ],
        }
