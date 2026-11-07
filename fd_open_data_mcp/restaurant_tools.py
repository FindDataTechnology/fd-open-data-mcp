"""Restaurant-vertical read tools (add-restaurant-vertical, task 4.5).

Five read-only entry points over the restaurant universe and the lanes that
already feed the observation store / crawl event plane:

* ``restaurant_universe``            — the 35-company universe + lane coverage
* ``restaurant_company_events``      — HKEX title events / SEC EDGAR events
* ``restaurant_company_financials``  — quarterly revenue / operating profit /
                                       net income observations
* ``restaurant_series``              — macro and cost-chain series (CPI pair
                                       included)
* ``restaurant_sampling_notices``    — SAMR food-inspection notices (event
                                       plane only, never an aggregate series)

Every tool is read-only: it opens a session, runs SELECTs, and closes. No
tool writes a row, warms a cache, or dispatches upstream. Unavailable data is
returned as an explicit ``unavailable`` block with the reason the supply side
already established (EDINET key missing, TWSE snapshot-only 月营收 with no
profit/event face, HKEX headline numbers only in PDFs, SEC periods below the
24-period gate) — never as an empty series pretending to be a real coverage gap.
"""
from __future__ import annotations

from typing import Any

from fastmcp import FastMCP
from sqlalchemy import text

from fd_open_data_mcp.annotations import annotations_for
from fd_open_data_mcp.db import get_database
from fd_open_data_mcp.fetch.dispatch import _coerce_value

# ─── reasons (verbatim from the change's unavailable-data contract) ─────────
R_EDINET = "该数据当前不可得：EDINET key 未到位"
# 2026-11-08 更正：此前写「上游 TWSE 通道不可达」是错的——`t187ap05_L`（上市公司每月
# 營業收入彙總）实测 200/1088 家；超时的是 STOCK_DAY_AVG 与 rwd/afterTrading 两个端点。
# 台股真实边界：月营收是**快照式**（只透当期、无历史，按月累积）；利润表与披露事件面
# 不在 TWSE OpenAPI 上（MOPS 被 FOR SECURITY REASONS 拦），故财务仍不可得。
R_TWSE = "台股仅月营收快照（当期、无历史），利润表与披露事件不可得"
R_HK_PDF = "仅在公告 PDF 原文，未结构化"
R_SHORT_WINDOW = "期数不足未入库"

FINANCIAL_CODES = (
    "financials.revenue",
    "financials.operating_profit",
    "financials.net_income",
)
_FINANCIAL_SHORT = {
    "revenue": "financials.revenue",
    "operating_profit": "financials.operating_profit",
    "net_income": "financials.net_income",
}

#: Entities whose SEC financial lane is below the 24-period observation gate
#: (documented in fd-industry-data/docs/sec-edgar-tag-map.md).
_SHORT_WINDOW_CONCEPTS: dict[str, set[str]] = {
    "BROS": set(FINANCIAL_CODES),          # 19 periods
    "QSR": {"financials.net_income"},      # 8 periods
    "TXRH": {"financials.net_income"},     # 16 periods
}

#: HKEX code the disclosure feed actually carries for 特海国际 (09658); the
#: universe row currently records 00965.HK. Reported, never silently rewritten.
_HK_TICKER_NOTES = {
    "00965.HK": (
        "披露易本轮公示的 09658「特海國際」公告未映射到宇宙登记的 00965.HK；"
        "请核对宇宙票号（本工具不改实体）"
    ),
}

_CPI_PAIR = {
    "price.cpi.us.food_away_from_home": "price.cpi.us.food_at_home",
    "price.cpi.us.food_at_home": "price.cpi.us.food_away_from_home",
}

MAX_SERIES_ROWS = 5000
MAX_EVENT_ROWS = 1000


# ─── helpers ────────────────────────────────────────────────────────────────
def _session():
    return get_database().get_session()


def _unavailable(reason: str, detail: str | None = None) -> dict:
    out: dict[str, Any] = {"status": "unavailable", "reason": reason}
    if detail:
        out["detail"] = detail
    return out


def _session_failure(exc: Exception) -> dict:
    """A DB read that cannot run is reported as unavailable, not as empty data."""
    return {
        "status": "error",
        "error": exc.__class__.__name__,
        "detail": f"数据面读取失败：{exc}",
        "note": "这是读取失败，不是「无观测」；不要把本结果当成真实缺测",
    }


def _universe_rows(session) -> list[dict]:
    rows = session.execute(
        text(
            """
            SELECT id, entity_type, code, name_en, name_zh, metadata_json
            FROM entities
            WHERE metadata_json->>'list' = :lst
            ORDER BY code
            """
        ),
        {"lst": "restaurant-universe"},
    ).mappings().all()
    return [dict(r) for r in rows]


def _resolve_entity(session, entity_id: int | None, ticker: str | None) -> dict | None:
    """Resolve a universe entity by id or ticker (with or without market suffix)."""
    rows = _universe_rows(session)
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
    # bare HK code ("06862") or bare numeric code ("002186")
    for r in rows:
        meta = r.get("metadata_json") or {}
        cand = str(meta.get("ticker") or r["code"]).upper()
        if t == cand.split(".")[0]:
            return r
    return None


def _name_key(name: str) -> str:
    """公司名归一：剥掉 A股风险警示前缀（``ST`` / ``*ST``）与空白。

    同一 6 位基础代码只对应一家上市公司，但公司被风险警示/改名后爬虫会以新
    名字另建一个 ``code#id`` 实体（例：宇宙登记 ``绝味食品`` / 观测实体
    ``ST绝味``）。故按归一名字比对，仍要求**基础代码相同 + 归一名字相同**。
    与 ``dashboards/build_restaurant_company_spec.py::_name_key`` 同一规则。
    """
    key = (name or "").strip()
    for prefix in ("*ST", "*st", "ST", "st"):
        if key.startswith(prefix):
            key = key[len(prefix):].strip()
            break
    return key


def _sibling_entities(session, row: dict) -> list[dict]:
    """同基础代码的兄弟实体（``603043#8227`` 一类）。

    没有这一步，``restaurant_company_financials`` 会对部分 A股餐饮公司报
    "源侧未覆盖" —— 库里明明有 20 项概念、到 2026-09-30，只是落在别名实体上。
    把"库里有"误报成"源侧没有"比空数组更坏，故读路径必须合并。

    判据：**同 6 位基础代码 + 同 ``entity_type``**。A股/港股的一只股票代码只对应
    一家上市公司，改名（如 ``603007`` → 顺景科技）后爬虫会以新名字另建实体，
    此时归一名字**也对不上**——若坚持名字相等，改过名的公司永远读不到。
    故名字只作披露信号（``name_matches``），不作准入闸；不同基础代码的实体
    一律不认（绝不跨公司借数）。
    """
    base = str(row.get("code") or "").split("#", 1)[0].strip()
    if not base:
        return []
    rows = session.execute(
        text(
            """
            SELECT id, code, name_zh
            FROM entities
            WHERE entity_type = 'stock'
              AND code LIKE :pat
            ORDER BY id
            """
        ),
        {"pat": f"{base}#%"},
    ).all()
    want = _name_key(row.get("name_zh") or "")
    out: list[dict] = []
    for r in rows:
        if int(r[0]) == int(row["id"]):
            continue
        alias_name = r[2] or ""
        out.append({
            "entity_id": int(r[0]),
            "code": str(r[1]),
            "name_zh": alias_name,
            "name_matches": _name_key(alias_name) == want,
        })
    return out


def _entity_brief(row: dict) -> dict:
    meta = row.get("metadata_json") or {}
    return {
        "entity_id": int(row["id"]),
        "entity_type": row["entity_type"],
        "code": row["code"],
        "ticker": meta.get("ticker"),
        "name_zh": row["name_zh"],
        "name_en": row["name_en"],
        "region": meta.get("region"),
        "segment": meta.get("segment"),
        "lanes": list(meta.get("lanes") or []),
    }


def _event_rows(session) -> list[dict]:
    """All successful event-plane rows, deduped by the upstream document key."""
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for source, key_col in (("hkex-disclosure", "doc_url"), ("sec-edgar-facts", "accession")):
        rows = session.execute(
            text(
                """
                SELECT ci.payload
                FROM crawl_items ci
                JOIN crawl_runs r ON r.id = ci.run_id
                WHERE r.source = :source
                  AND r.status = 'success'
                  AND (ci.payload->>'kind' IS NULL OR ci.payload->>'kind' = 'event')
                ORDER BY ci.run_id, ci.idx
                """
            ),
            {"source": source},
        ).all()
        for (payload,) in rows:
            if not isinstance(payload, dict):
                continue
            dedupe_key = str(payload.get(key_col) or payload.get("doc_url") or payload.get("primary_doc_url") or "")
            if not dedupe_key:
                continue
            marker = (source, dedupe_key)
            if marker in seen:
                continue
            seen.add(marker)
            out.append(payload)
    return out


def _financial_counts(session) -> dict[int, dict[str, int]]:
    """Per-entity stored-row counts for the three financial concepts.

    同基础代码的别名实体（``603043#8227``）计数并入宇宙裸码实体：否则宇宙清单
    会把"库里有、只是落在别名实体上"报成 `no_data / 源侧未覆盖`。
    """
    rows = session.execute(
        text(
            """
            SELECT o.entity_id, c.code, count(*) AS n
            FROM semantic_observations o
            JOIN concepts c ON c.id = o.concept_id
            JOIN entities e ON e.id = o.entity_id
            WHERE c.code IN ('financials.revenue',
                             'financials.operating_profit',
                             'financials.net_income')
              AND e.entity_type = 'stock'
              AND split_part(e.code, '#', 1) IN (
                  SELECT code FROM entities
                  WHERE metadata_json->>'list' = 'restaurant-universe'
              )
            GROUP BY o.entity_id, c.code
            """
        )
    ).all()
    out: dict[int, dict[str, int]] = {}
    for entity_id, code, n in rows:
        out.setdefault(int(entity_id), {})[str(code)] = int(n)
    for alias_id, canonical_id in _alias_map(session).items():
        counts = out.pop(alias_id, None)
        if not counts:
            continue
        bucket = out.setdefault(canonical_id, {})
        for code, n in counts.items():
            bucket[code] = bucket.get(code, 0) + n
    return out


def _alias_map(session) -> dict[int, int]:
    """同码别名实体 id -> 宇宙裸码实体 id（仅归一名字相同的同一家公司）。

    规模：全库 3,406 个 ``code#id`` 实体；这里只建宇宙内 35 家的映射，
    不碰其余实体（跨公司清洗属另一 change）。
    """
    universe = {str(r["code"]): r for r in _universe_rows(session)}
    if not universe:
        return {}
    rows = session.execute(
        text(
            """
            SELECT id, code, name_zh
            FROM entities
            WHERE entity_type = 'stock'
              AND code LIKE '%#%'
            """
        )
    ).all()
    mapping: dict[int, int] = {}
    for eid, code, _name_zh in rows:
        base = str(code or "").split("#", 1)[0]
        target = universe.get(base)
        if target:  # 同码即同一上市公司；名字差不作排除（改名公司）
            mapping[int(eid)] = int(target["id"])
    return mapping


def _cninfo_operating_counts(session) -> dict[str, int]:
    """``cninfo-operating`` 车道按 sec_code 数的成功条目。

    这条车道供的是「交易所月度经营数据公告」（产销快报/销售简报/发电量/
    主要运营数据/经营简报）。判它通不通只能看**爬取条目**：``financials.*``
    那批观测的 ``source_used`` 是 ``akshare``，与 cninfo 无关，拿它顶账会把
    「经营公告 0 命中」误报成「有数据」。
    """
    rows = session.execute(
        text(
            """
            SELECT split_part(coalesce(ci.payload->>'sec_code', ''), '#', 1) AS code,
                   count(*) AS n
            FROM crawl_items ci
            JOIN crawl_runs r ON r.id = ci.run_id
            WHERE r.source = 'cninfo-operating'
              AND r.status = 'success'
            GROUP BY 1
            """
        )
    ).all()
    return {str(code): int(n) for code, n in rows if code}


def _monthly_revenue_counts(session) -> dict[int, int]:
    """Per-entity row counts for the TWSE monthly-revenue concept.

    台股月营收在库里落在共享概念 ``industry.monthly_operation_metric``
    （TWSE ``t187ap05_L`` 快照式累积）；这里只数宇宙内实体，用于把
    ``twse-monthly-revenue`` 车道的状态从「不可得」翻成「有数据」。
    """
    rows = session.execute(
        text(
            """
            SELECT o.entity_id, count(*) AS n
            FROM semantic_observations o
            JOIN concepts c ON c.id = o.concept_id
            WHERE c.code = 'industry.monthly_operation_metric'
              AND o.source_used = 'twse-monthly-revenue'
              AND o.entity_id IN (
                  SELECT id FROM entities
                  WHERE metadata_json->>'list' = 'restaurant-universe'
              )
            GROUP BY o.entity_id
            """
        )
    ).all()
    return {int(entity_id): int(n) for entity_id, n in rows}


def _lane_status_for(row: dict, events_by_code: dict[str, list[str]],
                     fins: dict[int, dict[str, int]],
                     monthly: dict[int, int] | None = None,
                     cninfo: dict[str, int] | None = None) -> list[dict]:
    """Per-lane availability with explicit unavailable reasons.

    ``status`` is one of ``data`` (rows present), ``partial`` (some rows but a
    documented gap), ``no_data`` (lane registered, zero rows — the reason is
    reported) or ``unavailable`` (upstream blocker established by the change).
    """
    meta = row.get("metadata_json") or {}
    region = meta.get("region")
    ticker = str(meta.get("ticker") or row["code"])
    eid = int(row["id"])
    lanes: list[dict] = []

    for lane in meta.get("lanes") or []:
        if lane == "hkex-disclosure":
            code = ticker.split(".")[0]
            events = events_by_code.get(code, [])
            if events:
                lanes.append({
                    "lane": lane, "status": "data", "events": len(events),
                    "caveat": f"港股头条数字：{R_HK_PDF}",
                })
            else:
                lanes.append({
                    "lane": lane, "status": "no_data", "events": 0,
                    "detail": _HK_TICKER_NOTES.get(ticker)
                    or "披露易本轮公示窗口内无该公司公告",
                })
        elif lane == "sec-edgar-facts":
            events = events_by_code.get(ticker.upper(), [])
            counts = fins.get(eid, {})
            missing = [c for c in FINANCIAL_CODES if not counts.get(c)]
            if events and not missing:
                lanes.append({"lane": lane, "status": "data", "events": len(events),
                              "financial_points": sum(counts.values())})
            elif events or counts:
                detail = "；".join(
                    f"{c}: {R_SHORT_WINDOW}" for c in missing
                ) or None
                lanes.append({
                    "lane": lane,
                    "status": "partial" if (events or counts) else "no_data",
                    "events": len(events),
                    "financial_points": sum(counts.values()),
                    "detail": detail,
                })
            else:
                lanes.append({"lane": lane, "status": "no_data", "events": 0,
                              "detail": f"该实体在库内无事件与财务观测（{R_SHORT_WINDOW}）"})
        elif lane == "cninfo-operating":
            # 这条车道的真身是「交易所月度经营数据公告」，判据必须是爬取条目，
            # 不能拿 financials.* 顶上——那批观测的 source_used 是 akshare（东财转载），
            # 跟 cninfo 一点关系没有。用财务点数冒充，会把「经营公告 0 命中」
            # 报成「有数据」，读者再也没法判断这条车道到底通不通。
            n = int((cninfo or {}).get(str(ticker).split(".")[0], 0))
            if n:
                lanes.append({"lane": lane, "status": "data", "events": n})
            else:
                lanes.append({
                    "lane": lane, "status": "no_data", "events": 0,
                    "detail": "源侧无此类：餐饮 A股不发布月度经营数据公告"
                              "（披露节奏是季报/半年报/年报），不是抓取失败",
                })
        elif lane == "fd-cn-report":
            counts = fins.get(eid, {})
            if counts:
                lanes.append({
                    "lane": lane, "status": "data",
                    "financial_points": sum(counts.values()),
                    "caveat": "实际供源是 akshare（东方财富财务数据转载），"
                              "不是 cnreport——该 6 家在 cnreport 下 0 条",
                })
            else:
                lanes.append({
                    "lane": lane, "status": "no_data", "financial_points": 0,
                    "detail": "源侧未覆盖该实体营收/营业利润/净利概念",
                })
        elif lane == "twse-monthly-revenue":
            n = int((monthly or {}).get(eid, 0))
            if n:
                lanes.append({
                    "lane": lane, "status": "data", "monthly_points": n,
                    "caveat": "快照式月营收：端点只透当期、无历史回溯，按住月正向累积",
                })
            else:
                lanes.append({"lane": lane, "status": "unavailable", "events": 0,
                              "reason": R_TWSE})
        elif lane == "edinet-api":
            lanes.append({"lane": lane, "status": "unavailable", "events": 0,
                          "reason": R_EDINET})
        else:
            lanes.append({"lane": lane, "status": "no_data", "events": 0,
                          "detail": "未登记该车道的读取适配"})
    return lanes


def _events_by_code(payloads: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for p in payloads:
        if p.get("source") == "hkex-disclosure":
            code = str(p.get("stock_code") or "").upper()
        else:
            code = str(p.get("ticker") or "").upper()
        out.setdefault(code, []).append(p)
    return out


def _points_query(session, entity_id: int, codes: tuple[str, ...] | list[str]):
    return session.execute(
        text(
            """
            SELECT c.code AS concept_code, c.unit AS concept_unit,
                   o.date, o.value, o.unit, o.source_used, o.fetched_at,
                   o.granularity
            FROM semantic_observations o
            JOIN concepts c ON c.id = o.concept_id
            WHERE o.entity_id = :eid
              AND c.code = ANY(:codes)
            ORDER BY c.code, o.date DESC
            """
        ),
        {"eid": entity_id, "codes": list(codes)},
    ).mappings().all()


def _points_with_siblings(session, row: dict, codes: list[str]) -> tuple[int, list[dict], dict | None]:
    """取该公司的概念观测：在裸码实体与同基础代码兄弟实体之间挑**最新**的一份。

    返回 ``(选中的 entity_id, 行, 兄弟实体描述或 None)``。三条硬约束：

    * **同一概念只取一个实体**，绝不跨实体拼接（否则会拼出"半家 A + 半家 B"的假序列）；
    * **按最新观测日挑，不按"裸码优先"**：实测 `603043` 裸码实体只有到 2025-12-31 的
      应付/应收/负债率（37 条、无营收），别名实体 `603043#8227` 才有到 2026-09-30 的
      831 条全套报表。裸码优先会稳定地返回**更旧**的数据，而且看不出它旧；
    * 平票（同一最新日）再比条数，最后才用裸码实体兜底——保证结果可复现。
    """
    canonical = int(row["id"])
    own = _points_query(session, canonical, codes)
    siblings = _sibling_entities(session, row)
    candidates: list[tuple[int, list[dict], dict | None]] = []
    if own:
        candidates.append((canonical, own, None))
    for sib in siblings:
        got = _points_query(session, sib["entity_id"], codes)
        if got:
            candidates.append((sib["entity_id"], got, sib))
    if not candidates:
        return canonical, own, None
    if len(candidates) == 1:
        return candidates[0]

    def _rank(cand: tuple[int, list[dict], dict | None]):
        eid, got, _sib = cand
        latest = max((r["date"] for r in got), default=None)
        # 日期降序 → 条数降序 → 裸码实体优先（eid == canonical 排前）
        return (latest or "", len(got), eid == canonical)

    return max(candidates, key=_rank)


def _fmt_point(row: dict) -> dict:
    return {
        "date": row["date"],
        "value": _coerce_value(row["value"]),
        "unit": row["unit"] or row.get("concept_unit"),
        "source_used": row["source_used"],
        "as_of": row["date"],
        "fetched_at": row["fetched_at"].isoformat() if row["fetched_at"] else None,
    }


# ─── tools ──────────────────────────────────────────────────────────────────
def register_restaurant_tools(mcp: FastMCP) -> None:
    """Attach the restaurant read tools to the given FastMCP instance."""

    @mcp.tool(annotations=annotations_for("restaurant_universe"))
    def restaurant_universe(
        region: str | None = None,
        segment: str | None = None,
    ) -> dict:
        """List the registered restaurant universe with per-lane availability.

        Read-only. Returns the 35 monitored listed restaurant companies with
        region / segment / ticker / lanes and, for every lane, whether the
        store holds data or the lane is currently unavailable and why. An
        unavailable lane carries the explicit reason (EDINET key missing, TWSE
        snapshot-only 月营收 without a profit/event face, headline numbers only in
        HKEX PDFs, SEC periods below the observation gate) — never a zero-valued
        substitute.

        Args:
            region: Optional filter — HK, US, CN, TW or JP.
            segment: Optional filter — limited-service, full-service,
                prepared-food or cafe-teahouse.

        Returns:
            ``{count, filters, regions, segments, entities, as_of, notes}``.
            Each entity: ``{entity_id, ticker, code, name_zh, name_en, region,
            segment, lanes:[{lane, status, events, financial_points, detail,
            reason, caveat}], data_status}``.
        """
        s = None
        try:
            s = _session()
            rows = _universe_rows(s)
            payloads = _event_rows(s)
            by_code = _events_by_code(payloads)
            fins = _financial_counts(s)
            monthly = _monthly_revenue_counts(s)
            cninfo = _cninfo_operating_counts(s)
            out_entities: list[dict] = []
            for r in rows:
                meta = r.get("metadata_json") or {}
                if region and str(meta.get("region") or "").upper() != region.strip().upper():
                    continue
                if segment and str(meta.get("segment") or "") != segment.strip():
                    continue
                lanes = _lane_status_for(r, by_code, fins, monthly, cninfo)
                has_data = any(l["status"] in ("data", "partial") for l in lanes)
                out_entities.append(
                    {**_entity_brief(r), "lanes": lanes,
                     "data_status": "has_data" if has_data else "no_data"}
                )
            regions: dict[str, int] = {}
            segments: dict[str, int] = {}
            for r in rows:
                meta = r.get("metadata_json") or {}
                regions[str(meta.get("region"))] = regions.get(str(meta.get("region")), 0) + 1
                segments[str(meta.get("segment"))] = segments.get(str(meta.get("segment")), 0) + 1
            return {
                "count": len(out_entities),
                "filters": {"region": region, "segment": segment},
                "regions": dict(sorted(regions.items())),
                "segments": dict(sorted(segments.items())),
                "entities": out_entities,
                "as_of": _today(),
                "notes": [
                    "宇宙口径真源：fd-industry-data/docs/restaurant-universe.yaml（35 家）",
                    "status=data 表示该车道有真实行；partial 表示有行但存在已声明缺口；"
                    "no_data 表示车道已登记但零行（原因在 detail）；unavailable 表示上游阻塞"
                    "（原因在 reason）",
                    f"港股头条数字边界：{R_HK_PDF}",
                    f"日股：{R_EDINET}",
                    f"台股：{R_TWSE}",
                ],
            }
        except Exception as exc:  # noqa: BLE001
            return _session_failure(exc)
        finally:
            if s is not None:
                s.close()

    @mcp.tool(annotations=annotations_for("restaurant_company_events"))
    def restaurant_company_events(
        entity_id: int | None = None,
        ticker: str | None = None,
        limit: int = 50,
    ) -> dict:
        """Read a restaurant company's disclosure events (read-only).

        Sources currently registered: HKEX 披露易 title events
        (``source_used = hkex-disclosure``, date = ``release_date``) and SEC
        EDGAR company-facts events (``source_used = sec-edgar-facts``, date =
        ``filed``). Each event carries ``source_used``, ``disclosure_date``
        and ``doc_url``. Rows are deduplicated by upstream document key.
        Companies without an event lane return an explicit ``unavailable``
        block with the region-specific reason, not an empty list alone.

        Args:
            entity_id: Universe entity id (preferred when known).
            ticker: Universe ticker — ``06862``, ``06862.HK`` or ``MCD``.
            limit: Maximum events to return per source (default 50, cap 1000).

        Returns:
            ``{entity, count, events, unavailable, as_of}``.
        """
        if entity_id is None and not ticker:
            return {"error": "missing_entity",
                    "detail": "pass entity_id or ticker (e.g. 06862 or MCD)"}
        limit = max(1, min(int(limit), MAX_EVENT_ROWS))
        s = None
        try:
            s = _session()
            row = _resolve_entity(s, entity_id, ticker)
            if row is None:
                return {"error": "entity_not_found",
                        "detail": f"未在 restaurant-universe 中找到 entity_id={entity_id!r} "
                                  f"ticker={ticker!r}"}
            brief = _entity_brief(row)
            meta = row.get("metadata_json") or {}
            region = meta.get("region")
            tick = str(meta.get("ticker") or row["code"])

            unavailable: list[dict] = []
            events: list[dict] = []
            if region == "HK":
                code = tick.split(".")[0]
                payloads = [p for p in _event_rows(s)
                            if p.get("source") == "hkex-disclosure"
                            and str(p.get("stock_code") or "").upper() == code.upper()]
                payloads.sort(key=lambda p: str(p.get("release_date") or ""), reverse=True)
                events = [{
                    "source_used": "hkex-disclosure",
                    "disclosure_date": p.get("release_date"),
                    "as_of": p.get("release_date"),
                    "ann_type": p.get("ann_type"),
                    "title_zh": p.get("title_zh"),
                    "title": p.get("title"),
                    "is_profit_warning": bool(p.get("is_profit_warning")),
                    "doc_url": p.get("doc_url"),
                    "stock_code": p.get("stock_code"),
                    "stock_name": p.get("stock_name"),
                } for p in payloads[:limit]]
                if not events:
                    unavailable.append(_unavailable(
                        "披露易本轮窗口内无该票号公告",
                        _HK_TICKER_NOTES.get(tick) or "口径为标题事件；头条数字" + R_HK_PDF,
                    ))
            elif region == "US":
                payloads = [p for p in _event_rows(s)
                            if p.get("source") == "sec-edgar-facts"
                            and str(p.get("ticker") or "").upper() == tick.upper()]
                payloads.sort(key=lambda p: (str(p.get("filed") or ""),
                                             str(p.get("report_date") or "")), reverse=True)
                events = [{
                    "source_used": "sec-edgar-facts",
                    "disclosure_date": p.get("filed"),
                    "as_of": p.get("report_date") or p.get("filed"),
                    "form": p.get("form"),
                    "report_date": p.get("report_date"),
                    "accession": p.get("accession"),
                    "doc_url": p.get("primary_doc_url"),
                } for p in payloads[:limit]]
                if not events:
                    unavailable.append(_unavailable(
                        "SEC EDGAR 本轮窗口内无该实体披露事件"))
            elif region == "JP":
                unavailable.append(_unavailable(R_EDINET, "事件面随 EDINET 车道一并待接"))
            elif region == "TW":
                unavailable.append(_unavailable(
                    R_TWSE, "TWSE OpenAPI 只有月营收快照，无披露事件端点；MOPS 被拦"))
            else:
                unavailable.append(_unavailable(
                    "该实体未注册事件车道",
                    "A股餐饮公司不发布月度经营数据公告；财报事件属既有 cnreport 面，"
                    "不在本工具的港股/美股事件车道内",
                ))
            return {
                "entity": brief,
                "count": len(events),
                "events": events,
                "unavailable": unavailable,
                "as_of": events[0]["disclosure_date"] if events else _today(),
            }
        except Exception as exc:  # noqa: BLE001
            return _session_failure(exc)
        finally:
            if s is not None:
                s.close()

    @mcp.tool(annotations=annotations_for("restaurant_company_financials"))
    def restaurant_company_financials(
        entity_id: int | None = None,
        ticker: str | None = None,
        concept: str | None = None,
        limit: int = 20,
    ) -> dict:
        """Read a restaurant company's quarterly financial observations.

        Concepts: ``financials.revenue``, ``financials.operating_profit`` and
        ``financials.net_income`` (a short name ``revenue`` /
        ``operating_profit`` / ``net_income`` is also accepted). Each point
        carries ``source_used`` (``sec-edgar-facts`` for US names, the
        existing A-share lane for CN) and ``as_of``. HK headline figures are
        only in announcement PDFs and are not structured; JP and TW lanes are
        not connected — those return an explicit ``unavailable`` block per
        concept with the established reason. SEC names below the 24-period
        gate (BROS; QSR net income; TXRH net income) are reported as
        ``期数不足未入库``, never as an empty series.

        Args:
            entity_id: Universe entity id (preferred when known).
            ticker: Universe ticker — ``MCD``, ``06862`` or ``06862.HK``.
            concept: Optional single concept (full code or short name); the
                default reads all three.
            limit: Points per concept, newest first (default 20, cap 200).

        Returns:
            ``{entity, concepts:[{concept, points, count}], unavailable,
            as_of, notes}``.
        """
        if entity_id is None and not ticker:
            return {"error": "missing_entity",
                    "detail": "pass entity_id or ticker (e.g. MCD or 06862)"}
        limit = max(1, min(int(limit), 200))
        codes: list[str]
        if concept:
            key = concept.strip()
            codes = [_FINANCIAL_SHORT.get(key, key)]
            if codes[0] not in FINANCIAL_CODES:
                return {"error": "unknown_concept",
                        "detail": f"未知概念 {concept!r}；允许 {list(FINANCIAL_CODES)}"}
        else:
            codes = list(FINANCIAL_CODES)

        s = None
        try:
            s = _session()
            row = _resolve_entity(s, entity_id, ticker)
            if row is None:
                return {"error": "entity_not_found",
                        "detail": f"未在 restaurant-universe 中找到 entity_id={entity_id!r} "
                                  f"ticker={ticker!r}"}
            brief = _entity_brief(row)
            meta = row.get("metadata_json") or {}
            region = meta.get("region")
            tick = str(meta.get("ticker") or row["code"])
            eid = int(row["id"])
            unavailable: list[dict] = []
            out_concepts: list[dict] = []

            if region in ("JP", "TW", "HK"):
                reason = {"JP": R_EDINET, "TW": R_TWSE, "HK": R_HK_PDF}[region]
                for code in codes:
                    out_concepts.append({"concept": code, "count": 0, "points": []})
                    unavailable.append(_unavailable(
                        reason, f"{code} 在该市场车道当前不可得"
                    ))
            else:
                obs_eid, got, sibling = _points_with_siblings(s, row, codes)
                by_concept: dict[str, list[dict]] = {c: [] for c in codes}
                for r in got:
                    by_concept.setdefault(str(r["concept_code"]), []).append(_fmt_point(r))
                short_gated = _SHORT_WINDOW_CONCEPTS.get(tick.upper(), set())
                for code in codes:
                    pts = by_concept.get(code, [])[:limit]
                    out_concepts.append({"concept": code, "count": len(pts), "points": pts})
                    if not pts:
                        if code in short_gated:
                            unavailable.append(_unavailable(
                                R_SHORT_WINDOW,
                                f"{tick} 的 {code} 期数低于 24 期入库门，未入库",
                            ))
                        else:
                            unavailable.append(_unavailable(
                                "该实体在库内无此概念观测",
                                f"{tick} × {code} 零行；源侧未覆盖，不插值不补零",
                            ))
            latest = max(
                (p["as_of"] for c in out_concepts for p in c["points"]),
                default=None,
            )
            out = {
                "entity": brief,
                "concepts": out_concepts,
                "unavailable": unavailable,
                "as_of": latest or _today(),
                "notes": [
                    "每点带 source_used 与 as_of；跨市场货币不同（USD / CNY），不做汇率换算",
                    f"港股头条数字：{R_HK_PDF}",
                    f"日股：{R_EDINET}",
                    f"台股：{R_TWSE}",
                ],
            }
            if region not in ("JP", "TW", "HK") and obs_eid != eid:
                alias = (sibling or {}).get("code") or f"#{obs_eid}"
                out["observations_from"] = {
                    "entity_id": obs_eid,
                    "code": alias,
                    "name_zh": (sibling or {}).get("name_zh"),
                    "name_matches": (sibling or {}).get("name_matches", True),
                    "entity_id_requested": eid,
                    "reason": "同码别名实体：公司改名/风险警示后爬虫另建实体，财务观测落在其上",
                }
                out["notes"].append(
                    f"观测取自同基础代码别名实体 {alias}"
                    f"（{out['observations_from']['name_zh'] or '名称未知'}）；"
                    "同一概念只取一个实体，未跨公司借数、未跨实体拼接"
                )
                if not out["observations_from"]["name_matches"]:
                    out["notes"].append(
                        f"别名实体名称与宇宙登记名不同（{brief.get('name_zh')} → "
                        f"{out['observations_from']['name_zh']}），按同交易所基础代码认定为同一上市公司（改名）"
                    )
            return out
        except Exception as exc:  # noqa: BLE001
            return _session_failure(exc)
        finally:
            if s is not None:
                s.close()

    @mcp.tool(annotations=annotations_for("restaurant_series"))
    def restaurant_series(
        code: str,
        source_used: str | None = None,
        entity_id: int | None = None,
    ) -> dict:
        """Read one macro / cost-chain series by concept code (read-only).

        Covers the curated restaurant series (NBS catering revenue, Census
        food services, FAO food price index, hog/pork prices, DCE meal and
        corn, IMF palm oil / sugar, USDA beef and cattle inventory, RPI,
        Eurostat catering HICP) plus the US CPI comparison pair
        ``price.cpi.us.food_away_from_home`` / ``price.cpi.us.food_at_home``.
        Every point carries ``source_used`` and ``as_of``. An unknown or
        empty series returns an explicit ``unavailable`` block; it is never an
        empty list pretending to be a coverage gap.

        Args:
            code: Concept code, e.g. ``retail.cn.catering.monthly`` or
                ``price.cpi.us.food_away_from_home``.
            source_used: Optional source pin (e.g. ``fred-cpi``) — useful when
                two sources coexist for one series.
            entity_id: Optional entity id when the series is registered for
                more than one entity (defaults to the stored rows' entity).

        Returns:
            ``{code, concept, entity, sources, count, start, end, points,
            as_of, caveats, unavailable}``.
        """
        s = None
        try:
            s = _session()
            concept_row = s.execute(
                text(
                    """
                    SELECT id, code, name_zh, name_en, unit, frequency, entity_type
                    FROM concepts WHERE code = :code
                    ORDER BY id LIMIT 1
                    """
                ),
                {"code": code.strip()},
            ).mappings().first()
            if concept_row is None:
                return {"code": code, "count": 0, "points": [],
                        "unavailable": [_unavailable(
                            "该序列未注册或不可得",
                            f"概念 {code!r} 不存在；本工具只读已注册概念",
                        )],
                        "as_of": _today()}

            sql = """
                SELECT o.date, o.value, o.unit, o.source_used, o.fetched_at,
                       o.entity_type, o.entity_id
                FROM semantic_observations o
                WHERE o.concept_id = :cid
            """
            params: dict[str, Any] = {"cid": int(concept_row["id"])}
            if source_used:
                sql += " AND o.source_used = :src"
                params["src"] = source_used
            if entity_id is not None:
                sql += " AND o.entity_id = :eid"
                params["eid"] = int(entity_id)
            sql += " ORDER BY o.date"
            rows = s.execute(text(sql), params).mappings().all()

            if not rows:
                return {
                    "code": concept_row["code"],
                    "concept": {"name_zh": concept_row["name_zh"],
                                "name_en": concept_row["name_en"],
                                "unit": concept_row["unit"],
                                "frequency": concept_row["frequency"]},
                    "count": 0,
                    "points": [],
                    "sources": [],
                    "unavailable": [_unavailable(
                        "该序列在本查询条件下无观测",
                        "不是真实缺测的断言——是当前 store 无行；"
                        "换 source_used / entity_id 或确认源是否在役",
                    )],
                    "as_of": _today(),
                }

            truncated = len(rows) > MAX_SERIES_ROWS
            kept = rows[-MAX_SERIES_ROWS:] if truncated else rows
            points = [
                {"date": r["date"], "value": _coerce_value(r["value"]),
                 "unit": r["unit"] or concept_row["unit"],
                 "source_used": r["source_used"], "as_of": r["date"],
                 "fetched_at": r["fetched_at"].isoformat() if r["fetched_at"] else None}
                for r in kept
            ]
            sources = sorted({str(r["source_used"]) for r in rows})
            entity_type, eid = rows[-1]["entity_type"], int(rows[-1]["entity_id"])
            entity_row = s.execute(
                text("SELECT code, name_zh, name_en FROM entities WHERE id = :eid"),
                {"eid": eid},
            ).mappings().first()

            caveats: list[str] = []
            if concept_row["code"] in _CPI_PAIR:
                other = _CPI_PAIR[concept_row["code"]]
                caveats.append(
                    f"对照对：{other}（CPI 外食 vs 在家就餐剪刀差）"
                )
                srcs = s.execute(
                    text(
                        """
                        SELECT o.source_used, min(o.date) AS lo, max(o.date) AS hi,
                               count(*) AS n
                        FROM semantic_observations o
                        JOIN concepts c ON c.id = o.concept_id
                        WHERE c.code = :code
                        GROUP BY o.source_used ORDER BY 1
                        """
                    ),
                    {"code": concept_row["code"]},
                ).all()
                for src, lo, hi, n in srcs:
                    caveats.append(
                        f"来源 {src} 窗口 {lo}..{hi}（{n} 期）"
                        + ("；DBnomics 镜像曾冻结在 2025-01，"
                           "fred-cpi 为后续月度的可用通道"
                           if src == "dbnomics-api" and hi.startswith("2025") else "")
                    )
            if concept_row["code"] == "retail.us.retail_food_services_total.monthly":
                caveats.append(
                    "MARTS 44X72 = 零售贸易 + 餐饮服务总计，不是单餐饮口径"
                )
            if concept_row["code"] == "agri.hog.price.spot":
                caveats.append("short_window(15)：周度短窗，满 24 周后自动退出短窗")
            if concept_row["code"] == "price.cpi.us.food_away_from_home":
                caveats.append("BLS 官方口 403；本序列经 DBnomics/fred 镜像")

            return {
                "code": concept_row["code"],
                "concept": {"name_zh": concept_row["name_zh"],
                            "name_en": concept_row["name_en"],
                            "unit": concept_row["unit"],
                            "frequency": concept_row["frequency"]},
                "entity": {"entity_type": entity_type, "entity_id": eid,
                           "code": entity_row["code"] if entity_row else None,
                           "name_zh": entity_row["name_zh"] if entity_row else None,
                           "name_en": entity_row["name_en"] if entity_row else None},
                "sources": sources,
                "count": len(points),
                "start": points[0]["date"] if points else None,
                "end": points[-1]["date"] if points else None,
                "points": points,
                "as_of": points[-1]["date"] if points else _today(),
                "caveats": caveats,
                "unavailable": [],
                "note": (f"窗口超过 {MAX_SERIES_ROWS} 点，仅返回最近 "
                         f"{MAX_SERIES_ROWS} 点；收窄窗口取其余"
                         if truncated else None),
            }
        except Exception as exc:  # noqa: BLE001
            return _session_failure(exc)
        finally:
            if s is not None:
                s.close()

    @mcp.tool(annotations=annotations_for("restaurant_sampling_notices"))
    def restaurant_sampling_notices(
        category: str | None = None,
        limit: int = 50,
    ) -> dict:
        """Read SAMR food-inspection notices (event plane only, read-only).

        Returns the individual notices from the SAMR sampling-notice feed
        (``source_used = samr-food-inspection``) with notice date, URL,
        result category and whether the notice is restaurant-related. This is
        an event plane: no monthly counts or rates are returned here, because
        an aggregate would be a read-time statistic, not an upstream
        observation series. A category with no rows is reported as an explicit
        no-match note, not as a fake zero series.

        Args:
            category: Optional ``result_category`` filter — ``unqualified``,
                ``qualified`` or ``other``.
            limit: Maximum notices, newest first (default 50, cap 500).

        Returns:
            ``{source_used, count, window, notices, as_of, notes}``.
        """
        allowed = {"unqualified", "qualified", "other"}
        if category and category.strip() not in allowed:
            return {"error": "unknown_category",
                    "detail": f"category={category!r}；允许 {sorted(allowed)}"}
        limit = max(1, min(int(limit), 500))
        s = None
        try:
            s = _session()
            rows = s.execute(
                text(
                    """
                    SELECT ci.payload
                    FROM crawl_items ci
                    JOIN crawl_runs r ON r.id = ci.run_id
                    WHERE r.source = 'samr-food-inspection'
                      AND r.status = 'success'
                    """
                )
            ).all()
            notices: list[dict] = []
            seen: set[str] = set()
            for (p,) in rows:
                if not isinstance(p, dict):
                    continue
                cat = p.get("result_category")
                if category and cat != category.strip():
                    continue
                url = str(p.get("url") or "")
                if url and url in seen:
                    continue
                if url:
                    seen.add(url)
                notices.append({
                    "source_used": "samr-food-inspection",
                    "notice_date": p.get("notice_date"),
                    "as_of": p.get("notice_date"),
                    "title": p.get("title"),
                    "url": url,
                    "result_category": cat,
                    "restaurant_related": bool(p.get("restaurant_related")),
                    "body_digest": p.get("body_digest"),
                })
            notices.sort(key=lambda n: str(n.get("notice_date") or ""), reverse=True)
            kept = notices[:limit]
            dates = [n["notice_date"] for n in notices if n.get("notice_date")]
            out: dict[str, Any] = {
                "source_used": "samr-food-inspection",
                "count": len(kept),
                "total_matched": len(notices),
                "window": {"start": min(dates) if dates else None,
                           "end": max(dates) if dates else None},
                "notices": kept,
                "as_of": max(dates) if dates else _today(),
                "notes": [
                    "事件面：只返回单条通告，不返回聚合时序/不合格率",
                    "result_category 为标题口径分类；restaurant_related 为标题+正文"
                    "+附件名的命中标记（宽口径）",
                    "公告条数 ≠ 不合格批次数；本工具不做两者换算",
                ],
            }
            if not notices:
                out["unavailable"] = [_unavailable(
                    "该类别在当前事件面无通告",
                    f"category={category!r} 命中 0 条；这是事件面事实，不是零值序列",
                )]
            return out
        except Exception as exc:  # noqa: BLE001
            return _session_failure(exc)
        finally:
            if s is not None:
                s.close()


def _today() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%d")
