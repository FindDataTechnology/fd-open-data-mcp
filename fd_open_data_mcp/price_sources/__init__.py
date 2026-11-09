"""行情源供给层（openspec change: add-price-supply-and-caliber）。

三个子模块：

- ``declarations`` — 源声明 schema、YAML 加载、启动期校验（含 D7 的唯一首选源约束）
- ``watermark``    — 水位由数据派生（参照篮，只读）
- ``registry``     — 对外唯一出口：声明 + 实时水位合并

水位刻意不缓存：缓存水位正是"研究/交易/结算三路读到三个水位"的来源。
"""
from fd_open_data_mcp.price_sources.declarations import (  # noqa: F401
    ACCESS_KINDS,
    ADJUSTMENTS,
    DELIVERABLE_LICENSES,
    LICENSE_STATES,
    NOT_REPLACED_STATES,
    SCHEMA_VERSION,
    AccessSpec,
    DeclarationError,
    FamilyNotClaimed,
    NotDeliverable,
    SourceDeclaration,
    SourceNotRegistered,
    SourceRegistry,
    WatermarkProbe,
    load_registry,
)
from fd_open_data_mcp.price_sources.watermark import (  # noqa: F401
    NO_DATA,
    UNREACHABLE,
    Watermark,
    derive_watermark,
    derive_watermarks,
)
from fd_open_data_mcp.price_sources.registry import (  # noqa: F401
    assert_deliverable,
    list_price_sources,
    resolve_for_family,
    resolve_source,
    watermark_for,
)

from fd_open_data_mcp.price_sources.index_seed import (  # noqa: F401
    DEFAULT_SOURCE_ID,
    INDEX_CONCEPT_MAP,
    SeedReport,
    SeedRowError,
    ensure_index_concepts,
    ensure_index_entity,
    import_index_seed,
    normalize_index_symbol,
    read_seed_csv,
    seed_watermarks,
)
from fd_open_data_mcp.price_sources.delivery import (  # noqa: F401
    DeliveryAttribution,
    DeliveryAttributionMissing,
    LicensedAdapterNotConfigured,
    LicensedPriceAdapter,
    assert_delivery_allowed,
    register_licensed_adapter,
    required_attribution,
    resolve_licensed_adapter,
)
