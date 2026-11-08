"""Flower-vertical read tools (add-flower-vertical, task 5.6).

Five read-only entry points over the flower universe and the lanes that already
feed the observation store:

* ``flower_universe``             — 7 listed companies / 23 Dounan commodity items
                                    / 8 AIPH countries, with per-lane availability
* ``flower_price_series``         — Dounan daily market prices per item and caliber
* ``flower_planted_area``         — AIPH planted-area cross-sections (per-row ref year)
* ``flower_company_financials``   — quarterly financials + flower revenue share
* ``flower_trade``                — HS6 flower trade tiers (three tiers, never merged)

Every tool is read-only: it opens a session, runs SELECTs, and closes. No tool
writes a row, warms a cache, or dispatches upstream.

Three modeling facts drive the payload shapes, and each one is a way the data
would otherwise be misread:

1. **Unit lives on the entity, not the concept.** One Dounan concept
   (``price.flower.dounan.avg``) covers both 元/枝 and 元/把 items; the unit is
   recorded per item in ``entities.metadata_json->>'unit'``. A tool that printed a
   concept-level unit would silently invite cross-unit averaging.
2. **AIPH rows carry their own reference year.** ``date`` is ``<ref_year>-01-01``
   with ``granularity='year'`` and the years are mixed (2005–2024) *inside one
   workbook*, so the payload is a cross-section, not a time series.
3. **Flower revenue share is nullable on purpose.** ``null`` means "disclosure
   not obtained" and must never be ranked as 0 — the payload separates
   ``share`` from ``share_unavailable_reason``.

Unavailable data is returned as an explicit ``unavailable`` block carrying the
reason the supply side already established (KIFA WAF block, JP EDINET key,
US segment XBRL extraction, trade tiers not scheduled, Dounan rolling window) —
never as an empty series pretending to be a real coverage gap.
"""
from __future__ import annotations

from typing import Any

from fastmcp import FastMCP
from sqlalchemy import text

from fd_open_data_mcp.annotations import annotations_for
from fd_open_data_mcp.db import get_database
from fd_open_data_mcp.fetch.dispatch import _coerce_value

# ─── reasons (verbatim from the change's unavailable-data contract) ─────────
R_KIFA = (
    "该数据当前不可得：KIFA（云南国际花卉拍卖交易中心）全站 HTTP 468，"
    "雷池 WAF JS 挑战未过，价格指数/品种报价/行情分析三端点均不可直取"
)
R_EDINET = "该数据当前不可得：日股 EDINET API key 未申请，票符亦未上游核实"
R_US_SEGMENT = (
    "该数据当前不可得：美股分部收入需从 10-K instance 抽取"
    "（company facts 已剥离维度），该通道尚未实施"
)
#: 实测（2026-11-08）：`sec-edgar-facts` 的底账原先只跑过 12 家餐饮票符，
#: 五家花卉美股从未被抓过——抽取器落地后（`flower_extractors/sec_edgar_facts.py`）
#: 才补齐 329 个观测点。所以「美股零行」现在的真因只剩「该公司不在本车道的
#: 抓取参数里」，不是「没有财务」，更不是「零营收」。
R_US_NOT_CRAWLED = (
    "该数据当前不可得：该公司没有 sec-edgar-facts 底账"
    "（车道未以它的票符触发过抓取），需先爬取再抽取"
)
R_DOUNAN_WINDOW = (
    "斗南上游仅保留约 2 个月滚动窗，历史自接入日起累积；"
    "缺日不补、缺价留空，不得以邻期插值或补 0 冒充"
)
R_AIPH_MIXED = (
    "AIPH 每个数据行自带参考年（实测 2005–2024 混装），"
    "这是截面不是时序：不跨年份求增速，也不把不同品类的参考年当成同一年"
)
R_AIPH_TRADE = "AIPH 只做面积不做贸易：贸易章为 Eurostat 二次发布，与既有贸易车道重复计数"
R_TRADE_NOT_SCHEDULED = (
    "该数据当前不可得：花卉三档 HS6（060311/060319/060120）尚未进商品贸易单元调度参数，"
    "库内零条。解锁动作：为 each 码配一次带 cmd_code 的 invocation"
)
R_NO_STOCK_TARGET = "该市场无上市花卉标的：港股检索 flower 全年命中 0，欧洲多为合作社/私有企业"

#: Dounan caliber concepts → (code, Chinese label)
DOUNAN_CALIBERS: tuple[tuple[str, str], ...] = (
    ("price.flower.dounan.avg", "均价"),
    ("price.flower.dounan.high", "最高价"),
    ("price.flower.dounan.low", "最低价"),
)
_DOUNAN_CODES = tuple(c for c, _ in DOUNAN_CALIBERS)

#: AIPH product slug → planted-area concept (product lives in the code: one
#: country legitimately reports several products in the same reference year, so
#: putting the product on the entity side would collide on uq_sem_obs).
PLANTED_CONCEPTS: tuple[tuple[str, str], ...] = (
    ("area.flower.rose.planted", "玫瑰"),
    ("area.flower.carnation.planted", "康乃馨"),
    ("area.flower.orchid.planted", "兰花"),
    ("area.flower.chrysanthemum.planted", "菊花"),
    ("area.flower.lily.planted", "百合"),
    ("area.flower.christmas_tree.planted", "圣诞树"),
    ("area.flower.cut_foliage.planted", "切叶"),
    ("area.flower.flowering_plant.planted", "开花植物"),
    ("area.flower.green_foliage_plant.planted", "绿植观叶"),
)
_PLANTED_CODES = tuple(c for c, _ in PLANTED_CONCEPTS)

#: HS6 flower tiers — never merged (merging hides the product mix).
TRADE_TIERS: tuple[tuple[str, str], ...] = (
    ("060311", "玫瑰切花（鲜）"),
    ("060319", "其他鲜切花"),
    ("060120", "花卉鳞茎/块根/球根"),
)

FINANCIAL_CODES = (
    "financials.revenue",
    "financials.operating_profit",
    "financials.net_income",
)
_FINANCIAL_ZH = {
    "financials.revenue": "营业收入",
    "financials.operating_profit": "营业利润",
    "financials.net_income": "净利润",
}

#: 观测行的 unit 列只写 ``currency``（不带币种）；币种按注册地补，避免跨市场混算。
_CURRENCY_BY_REGION = {"CN": "CNY", "US": "USD", "JP": "JPY"}

MAX_SERIES_ROWS = 5000


# ─── helpers ────────────────────────────────────────────────────────────────
def _session():
    return get_database().get_session()


def _unavailable(reason: str, detail: str | None = None) -> dict:
    out: dict[str, Any] = {"status": "unavailable", "reason": reason}
    if detail:
        out["detail"] = detail
    return out


def _session_failure(exc: Exception) -> dict:
    """A DB read that cannot run is reported as failure, not as empty data."""
    return {
        "status": "error",
        "error": exc.__class__.__name__,
        "detail": f"数据面读取失败：{exc}",
        "note": "这是读取失败，不是「无观测」；不要把本结果当成真实缺测",
    }


def _universe_rows(session, entity_type: str | None = None) -> list[dict]:
    sql = """
        SELECT id, entity_type, code, name_en, name_zh, metadata_json
        FROM entities
        WHERE metadata_json->>'list' = :lst
    """
    params: dict[str, Any] = {"lst": "flower-universe"}
    if entity_type:
        sql += " AND entity_type = :etype"
        params["etype"] = entity_type
    sql += " ORDER BY entity_type, code"
    return [dict(r) for r in session.execute(text(sql), params).mappings().all()]


def _stock_rows(session) -> list[dict]:
    return _universe_rows(session, "stock")


def _commodity_rows(session) -> list[dict]:
    return _universe_rows(session, "commodity")


def _country_rows(session) -> list[dict]:
    return _universe_rows(session, "country")


def _entity_brief(row: dict) -> dict:
    meta = row.get("metadata_json") or {}
    return {
        "entity_id": int(row["id"]),
        "entity_type": row["entity_type"],
        "code": row["code"],
        "name_zh": row["name_zh"],
        "name_en": row["name_en"],
    }


def _resolve_stock(session, entity_id: int | None, ticker: str | None) -> dict | None:
    rows = _stock_rows(session)
    if entity_id is not None:
        for r in rows:
            if int(r["id"]) == int(entity_id):
                return r
        return None
    if not ticker:
        return None
    t = ticker.strip().upper()
    for r in rows:
        meta = r.get("metadata_json") or {}
        if t in {str(r["code"]).upper(), str(meta.get("ticker") or "").upper()}:
            return r
    for r in rows:
        meta = r.get("metadata_json") or {}
        cand = str(meta.get("ticker") or r["code"]).upper()
        if t == cand.split(".")[0]:
            return r
    return None


def _resolve_commodity(session, entity_id: int | None, item: str | None) -> dict | None:
    rows = _commodity_rows(session)
    if entity_id is not None:
        for r in rows:
            if int(r["id"]) == int(entity_id):
                return r
        return None
    if not item:
        return None
    t = item.strip()
    for r in rows:
        meta = r.get("metadata_json") or {}
        if t in {str(r["code"]), str(meta.get("item_raw") or ""), str(r["name_zh"] or "")}:
            return r
    return None


def _counts_by_entity(session, codes: tuple[str, ...] | list[str]) -> dict[tuple[int, str], int]:
    """Per-(entity, concept) stored-row counts for the given concept codes."""
    rows = session.execute(
        text(
            """
            SELECT o.entity_id, c.code, count(*) AS n
            FROM semantic_observations o
            JOIN concepts c ON c.id = o.concept_id
            WHERE c.code = ANY(:codes)
              AND o.entity_id IN (
                  SELECT id FROM entities WHERE metadata_json->>'list' = 'flower-universe'
              )
            GROUP BY o.entity_id, c.code
            """
        ),
        {"codes": list(codes)},
    ).all()
    return {(int(eid), str(code)): int(n) for eid, code, n in rows}


def _points(session, entity_id: int, codes: tuple[str, ...] | list[str],
            limit: int = MAX_SERIES_ROWS) -> list[dict]:
    """Latest ``limit`` points **per concept**, not per call.

    A bare ``LIMIT n`` over a multi-concept query spends the whole budget on the
    alphabetically-first concept (observed live: 002200's ``financials.revenue``
    came back empty while the rows existed, because ``net_income`` sorted first).
    The window function keeps the budget per concept, which is what the payload
    contract promises.
    """
    rows = session.execute(
        text(
            """
            SELECT concept_code, concept_unit, date, value, unit, source_used,
                   fetched_at, granularity
            FROM (
                SELECT c.code AS concept_code, c.unit AS concept_unit,
                       o.date, o.value, o.unit, o.source_used, o.fetched_at, o.granularity,
                       row_number() OVER (PARTITION BY c.code ORDER BY o.date DESC) AS rn
                FROM semantic_observations o
                JOIN concepts c ON c.id = o.concept_id
                WHERE o.entity_id = :eid
                  AND c.code = ANY(:codes)
            ) t
            WHERE rn <= :lim
            ORDER BY concept_code, date DESC
            """
        ),
        {"eid": entity_id, "codes": list(codes), "lim": int(limit)},
    ).mappings().all()
    return [_fmt_point(r) for r in rows]


def _fmt_point(row: dict) -> dict:
    return {
        "concept": row["concept_code"],
        "date": row["date"],
        "value": _coerce_value(row["value"]),
        "unit": row["unit"] or row.get("concept_unit"),
        "source_used": row["source_used"],
        "as_of": row["date"],
        "granularity": row.get("granularity"),
        "fetched_at": row["fetched_at"].isoformat() if row["fetched_at"] else None,
    }


def _fin_lane_reason(lane: str) -> str | None:
    """财务车道零行时的归因。EDINET 是「key 未申请」，美股是「该公司未被爬过」。"""
    if lane == "edinet-api":
        return R_EDINET
    if lane == "sec-edgar-facts":
        return R_US_NOT_CRAWLED
    return None


def _lane_status(count: int, reason: str | None = None, caveat: str | None = None) -> dict:
    if count > 0:
        out = {"status": "data", "points": count}
        if caveat:
            out["caveat"] = caveat
        return out
    out = {"status": "unavailable", "points": 0}
    if reason:
        out["reason"] = reason
    if caveat:
        out["caveat"] = caveat
    return out


def _today() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ─── tools ──────────────────────────────────────────────────────────────────
def register_flower_tools(mcp: FastMCP) -> None:
    """Attach the flower read tools to the given FastMCP instance."""

    @mcp.tool(annotations=annotations_for("flower_universe"))
    def flower_universe(
        tier: str | None = None,
        region: str | None = None,
    ) -> dict:
        """List the registered flower universe with per-lane availability.

        Read-only. Returns three groups registered under one universe tag: the
        7 monitored listed companies (``pure-flower`` / ``adjacency`` tiers), the
        23 Kunming Dounan commodity items, and the 8 countries that appear in
        the AIPH planted-area chapter. Every lane carries either a real row
        count or an explicit unavailable reason — never a zero-valued
        substitute for "not obtained".

        Args:
            tier: Optional company-tier filter — ``pure-flower`` or ``adjacency``.
            region: Optional company-region filter — US, CN, JP.

        Returns:
            ``{counts, companies, commodities, countries, unavailable, as_of,
            notes}``. Each company: ``{entity_id, code, ticker, name_zh,
            name_en, region, segment, tier, flower_revenue_share,
            share_unavailable_reason, lanes:[{lane, status, points, reason,
            caveat}]}``. Each commodity: ``{entity_id, code, name_zh, item_raw,
            unit, lanes}``.
        """
        s = None
        try:
            s = _session()
            stocks = _stock_rows(s)
            commodities = _commodity_rows(s)
            countries = _country_rows(s)

            price_counts = _counts_by_entity(s, _DOUNAN_CODES)
            fin_counts = _counts_by_entity(s, FINANCIAL_CODES)
            area_counts = _counts_by_entity(s, _PLANTED_CODES)

            out_companies: list[dict] = []
            for r in stocks:
                meta = r.get("metadata_json") or {}
                if tier and str(meta.get("tier") or "") != tier.strip():
                    continue
                if region and str(meta.get("region") or "").upper() != region.strip().upper():
                    continue
                lane = str((meta.get("lanes") or ["none"])[0])
                n_fin = sum(fin_counts.get((int(r["id"]), c), 0) for c in FINANCIAL_CODES)
                region_code = str(meta.get("region") or "").upper()
                if lane == "edinet-api":
                    fin_lane = _lane_status(n_fin, R_EDINET)
                elif n_fin > 0:
                    fin_lane = _lane_status(n_fin)
                else:
                    fin_lane = _lane_status(n_fin, _fin_lane_reason(lane))
                share = meta.get("flower_revenue_share")
                out_companies.append({
                    **_entity_brief(r),
                    "ticker": meta.get("ticker"),
                    "region": region_code,
                    "segment": meta.get("segment"),
                    "tier": meta.get("tier"),
                    # null = 披露未取得；与 0 严格区分，工具不替它做排名
                    "flower_revenue_share": share,
                    "share_unavailable_reason": None if share is not None else (
                        R_EDINET if lane == "edinet-api" else R_US_SEGMENT
                    ),
                    "lanes": [{"lane": lane, "financials": fin_lane}],
                })

            out_commodities: list[dict] = []
            for r in commodities:
                meta = r.get("metadata_json") or {}
                lanes = []
                for code, zh in DOUNAN_CALIBERS:
                    n = price_counts.get((int(r["id"]), code), 0)
                    lanes.append({"caliber": zh, "concept": code,
                                  **_lane_status(n, None, R_DOUNAN_WINDOW)})
                out_commodities.append({
                    **_entity_brief(r),
                    "item_raw": meta.get("item_raw"),
                    # 单位在实体侧：同一概念横跨 元/枝 与 元/把，绝不取概念级单位
                    "unit": meta.get("unit"),
                    "lanes": lanes,
                })

            out_countries: list[dict] = []
            for r in countries:
                products: list[dict] = []
                total = 0
                for code, zh in PLANTED_CONCEPTS:
                    n = area_counts.get((int(r["id"]), code), 0)
                    if n:
                        total += n
                        products.append({"product": zh, "concept": code, "points": n})
                out_countries.append({
                    **_entity_brief(r),
                    "planted_products": products,
                    **_lane_status(total, None, R_AIPH_MIXED),
                })

            regions: dict[str, int] = {}
            for r in stocks:
                rc = str((r.get("metadata_json") or {}).get("region") or "")
                regions[rc] = regions.get(rc, 0) + 1

            return {
                "counts": {
                    "companies": len(out_companies),
                    "commodities": len(out_commodities),
                    "countries": len(out_countries),
                },
                "companies": out_companies,
                "commodities": out_commodities,
                "countries": out_countries,
                "regions": dict(sorted(regions.items())),
                "unavailable": [
                    _unavailable(R_KIFA, "KIFA 全站从未取到，属 never_held 而非断流"),
                    _unavailable(R_TRADE_NOT_SCHEDULED),
                    _unavailable(R_NO_STOCK_TARGET),
                ],
                "as_of": _today(),
                "notes": [
                    "宇宙口径真源：fd-industry-data/docs/flower-universe.yaml（7 家上市 + 23 个斗南品名 + 8 个 AIPH 国别）",
                    "纯花卉/邻接分层看主营构成证据，不看公司名称（FLO 是烘焙、AFCG 是大麻贷款 REIT、GSIW 是港资资管，均已排除）",
                    "A股无纯花卉主业标的：唯一带苗木/花卉分部的是 002200（苗木 0.15% + 租摆 0.08% = 0.23%），只进邻接层",
                    f"斗南价格：{R_DOUNAN_WINDOW}",
                    f"AIPH 面积：{R_AIPH_MIXED}；{R_AIPH_TRADE}",
                ],
            }
        except Exception as exc:  # noqa: BLE001
            return _session_failure(exc)
        finally:
            if s is not None:
                s.close()

    @mcp.tool(annotations=annotations_for("flower_price_series"))
    def flower_price_series(
        entity_id: int | None = None,
        item: str | None = None,
        calibers: list[str] | None = None,
        limit: int = 400,
    ) -> dict:
        """Read one Dounan commodity item's daily market prices (read-only).

        Calibers are three separate published values (``avg`` / ``high`` /
        ``low``); the average is *not* derived from high/low. The unit comes
        from the item entity (元/枝 or 元/把) because one concept covers both —
        averaging across items is therefore forbidden, and this tool never does
        it.

        Missing days are missing: the payload reports ``missing_dates`` inside
        the item's observed span and never interpolates or zero-fills.

        Args:
            entity_id: Commodity entity id (preferred when known).
            item: Item name — a slug (``dounan-red-rose-single``), the raw
                upstream name (``红玫瑰（单头）``) or the Chinese display name.
            calibers: Optional subset of ``avg`` / ``high`` / ``low``
                (default: all three).
            limit: Max rows per caliber (default 400, cap 5000).

        Returns:
            ``{item, unit, calibers:{avg:[points], high:[...], low:[...]},
            span, missing_dates, unavailable, as_of, notes}``.
        """
        if entity_id is None and not item:
            return {"error": "missing_item",
                    "detail": "pass entity_id or item (e.g. dounan-red-rose-single or 红玫瑰（单头）)"}
        limit = max(1, min(int(limit), MAX_SERIES_ROWS))
        wanted = {c.strip().lower() for c in (calibers or []) if c.strip()}
        codes = tuple(c for c, key in zip(_DOUNAN_CODES, ("avg", "high", "low"))
                      if not wanted or key in wanted)
        s = None
        try:
            s = _session()
            row = _resolve_commodity(s, entity_id, item)
            if row is None:
                return {
                    "error": "unknown_item",
                    "detail": f"未在花卉宇宙的斗南品名中找到 {item or entity_id!r}",
                    "hint": "先调 flower_universe 取商品清单（23 个品名）",
                }
            meta = row.get("metadata_json") or {}
            points = _points(s, int(row["id"]), codes, limit)
            # 观测行的 unit 列是维度码（实测为 NULL 或缺省为 currency_cny），
            # 真正的量纲在品名实体上（元/枝 vs 元/把）。逐点用实体单位，
            # 同时保留原始码字，避免把维度码当成给人看的单位。
            item_unit = meta.get("unit")
            by_caliber: dict[str, list[dict]] = {}
            for p in points:
                key = p["concept"].rsplit(".", 1)[-1]
                p["observed_unit_code"] = p["unit"]
                if item_unit:
                    p["unit"] = item_unit
                by_caliber.setdefault(key, []).append(p)
            dates = sorted({p["date"] for p in points})
            out: dict[str, Any] = {
                "item": {
                    **_entity_brief(row),
                    "item_raw": meta.get("item_raw"),
                    "unit": item_unit,
                    "market": meta.get("market"),
                },
                "unit": item_unit,
                "calibers": by_caliber,
                "span": {"start": dates[0], "end": dates[-1], "distinct_dates": len(dates)}
                if dates else None,
                "as_of": dates[-1] if dates else _today(),
                "notes": [
                    f"三价为上游发布值，均价不由最高/最低派生；{R_DOUNAN_WINDOW}",
                    "单位取品名实体（元/枝 或 元/把）：跨品名求「均价」是量纲混装，本工具不提供该聚合",
                    "价格日期取自报告标题（T+1 发布），与列表页发布日期相差一天",
                ],
            }
            if not points:
                out["unavailable"] = [_unavailable(
                    "该品名在其车道内零观测",
                    f"item={meta.get('item_raw')!r} 三个口径均 0 行；车道可能尚未跑或该品名未进报告",
                )]
            else:
                out["missing_dates"] = _missing_dates(dates)
            return out
        except Exception as exc:  # noqa: BLE001
            return _session_failure(exc)
        finally:
            if s is not None:
                s.close()

    @mcp.tool(annotations=annotations_for("flower_planted_area"))
    def flower_planted_area(
        product: str | None = None,
        country: str | None = None,
        limit: int = 200,
    ) -> dict:
        """Read AIPH planted-area cross-sections (read-only, annual, hectares).

        Every row carries its **own** reference year (``ref_year``, extracted
        from the observation date) because the yearbook mixes editions inside
        one workbook (2005–2024). The payload is therefore a cross-section: no
        growth rate is computed, and a per-product year range is reported so a
        caller cannot mistake it for a time series.

        Args:
            product: Optional product filter — ``rose``, ``carnation``,
                ``orchid``, ``chrysanthemum``, ``lily``, ``christmas_tree``,
                ``cut_foliage``, ``flowering_plant``, ``green_foliage_plant``.
            country: Optional ISO-2 country code (``EC``, ``NL``, ``CO`` …).
            limit: Max rows (default 200, cap 5000).

        Returns:
            ``{filters, rows:[{product, country, name_zh, ref_year, value,
            unit, source_used}], year_range, unavailable, as_of, notes}``.
            ``year_range`` is per product because the years are not aligned.
        """
        limit = max(1, min(int(limit), MAX_SERIES_ROWS))
        want = {c for c, _ in PLANTED_CONCEPTS}
        if product:
            key = product.strip().lower()
            match = [c for c, _ in PLANTED_CONCEPTS if c == f"area.flower.{key}.planted"]
            if not match:
                return {
                    "error": "unknown_product",
                    "detail": f"未知品类 {product!r}",
                    "hint": f"可用品类：{[c.split('.')[2] for c, _ in PLANTED_CONCEPTS]}",
                }
            want = set(match)
        s = None
        try:
            s = _session()
            sql = """
                SELECT c.code AS concept_code, c.unit AS concept_unit,
                       o.date, o.value, o.unit, o.source_used, o.granularity,
                       e.code AS country_code, e.name_zh, e.name_en
                FROM semantic_observations o
                JOIN concepts c ON c.id = o.concept_id
                JOIN entities e ON e.id = o.entity_id
                WHERE c.code = ANY(:codes)
            """
            params: dict[str, Any] = {"codes": sorted(want), "lim": limit}
            if country:
                sql += " AND e.code = :cc"
                params["cc"] = country.strip().upper()
            sql += " ORDER BY c.code, o.date DESC, e.code LIMIT :lim"
            rows = s.execute(text(sql), params).mappings().all()

            by_code = dict(PLANTED_CONCEPTS)
            out_rows: list[dict] = []
            years: dict[str, list[int]] = {}
            countries_seen: set[str] = set()
            for r in rows:
                date = r["date"]
                ref_year = date.year if hasattr(date, "year") else int(str(date)[:4])
                slug = r["concept_code"].split(".")[2]
                countries_seen.add(str(r["country_code"]))
                years.setdefault(slug, []).append(ref_year)
                out_rows.append({
                    "product": slug,
                    "product_zh": by_code.get(r["concept_code"]),
                    "country": r["country_code"],
                    "name_zh": r["name_zh"],
                    "name_en": r["name_en"],
                    "ref_year": ref_year,
                    "value": _coerce_value(r["value"]),
                    "unit": r["unit"] or r["concept_unit"],
                    "granularity": r["granularity"],
                    "source_used": r["source_used"],
                })
            out: dict[str, Any] = {
                "filters": {"product": product, "country": country},
                "count": len(out_rows),
                "rows": out_rows,
                "year_range": {k: {"min": min(v), "max": max(v), "distinct": len(set(v))}
                               for k, v in sorted(years.items())},
                "countries": sorted(countries_seen),
                "as_of": _today(),
                "notes": [
                    R_AIPH_MIXED,
                    R_AIPH_TRADE,
                    "品类落在概念码上（area.flower.<product>.planted），国别落在实体上："
                    "同一国别同一年可以有多个品类，故品类不能挂实体",
                    "数值为年鉴报告值，未做单位换算；公顷即上游口径",
                ],
            }
            if not out_rows:
                out["unavailable"] = [_unavailable(
                    "该筛选组合在 AIPH 面积章内零行",
                    "品类或国别可能不在本期年鉴内；这是覆盖事实，不是零面积",
                )]
            return out
        except Exception as exc:  # noqa: BLE001
            return _session_failure(exc)
        finally:
            if s is not None:
                s.close()

    @mcp.tool(annotations=annotations_for("flower_company_financials"))
    def flower_company_financials(
        entity_id: int | None = None,
        ticker: str | None = None,
        limit: int = 40,
    ) -> dict:
        """Read a flower company's quarterly financials and flower revenue share
        (read-only).

        Stored concepts: ``financials.revenue`` / ``financials.operating_profit``
        / ``financials.net_income``. The flower revenue share comes from the
        universe row: ``flower_revenue_share`` is ``null`` when disclosure was
        not obtained, and the payload then carries ``share_unavailable_reason``.
        ``null`` is never rendered as 0 and never ranked.

        Segment figures and consolidated figures are **not** additive; this tool
        returns consolidated financials only and says so.

        Args:
            entity_id: Stock entity id (preferred when known).
            ticker: Universe ticker — ``FLWS``, ``1377.T``, ``002200``.
            limit: Max rows per concept (default 40, cap 5000).

        Returns:
            ``{company, financials:{revenue:[...], operating_profit:[...],
            net_income:[...]}, flower_revenue_share, share_unavailable_reason,
            artifact, unavailable, as_of, notes}``.
        """
        if entity_id is None and not ticker:
            return {"error": "missing_entity",
                    "detail": "pass entity_id or ticker (e.g. FLWS or 002200)"}
        limit = max(1, min(int(limit), MAX_SERIES_ROWS))
        s = None
        try:
            s = _session()
            row = _resolve_stock(s, entity_id, ticker)
            if row is None:
                return {
                    "error": "unknown_entity",
                    "detail": f"未在花卉宇宙的上市公司中找到 {ticker or entity_id!r}",
                    "hint": "先调 flower_universe 取 7 家公司清单",
                }
            meta = row.get("metadata_json") or {}
            points = _points(s, int(row["id"]), FINANCIAL_CODES, limit)
            by_concept: dict[str, list[dict]] = {}
            for p in points:
                by_concept.setdefault(p["concept"], []).append(p)
            share = meta.get("flower_revenue_share")
            lane = str((meta.get("lanes") or ["none"])[0])
            reason = None
            if share is None:
                # 占比不可得与财务不可得是两个原因，分开给
                reason = R_EDINET if lane == "edinet-api" else R_US_SEGMENT
            unavailable: list[dict] = []
            if not points:
                unavailable.append(_unavailable(
                    _fin_lane_reason(lane) or "该实体在其财务车道内零观测",
                    f"ticker={meta.get('ticker')!r} lane={lane!r}"
                    "（零行≠零营收；原因见 reason）",
                ))
            if share is None:
                share_reason = reason or "花卉营收占比未披露"
                if unavailable and unavailable[0]["reason"] == share_reason:
                    # 同一原因（如 EDINET key 未申请）既让财务零行又让占比为
                    # null：合成一条，别在渲染层把同一件事列两遍。
                    unavailable[0]["detail"] += (
                        "；占比同样不可得（同一原因，不把 null 当 0）"
                    )
                else:
                    unavailable.append(_unavailable(
                        share_reason,
                        "占比不可得与占比为 0 是两件事：本工具不把 null 当 0",
                    ))
            return {
                "company": {
                    **_entity_brief(row),
                    "ticker": meta.get("ticker"),
                    "region": meta.get("region"),
                    "segment": meta.get("segment"),
                    "tier": meta.get("tier"),
                    # 观测行的 unit 只有 'currency'，币种不在里面：按市场补，
                    # 否则 A股「元」与美股「美元」会被读成同一量纲。
                    "currency": _CURRENCY_BY_REGION.get(str(meta.get("region") or "").upper()),
                },
                "financials": {
                    _FINANCIAL_ZH[c]: by_concept.get(c, []) for c in FINANCIAL_CODES
                },
                "flower_revenue_share": share,
                "share_unavailable_reason": reason,
                "artifact": {
                    "verified": meta.get("verified"),
                    "lanes": meta.get("lanes"),
                },
                "unavailable": unavailable,
                "as_of": _today(),
                "notes": [
                    "合并口径财务：分部口径与合并口径不可相加，分部合计通常小于合并营收",
                    "花卉占比真源为分部/主营构成披露，未披露或口径不可比的一律 null（不写 0、不入排名）",
                    "季频：财务数据期间以各点 date 为准，不是本工具的 as_of",
                ],
            }
        except Exception as exc:  # noqa: BLE001
            return _session_failure(exc)
        finally:
            if s is not None:
                s.close()

    @mcp.tool(annotations=annotations_for("flower_trade"))
    def flower_trade(
        flow: str | None = None,
        hs_code: str | None = None,
        limit: int = 300,
    ) -> dict:
        """Read flower HS6 trade tiers (read-only, annual, USD).

        Three tiers exist and are **never merged** (merging hides the product
        mix): ``060311`` fresh roses, ``060319`` other fresh cut flowers,
        ``060120`` bulbs/corms/tubers in growth. ``0602`` live ornamental plants
        is a different structure and is not part of this surface.

        Export and import are separate flows on purpose: subtracting one from
        the other mixes FOB and CIF valuation, so the payload reports each flow
        and never derives a net figure here.

        Args:
            flow: ``X`` (export) or ``M`` (import); omit for both.
            hs_code: Optional single tier among the three above.
            limit: Max rows per tier (default 300, cap 5000).

        Returns:
            ``{filters, tiers:[{hs_code, label_zh, flow, points}], unavailable,
            as_of, notes}``.
        """
        limit = max(1, min(int(limit), MAX_SERIES_ROWS))
        f = (flow or "").strip().upper() or None
        if f not in (None, "X", "M"):
            return {"error": "bad_flow", "detail": "flow 只能是 X（出口）或 M（进口）"}
        tiers = [t for t in TRADE_TIERS if not hs_code or t[0] == hs_code.strip()]
        if not tiers:
            return {"error": "unknown_hs_code",
                    "detail": f"未知档位 {hs_code!r}；可用 {[t[0] for t in TRADE_TIERS]}"}
        flows = [f] if f else ["X", "M"]
        codes = [f"trade.{'export' if x == 'X' else 'import'}_flow.world" for x in flows]
        s = None
        try:
            s = _session()
            rows = s.execute(
                text(
                    """
                    SELECT e.code AS hs_code, c.code AS concept_code, c.unit AS concept_unit,
                           o.date, o.value, o.unit, o.source_used
                    FROM semantic_observations o
                    JOIN concepts c ON c.id = o.concept_id
                    JOIN entities e ON e.id = o.entity_id
                    WHERE e.entity_type = 'hs_code'
                      AND e.code = ANY(:hs)
                      AND c.code = ANY(:concepts)
                    ORDER BY e.code, c.code, o.date DESC
                    LIMIT :lim
                    """
                ),
                {"hs": [t[0] for t in tiers], "concepts": codes, "lim": limit},
            ).mappings().all()
            by_key: dict[tuple[str, str], list[dict]] = {}
            for r in rows:
                fl = "X" if "export" in r["concept_code"] else "M"
                by_key.setdefault((r["hs_code"], fl), []).append({
                    "date": r["date"],
                    "value": _coerce_value(r["value"]),
                    "unit": r["unit"] or r["concept_unit"],
                    "source_used": r["source_used"],
                })
            labels = dict(TRADE_TIERS)
            out_tiers = [
                {"hs_code": code, "label_zh": labels.get(code), "flow": fl,
                 "points": by_key.get((code, fl), [])}
                for code, _zh in tiers for fl in flows
            ]
            out: dict[str, Any] = {
                "filters": {"flow": f, "hs_code": hs_code},
                "tiers": out_tiers,
                "as_of": _today(),
                "notes": [
                    "三档恒不合并：合并会掩盖品类结构",
                    "出口 FOB 与进口 CIF 存在运保费差，本工具不派生「净出口」",
                    "只取 partner=world 轴；伙伴国明细需另轮参数化取数，不与世界轴混画",
                ],
            }
            if not rows:
                out["unavailable"] = [_unavailable(
                    R_TRADE_NOT_SCHEDULED,
                    "三档 HS 实体或观测均未入库；这是车道未开，不是贸易为零",
                )]
            return out
        except Exception as exc:  # noqa: BLE001
            return _session_failure(exc)
        finally:
            if s is not None:
                s.close()


def _missing_dates(dates: list[str], max_report: int = 10) -> dict:
    """Gaps inside an observed span — reported, never filled.

    The Dounan source does not publish on every calendar day (weekends and
    holidays are legitimately absent), so a calendar-day gap list is only a
    prompt to look, not a defect claim. ``weekday_gaps`` counts the missing
    Monday–Friday dates, which is the useful subset.
    """
    from datetime import date, timedelta

    if not dates:
        return {"count": 0, "sample": [], "weekday_gaps": 0}
    ds = sorted({d if isinstance(d, date) else date.fromisoformat(str(d)[:10]) for d in dates})
    gaps: list[str] = []
    weekday = 0
    cur = ds[0]
    known = set(ds)
    while cur <= ds[-1]:
        if cur not in known:
            gaps.append(cur.isoformat())
            if cur.weekday() < 5:
                weekday += 1
        cur += timedelta(days=1)
    return {
        "count": len(gaps),
        "weekday_gaps": weekday,
        "sample": gaps[:max_report],
        "note": "仅列出观测区间内的空档；源本身按交易日发布，工作日空档才值得追",
    }
