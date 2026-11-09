"""行情源注册表（openspec change: add-price-supply-and-caliber, tasks 1.1-1.5）。

测的是**拒绝**：未注册源、歧义首选源、不可交付许可、锚点缺失。这些才是这个能力存在的
理由——它能通过的那些事，一个字典也能做到。
"""
from __future__ import annotations

import datetime as dt
import textwrap

import pytest

from fd_open_data_mcp.models import Concept, Entity, SemanticObservation
from fd_open_data_mcp.price_sources import (
    DeclarationError,
    FamilyNotClaimed,
    NotDeliverable,
    SourceNotRegistered,
    load_registry,
    resolve_for_family,
    resolve_source,
)
from fd_open_data_mcp.price_sources.declarations import ANCHOR_PER_FETCH, AccessSpec
from fd_open_data_mcp.price_sources.watermark import (
    NO_DATA,
    METHOD_BASKET,
    derive_watermark,
    derive_watermarks,
)

MINIMAL = """
schema_version: "1"
price_families: [price.stock.daily]
sources:
  s1:
    instrument_types: [stock]
    markets: [XSHG]
    adjustment: qfq
    adjustment_anchor: "2026-07-10"
    frequency: daily
    license_state: legacy
    preferred_for: [price.stock.daily]
    access:
      kind: federated_view
      dsn_env: ASTOCK_DSN
    watermark:
      entity_type: stock
      concept_codes: [price.close]
      reference_basket: ["600000"]
"""


def _write(tmp_path, body: str):
    p = tmp_path / "decl.yaml"
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


# --------------------------------------------------------------- 1.1 schema


def test_packaged_registry_loads_and_is_complete():
    reg = load_registry()
    assert {"astock_daily", "akshare", "index-seed"} <= set(reg.ids())
    assert reg.schema_version == "1"


def test_wrong_schema_version_is_rejected(tmp_path):
    p = _write(tmp_path, MINIMAL.replace('"1"', '"9"'))
    with pytest.raises(DeclarationError, match="schema_version"):
        load_registry(p)


def test_missing_basket_is_rejected(tmp_path):
    p = _write(tmp_path, MINIMAL.replace('      reference_basket: ["600000"]\n', ""))
    with pytest.raises(DeclarationError, match="reference_basket"):
        load_registry(p)


def test_qfq_without_anchor_is_rejected(tmp_path):
    body = MINIMAL.replace('    adjustment_anchor: "2026-07-10"\n', "")
    with pytest.raises(DeclarationError, match="requires adjustment_anchor"):
        load_registry(_write(tmp_path, body))


def test_anchor_on_non_adjusted_source_is_rejected(tmp_path):
    body = MINIMAL.replace("adjustment: qfq", "adjustment: none")
    with pytest.raises(DeclarationError, match="must not declare adjustment_anchor"):
        load_registry(_write(tmp_path, body))


def test_anchor_per_fetch_is_allowed_and_is_not_a_date():
    reg = load_registry()
    decl = reg.get("akshare")
    assert decl.adjustment_anchor == ANCHOR_PER_FETCH
    # 口径文本必须把哨兵原样带出——"每批各自锚定"是有害事实，不能渲染成空。
    assert ANCHOR_PER_FETCH in decl.caliber


# ------------------------------------------------------- 1.5 unique preferred


def test_duplicate_preferred_source_is_rejected(tmp_path):
    body = MINIMAL + """  s2:
    instrument_types: [stock]
    markets: [XSHG]
    adjustment: qfq
    adjustment_anchor: "2026-07-10"
    frequency: daily
    license_state: legacy
    preferred_for: [price.stock.daily]
    access:
      kind: federated_view
      dsn_env: ASTOCK_DSN
    watermark:
      entity_type: stock
      concept_codes: [price.close]
      reference_basket: ["600000"]
"""
    with pytest.raises(DeclarationError, match="claimed by both"):
        load_registry(_write(tmp_path, body))


def test_unknown_family_in_preferred_for_is_rejected(tmp_path):
    body = MINIMAL.replace("preferred_for: [price.stock.daily]", "preferred_for: [price.fund.daily]")
    with pytest.raises(DeclarationError, match="not in price_families"):
        load_registry(_write(tmp_path, body))


def test_unclaimed_family_refuses_resolution():
    """没有首选源的族必须拒绝读取，不得回落到"排名最高的那个"。"""
    reg = load_registry()
    assert "price.fund.daily" not in reg.price_families
    with pytest.raises(FamilyNotClaimed, match="claimed by no source"):
        reg.resolve_for_family("price.fund.daily")


def test_ackshare_does_not_claim_the_stock_family():
    """同族第二个源只作对照，不得与首选源争抢（D7）。"""
    reg = load_registry()
    assert reg.get("akshare").preferred_for == ()
    assert reg.resolve_for_family("price.stock.daily").source_id == "astock_daily"


def test_unregistered_source_is_refused_without_fallback():
    reg = load_registry()
    with pytest.raises(SourceNotRegistered, match="not registered"):
        reg.get("does-not-exist")


def test_delivery_gate_refuses_seed_and_legacy():
    reg = load_registry()
    with pytest.raises(NotDeliverable, match="license_state"):
        reg.assert_deliverable("index-seed")
    with pytest.raises(NotDeliverable, match="license_state"):
        reg.assert_deliverable("astock_daily")


# --------------------------------------------------------- 1.2 watermarks


@pytest.fixture
def seeded(session):
    """一只参照篮标的 + 价格概念 + 两天的观测。"""
    c = Concept(code="price.close", entity_type="stock", frequency="daily", unit="CNY")
    e = Entity(entity_type="stock", code="600000", name_zh="浦发银行")
    session.add_all([c, e])
    session.flush()
    session.add_all(
        [
            SemanticObservation(
                concept_id=c.id, entity_type="stock", entity_id=e.id,
                date="2026-07-09", granularity="day", value="8.90",
                source_used="astock_daily",
            ),
            SemanticObservation(
                concept_id=c.id, entity_type="stock", entity_id=e.id,
                date="2026-07-10", granularity="day", value="9.06",
                source_used="astock_daily",
            ),
            SemanticObservation(
                concept_id=c.id, entity_type="stock", entity_id=e.id,
                date="2026-07-10", granularity="day", value="10.06",
                source_used="akshare",
            ),
        ]
    )
    session.commit()
    return session


def test_watermark_is_max_date_per_source(seeded):
    mark = derive_watermark(
        seeded,
        source_id="astock_daily",
        entity_type="stock",
        concept_codes=["price.close"],
        reference_basket=["600000"],
        today=dt.date(2026, 7, 20),
    )
    assert mark.status == "ok"
    assert mark.as_of == "2026-07-10"
    assert mark.method == METHOD_BASKET


def test_watermark_is_per_source_not_pooled(seeded):
    """两个源同一天两行：各自的水位与值互不影响。"""
    both = derive_watermarks(
        seeded,
        {
            "astock_daily": _decl("astock_daily"),
            "akshare": _decl("akshare"),
        },
        today=dt.date(2026, 7, 20),
    )
    assert both["astock_daily"].as_of == "2026-07-10"
    assert both["akshare"].as_of == "2026-07-10"
    assert both["astock_daily"].source_id == "astock_daily"


def test_unresolvable_basket_is_no_data_not_ok(session):
    """篮内标的在库里找不到：这是"声明与库对不上"，不许报成 ok。"""
    mark = derive_watermark(
        session,
        source_id="astock_daily",
        entity_type="stock",
        concept_codes=["price.close"],
        reference_basket=["999999"],
        today=dt.date(2026, 7, 20),
    )
    assert mark.status == NO_DATA
    assert mark.as_of is None
    assert "disagree" in (mark.detail or "")


def test_empty_window_is_no_data(session):
    mark = derive_watermark(
        session,
        source_id="astock_daily",
        entity_type="stock",
        concept_codes=["price.close"],
        reference_basket=["600000"],
        today=dt.date(2026, 7, 20),
    )
    assert mark.status == NO_DATA
    assert mark.as_of is None


def test_basket_scoped_watermark_does_not_see_other_symbols(seeded):
    """参照篮给出的是下界：篮外标的有更新数据，也不抬高这个水位。"""
    e2 = Entity(entity_type="stock", code="000001", name_zh="平安银行")
    seeded.add(e2)
    seeded.flush()
    cid = seeded.query(Concept.id).filter(Concept.code == "price.close").scalar()
    seeded.add(
        SemanticObservation(
            concept_id=cid, entity_type="stock", entity_id=e2.id,
            date="2026-07-15", granularity="day", value="11.11",
            source_used="astock_daily",
        )
    )
    seeded.commit()
    mark = derive_watermark(
        seeded,
        source_id="astock_daily",
        entity_type="stock",
        concept_codes=["price.close"],
        reference_basket=["600000"],
        today=dt.date(2026, 7, 20),
    )
    assert mark.as_of == "2026-07-10"


# ------------------------------------------------------- 1.4 caliber variants


def test_caliber_variant_keeps_its_own_identity_but_reads_base_db_source(seeded):
    """同一源换锚点＝新口径 id，但水位查的还是库里那个基名。"""
    variants = {
        "astock_daily@2026-07-10": _decl("astock_daily", anchor="2026-07-10"),
        "astock_daily@per_fetch": _decl(
            "astock_daily@per_fetch", db_source="astock_daily", anchor=ANCHOR_PER_FETCH
        ),
    }
    marks = derive_watermarks(seeded, variants, today=dt.date(2026, 7, 20))
    assert set(marks) == set(variants)
    assert all(m.as_of == "2026-07-10" for m in marks.values())
    # 身份仍是两个：换锚点不是原地改写。
    assert marks["astock_daily@2026-07-10"].source_id != marks["astock_daily@per_fetch"].source_id


def _decl(source_id: str, *, anchor: str = "2026-07-10", db_source: str | None = None):
    from fd_open_data_mcp.price_sources.declarations import SourceDeclaration, WatermarkProbe

    return SourceDeclaration(
        source_id=source_id,
        instrument_types=("stock",),
        markets=("XSHG",),
        adjustment="qfq",
        frequency="daily",
        license_state="legacy",
        watermark_probe=WatermarkProbe(
            entity_type="stock",
            concept_codes=("price.close",),
            reference_basket=("600000",),
            lookback_days=400,
        ),
        access=AccessSpec(kind="federated_view", dsn_env="ASTOCK_DSN"),
        adjustment_anchor=anchor,
        db_source=db_source or source_id,
    )


# ------------------------------------------------------- 1.3 merged listing


def _seed_close(session, entity_code: str, date: str, value: str, source: str):
    from fd_open_data_mcp.models import Concept, Entity, SemanticObservation

    c = session.query(Concept).filter(Concept.code == "price.close").first()
    if c is None:
        c = Concept(code="price.close", entity_type="stock", frequency="daily", unit="CNY")
        session.add(c)
        session.flush()
    e = session.query(Entity).filter(Entity.code == entity_code).first()
    if e is None:
        e = Entity(entity_type="stock", code=entity_code, name_zh=entity_code)
        session.add(e)
        session.flush()
    session.add(
        SemanticObservation(
            concept_id=c.id, entity_type="stock", entity_id=e.id,
            date=date, granularity="day", value=value, source_used=source,
        )
    )
    session.commit()


def test_listing_merges_declaration_and_live_watermark(session, tmp_path, monkeypatch):
    """声明字段与实时水位同一次查询给出；水位随库变化（不缓存）。"""
    monkeypatch.setenv("FD_OPEN_DATA_MCP_DATABASE_URL", f"sqlite:///{tmp_path/'list.db'}")
    _seed_close(session, "600000", "2026-07-09", "8.90", "astock_daily")

    from fd_open_data_mcp.price_sources import list_price_sources

    first = list_price_sources(source_ids=["astock_daily"], today=dt.date(2026, 7, 20))
    row = first["sources"][0]
    assert row["source_id"] == "astock_daily"
    assert row["adjustment"] == "qfq"
    assert row["watermark"]["as_of"] == "2026-07-09"
    assert row["watermark"]["method"] == METHOD_BASKET

    # 库前移一天 → 水位必须跟着动（水位不缓存，这是"三路读同一水位"的前提）
    _seed_close(session, "600000", "2026-07-10", "9.06", "astock_daily")
    second = list_price_sources(source_ids=["astock_daily"], today=dt.date(2026, 7, 20))
    assert second["sources"][0]["watermark"]["as_of"] == "2026-07-10"


def test_listing_refuses_unregistered_id(tmp_path, monkeypatch, session):
    monkeypatch.setenv("FD_OPEN_DATA_MCP_DATABASE_URL", f"sqlite:///{tmp_path/'bad.db'}")
    from fd_open_data_mcp.price_sources import list_price_sources

    with pytest.raises(SourceNotRegistered):
        list_price_sources(source_ids=["astock_daily", "typo-source"])


# --------------------------------------------- delivery 覆盖（把决定留在声明里）


def test_delivery_auto_follows_license_state():
    """默认 auto：按许可种别推演，行为与引入该字段之前完全一致。"""
    reg = load_registry()
    assert reg.get("astock_daily").delivery == "auto"
    assert reg.get("astock_daily").deliverable is False   # legacy → 不可交付
    assert reg.get("index-seed").deliverable is False     # provisional → 不可交付


def test_delivery_override_can_grant_legacy(tmp_path):
    """显式 deliverable 覆盖许可种别——"能不能对客"是商业决定，不是数据出处问题。"""
    body = MINIMAL.replace(
        "    license_state: legacy\n", "    license_state: legacy\n    delivery: deliverable\n"
    )
    reg = load_registry(_write(tmp_path, body))
    assert reg.get("s1").deliverable is True


def test_delivery_override_can_revoke_a_deliverable_state(tmp_path):
    """反向也要能收：许可上是 self_crawl，但合同不允许对客时显式 internal_only。"""
    body = MINIMAL.replace(
        "    license_state: legacy\n",
        "    license_state: self_crawl\n    delivery: internal_only\n",
    )
    reg = load_registry(_write(tmp_path, body))
    assert reg.get("s1").deliverable is False


def test_unknown_delivery_mode_is_rejected(tmp_path):
    body = MINIMAL.replace(
        "    license_state: legacy\n", "    license_state: legacy\n    delivery: maybe\n"
    )
    with pytest.raises(DeclarationError, match="delivery="):
        load_registry(_write(tmp_path, body))


def test_override_is_recorded_in_the_listing():
    """覆盖必须可见：否则"为什么这个源能对客"在输出里看不出来出处。"""
    reg = load_registry()
    row = reg.get("astock_daily").to_dict()
    assert row["delivery"] == "auto"
    assert row["deliverable"] is False


# --------------------------------------------------- 打包完整性（发版前必验）


def test_package_data_declares_the_yaml():
    """`declarations.yaml` 必须进 wheel。

    实测过的失败形态：未声明 package-data 时 wheel 里 **0 个 yaml**，而 `.py` 全在——
    装到干净环境后 `load_registry()` 直接 FileNotFoundError，且只在**发版之后**才暴露。
    这条静态断言把那次事故钉住，删掉 package-data 就红。
    """
    import pathlib
    import tomllib

    root = pathlib.Path(__file__).resolve().parent.parent
    cfg = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    pkg_data = cfg.get("tool", {}).get("setuptools", {}).get("package-data", {})
    declared = pkg_data.get("fd_open_data_mcp.price_sources") or []
    assert any(p.endswith(".yaml") for p in declared), (
        "pyproject.toml 的 [tool.setuptools.package-data] 必须为 "
        "fd_open_data_mcp.price_sources 声明 *.yaml，否则 wheel 不含声明文件，"
        f"装上去 load_registry() 必然失败。当前声明：{declared}"
    )


def test_declarations_yaml_sits_next_to_the_module():
    """声明文件必须与模块同目录（load_registry 按 __file__ 相对定位）。"""
    import pathlib

    from fd_open_data_mcp.price_sources import declarations as d

    assert (pathlib.Path(d.__file__).parent / "declarations.yaml").is_file()
