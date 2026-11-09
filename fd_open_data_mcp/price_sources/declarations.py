"""行情源声明：源成为一个可声明、可校验、可替换的登记事实。

设计（openspec/changes/add-price-supply-and-caliber/design.md D1）：

静态声明（覆盖 / 复权口径 / 因子来源 / 许可）随镜像走 YAML；**水位不由 YAML 声明**
——水位必须由数据算出来。写死的水位必然变成假绿，本项目反复踩过"配置值在启动期被
快照"的坑。故本模块只负责声明与校验，水位在 ``watermark.py`` 里从库派生。

三条硬约束（对应 spec ``price-source-registry``）：

1. 注册表是消费方解析源的**唯一**入口——读到未注册 id 必须失败，不得回落默认源。
2. 每个价格概念族必须**显式**指定首选源（D7）。排名隐式胜出对价格是危险的：排名一变，
   价格就悄悄换了复权口径，而报告上看不出来。
3. 许可状态以**拒绝**生效，不是警告。
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

SCHEMA_VERSION = "1"

#: 复权口径的封闭取值。``not_applicable`` 供指数使用——指数不做复权，但必须显式登记，
#: 不能让空值冒充"不适用"（空值与不适用的区别正是本仓最贵的一类混淆）。
ADJUSTMENTS = ("qfq", "hfq", "none", "not_applicable")

#: 锚点哨兵：每批抓取各自锚定在抓取时点的最新价上。
#: 这不是"没有锚点"，而是一个**已知有害**的口径——不同批次之间不可比，正是实测
#: 两源同日整差 1.00 元的根因。显式写出来，比留空更诚实。
ANCHOR_PER_FETCH = "per_fetch"

#: 许可状态。``provisional`` 指"可用但待持牌源替换"（如一次性种子导入的指数序列）：
#: 可内部消费，对外交付必须显式声明来源与待替换。
LICENSE_STATES = ("self_crawl", "licensed", "byok", "legacy", "provisional", "seed")

#: 访问协议。消费方按**协议**选实现，不按**源名**选——源名是数据身份，
#: 协议是取数方式。把两者混为一谈，换源就等于改代码。
ACCESS_KINDS = (
    "federated_view",          # 联邦视图（semantic_observations_all）+ 只读 DSN
    "fd_open_data_ontology",   # fd-open-data 本体读面（概念族 + 缓存）
    "polygon",                 # 外部行情 API
    "simulated",               # 确定性合成，测试与开发
)

#: 对外交付允许的许可状态。``seed``/``provisional`` 不在其中——它们只能内部消费。
DELIVERABLE_LICENSES = ("licensed", "byok", "self_crawl")

#: "临时源"：可内部消费，但对外引用必须附带来源与"待替换"声明（闸门在 ``delivery``）。
NOT_REPLACED_STATES = ("provisional", "seed")

#: 对外交付覆盖。``auto`` = 按许可种别推演（默认，即现状）；``deliverable`` / ``internal_only``
#: 是显式覆盖，用于「许可种别只是出处描述，交付资格是商业决定」这一真实情形。
#:
#: 为什么要有这一层：A 股价格目前全在 ``legacy`` 表里。按种别推演它是"不可对客"，
#: 于是整个 A 股投研对客是空的；但"能不能对客"是执照与合同问题，不是数据出处问题。
#: 把它做成声明字段，决定就落在 YAML 一行上，而不是改代码——**代码不该替业务做这个决定**。
DELIVERY_MODES = ("auto", "deliverable", "internal_only")


class DeclarationError(ValueError):
    """声明本身不合法（缺字段、取值越界、族被多个源同时声明）。"""


class SourceNotRegistered(KeyError):
    """请求了一个不在注册表中的 source_id。"""

    def __init__(self, source_id: str, known: Sequence[str]):
        self.source_id = source_id
        self.known = tuple(known)
        super().__init__(
            f"source_id={source_id!r} is not registered "
            f"(registered: {', '.join(self.known) or '<none>'})"
        )


class FamilyNotClaimed(KeyError):
    """价格概念族没有唯一首选源——按 D7 必须拒绝读取，不得回落排名。"""

    def __init__(self, family: str, claimants: Sequence[str]):
        self.family = family
        self.claimants = tuple(claimants)
        detail = (
            f"claimed by multiple sources ({', '.join(self.claimants)})"
            if self.claimants
            else "claimed by no source"
        )
        super().__init__(
            f"price family {family!r} has no unique preferred source: {detail}; "
            "declare it under exactly one source's preferred_for"
        )


class NotDeliverable(PermissionError):
    """该源不得用于对外交付（许可状态不在可交付集合内）。"""


@dataclass(frozen=True)
class AccessSpec:
    """取数方式——按**协议**而不是源名选择实现。

    ``kind`` 必须属于 :data:`ACCESS_KINDS`；``dsn_env`` 是读只读 DSN 的环境变量名
    （协议需要凭据时）。消费方据 ``kind`` 选 provider 类，据 ``dsn_env`` 取凭据
    ——两者都不含源名，换源不必改代码。
    """

    kind: str
    dsn_env: str | None = None

    @classmethod
    def from_dict(cls, source_id: str, raw: Mapping[str, Any] | None) -> "AccessSpec":
        if not raw:
            raise DeclarationError(f"{source_id}: access block is required")
        kind = str(raw.get("kind") or "")
        if kind not in ACCESS_KINDS:
            raise DeclarationError(
                f"{source_id}: access.kind={kind!r} not in {ACCESS_KINDS}"
            )
        dsn_env = raw.get("dsn_env")
        if kind == "federated_view" and not dsn_env:
            raise DeclarationError(
                f"{source_id}: access.kind=federated_view requires dsn_env"
            )
        return cls(kind=kind, dsn_env=str(dsn_env) if dsn_env else None)

    def to_dict(self) -> dict:
        return {"kind": self.kind, "dsn_env": self.dsn_env}


@dataclass(frozen=True)
class WatermarkProbe:
    """水位探针：怎么为这个源取一个「已证可用」的最新日期。

    刻意不用全窗 ``group by date``——实测全市场全窗扫描 >200s 超时，参照篮
    （5 只 × 全年平日）4.9s/年。参照篮给出的是**下界**，不是真实最大值；这个方向
    是安全的：低估水位只会让陈旧更早被发现，不会把陈旧冒充成新鲜。
    """

    entity_type: str
    concept_codes: tuple[str, ...]
    reference_basket: tuple[str, ...]
    lookback_days: int = 400

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "WatermarkProbe":
        basket = tuple(raw.get("reference_basket") or ())
        codes = tuple(raw.get("concept_codes") or ())
        if not basket:
            raise DeclarationError("watermark.reference_basket must be non-empty")
        if not codes:
            raise DeclarationError("watermark.concept_codes must be non-empty")
        return cls(
            entity_type=str(raw.get("entity_type") or "stock"),
            concept_codes=codes,
            reference_basket=basket,
            lookback_days=int(raw.get("lookback_days") or 400),
        )


@dataclass(frozen=True)
class SourceDeclaration:
    source_id: str
    instrument_types: tuple[str, ...]
    markets: tuple[str, ...]
    adjustment: str
    frequency: str
    license_state: str
    watermark_probe: WatermarkProbe
    access: AccessSpec
    adjustment_anchor: str | None = None
    factor_source: str | None = None
    preferred_for: tuple[str, ...] = ()
    #: 对外交付覆盖（``auto`` / ``deliverable`` / ``internal_only``）。
    delivery: str = "auto"
    #: 库内 ``source_used`` 的实际取值。默认与 ``source_id`` 相同；口径变体
    #: （``<id>@<anchor>``）必须显式指回基名，否则水位查询会去查一个库里不存在的源。
    db_source: str | None = None
    notes: str = ""
    extra: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, source_id: str, raw: Mapping[str, Any]) -> "SourceDeclaration":
        adjustment = str(raw.get("adjustment") or "")
        if adjustment not in ADJUSTMENTS:
            raise DeclarationError(
                f"{source_id}: adjustment={adjustment!r} not in {ADJUSTMENTS}"
            )
        license_state = str(raw.get("license_state") or "")
        if license_state not in LICENSE_STATES:
            raise DeclarationError(
                f"{source_id}: license_state={license_state!r} not in {LICENSE_STATES}"
            )
        anchor = raw.get("adjustment_anchor")
        if adjustment in ("qfq", "hfq"):
            # 复权源必须交代锚点：换锚点等于换口径，没有锚点就无法判断两批数据可比不可比。
            if not anchor:
                raise DeclarationError(
                    f"{source_id}: adjustment={adjustment} requires adjustment_anchor "
                    f"(an ISO date, or {ANCHOR_PER_FETCH!r} when each batch is anchored "
                    "at its own fetch time)"
                )
            if anchor != ANCHOR_PER_FETCH:
                _parse_date(source_id, anchor, "adjustment_anchor")
        elif anchor:
            raise DeclarationError(
                f"{source_id}: adjustment={adjustment} must not declare adjustment_anchor"
            )

        instrument_types = tuple(raw.get("instrument_types") or ())
        if not instrument_types:
            raise DeclarationError(f"{source_id}: instrument_types must be non-empty")

        delivery = str(raw.get("delivery") or "auto")
        if delivery not in DELIVERY_MODES:
            raise DeclarationError(
                f"{source_id}: delivery={delivery!r} not in {DELIVERY_MODES}"
            )

        return cls(
            source_id=source_id,
            instrument_types=instrument_types,
            markets=tuple(raw.get("markets") or ()),
            adjustment=adjustment,
            frequency=str(raw.get("frequency") or "daily"),
            license_state=license_state,
            watermark_probe=WatermarkProbe.from_dict(raw.get("watermark") or {}),
            access=AccessSpec.from_dict(source_id, raw.get("access")),
            adjustment_anchor=anchor,
            factor_source=raw.get("factor_source"),
            preferred_for=tuple(raw.get("preferred_for") or ()),
            delivery=delivery,
            db_source=str(raw.get("db_source") or source_id),
            notes=str(raw.get("notes") or ""),
            extra={k: v for k, v in raw.items() if k not in _KNOWN_KEYS},
        )

    @property
    def deliverable(self) -> bool:
        """能否对外交付。

        ``delivery`` 非 ``auto`` 时以显式声明为准——这是有意的：许可种别说的是
        "数据从哪来"，交付资格说的是"我们有权给客户看吗"，两者不必然同向。
        """
        if self.delivery == "deliverable":
            return True
        if self.delivery == "internal_only":
            return False
        return self.license_state in DELIVERABLE_LICENSES

    @property
    def caliber(self) -> str:
        """口径标识：``(source_id, adjustment, anchor)`` 的稳定文本形态。

        换锚点在声明层就是换这个字符串——不是原地改写历史行。
        """
        return f"{self.source_id}|{self.adjustment}|{self.adjustment_anchor or '-'}"

    def to_dict(self) -> dict:
        return {
            "source_id": self.source_id,
            "instrument_types": list(self.instrument_types),
            "markets": list(self.markets),
            "adjustment": self.adjustment,
            "adjustment_anchor": self.adjustment_anchor,
            "db_source": self.db_source,
            "factor_source": self.factor_source,
            "frequency": self.frequency,
            "license_state": self.license_state,
            "deliverable": self.deliverable,
            "preferred_for": list(self.preferred_for),
            "delivery": self.delivery,
            "access": self.access.to_dict(),
            "caliber": self.caliber,
            "notes": self.notes,
        }


_KNOWN_KEYS = frozenset(
    {
        "instrument_types", "markets", "adjustment", "adjustment_anchor",
        "factor_source", "frequency", "license_state", "preferred_for",
        "db_source", "notes", "watermark", "access", "delivery",
    }
)


def _parse_date(source_id: str, value: Any, field_name: str) -> dt.date:
    try:
        return dt.date.fromisoformat(str(value))
    except ValueError as exc:
        raise DeclarationError(
            f"{source_id}: {field_name}={value!r} is not an ISO date"
        ) from exc


def _split_caliber_keys(raw: Mapping[str, Any]) -> dict[str, Any]:
    """拆分「口径键」与普通源键。

    同一个 ``source_id`` 可以有多套口径（换锚点 = 换口径），故声明文件的
    ``sources`` 下每个 id 要么是一份 declaration，要么是 ``calibers`` 下的一组
    declaration。后者展开成 ``<id>@<anchor|label>`` 形式的独立 source_id，
    使"同一源两个锚点"在注册表里是两个可分辨的源，而不是一个源被改写。
    """
    out: dict[str, Any] = {}
    for sid, body in (raw or {}).items():
        if isinstance(body, Mapping) and "calibers" in body:
            base = {k: v for k, v in body.items() if k != "calibers"}
            for label, cal in (body.get("calibers") or {}).items():
                merged = {**base, **(cal or {})}
                out[f"{sid}@{label}"] = merged
        else:
            out[str(sid)] = body
    return out


@dataclass(frozen=True)
class SourceRegistry:
    schema_version: str
    sources: Mapping[str, SourceDeclaration]
    price_families: tuple[str, ...]
    _family_owner: Mapping[str, str] = field(default_factory=dict)

    # ---- 解析 -------------------------------------------------------------

    def get(self, source_id: str) -> SourceDeclaration:
        """按 id 取源；未注册即失败，绝不回落默认源（spec: Unregistered source is refused）。"""
        try:
            return self.sources[source_id]
        except KeyError:
            raise SourceNotRegistered(source_id, sorted(self.sources)) from None

    def resolve_for_family(self, family: str) -> SourceDeclaration:
        """取某价格概念族的首选源（D7：必须显式，不得靠排名隐式胜出）。"""
        owner = self._family_owner.get(family)
        if owner is None:
            claimants = sorted(
                sid for sid, d in self.sources.items() if family in d.preferred_for
            )
            raise FamilyNotClaimed(family, claimants)
        return self.sources[owner]

    def assert_deliverable(self, source_id: str) -> SourceDeclaration:
        """对外交付前的许可闸：以拒绝生效，不是警告。"""
        decl = self.get(source_id)
        if not decl.deliverable:
            raise NotDeliverable(
                f"source {source_id!r} has license_state={decl.license_state!r}; "
                f"deliverable states are {DELIVERABLE_LICENSES}"
            )
        return decl

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self.sources))

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "price_families": list(self.price_families),
            "sources": [d.to_dict() for d in self.sources.values()],
        }


def load_registry(path: str | Path | None = None) -> SourceRegistry:
    """加载并校验声明文件。任何不一致都在这里失败，不留到运行时。"""
    p = Path(path) if path else DEFAULT_DECLARATIONS_PATH
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}

    version = str(raw.get("schema_version") or "")
    if version != SCHEMA_VERSION:
        raise DeclarationError(
            f"{p}: schema_version={version!r}, expected {SCHEMA_VERSION!r}"
        )

    families = tuple(raw.get("price_families") or ())
    if not families:
        raise DeclarationError(f"{p}: price_families must be non-empty")

    sources = {
        sid: SourceDeclaration.from_dict(sid, body or {})
        for sid, body in _split_caliber_keys(raw.get("sources") or {}).items()
    }
    if not sources:
        raise DeclarationError(f"{p}: sources must be non-empty")

    # D7：每个价格族恰好一个首选源。重复声明是**歧义**，不是"后来者覆盖"。
    owner: dict[str, str] = {}
    for sid, decl in sources.items():
        for fam in decl.preferred_for:
            if fam not in families:
                raise DeclarationError(
                    f"{sid}: preferred_for={fam!r} not in price_families {families}"
                )
            if fam in owner:
                raise DeclarationError(
                    f"price family {fam!r} claimed by both {owner[fam]!r} and {sid!r}; "
                    "exactly one preferred source is required"
                )
            owner[fam] = sid

    return SourceRegistry(
        schema_version=version,
        sources=sources,
        price_families=families,
        _family_owner=owner,
    )


DEFAULT_DECLARATIONS_PATH = Path(__file__).with_name("declarations.yaml")
