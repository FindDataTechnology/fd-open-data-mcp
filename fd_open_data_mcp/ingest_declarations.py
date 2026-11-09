"""接入声明的读取与巡检（change: define-ingest-revision-and-coverage，任务 3.1–3.3）。

只读纪律
--------
本模块不写任何文件、不改任何数据源。它只回答三个问题：

  1. 哪些源已声明 `revision_policy` / `coverage_capability`？（已声明）
  2. 哪些源没声明？（待补——**这是绝大多数**，如实列出，不批量填假声明）
  3. 声明与行为是否一致？（只有跑过 `probe_*` 才能回答；没跑就明说没跑）

为什么"声明存在"不能算通过
------------------------
只核 manifest 文字，等于又造一个 `eventlog.set_status`：声明了却没人实现，巡检
全绿而问题全在。所以一致性核验必须打到读面上——对"声明提供缺失区间"的源，用
一个明显超出数据范围的窗口去读，`missing` 必须非空；返回"完整"就是撒谎。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

#: 允许的修订策略取值。新增值必须同时改本元组与 spec。
REVISION_POLICIES = ("overwrite_with_history", "batch_revision", "immutable")
#: 允许的覆盖能力声明取值。
COVERAGE_CAPABILITIES = ("full", "gaps_only", "none")

DECLARED_FIELDS = ("revision_policy", "coverage_capability")


@dataclass
class SourceDeclaration:
    name: str
    path: str
    revision_policy: str | None = None
    coverage_capability: str | None = None
    #: 声明值非法（不在允许集合内）——与"未声明"是两件事，不能混为一谈。
    invalid: dict[str, str] = field(default_factory=dict)

    @property
    def missing_fields(self) -> list[str]:
        return [f for f in DECLARED_FIELDS if getattr(self, f) is None]

    @property
    def declared(self) -> bool:
        return not self.missing_fields and not self.invalid

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "path": self.path,
            "revision_policy": self.revision_policy,
            "coverage_capability": self.coverage_capability,
            "missing_fields": self.missing_fields,
            "invalid": dict(self.invalid),
            "declared": self.declared,
        }


def _load_yaml(path: Path) -> dict[str, Any]:
    """惰性导入 yaml：运行时不因巡检而硬依赖 PyYAML。"""
    try:
        import yaml  # type: ignore
    except ModuleNotFoundError:  # pragma: no cover - venv 里有
        raise RuntimeError("需要 PyYAML 才能读取 YAML manifest（pip install pyyaml）")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: manifest 顶层不是对象")
    return data


def declaration_from_manifest(path: str | Path) -> SourceDeclaration:
    """从一份 manifest 读出声明位（缺失即 None，非法值单独记）。"""
    p = Path(path)
    data = _load_yaml(p)
    name = str(data.get("name") or p.parent.name)

    decl = SourceDeclaration(name=name, path=str(p))
    rev = data.get("revision_policy")
    cov = data.get("coverage_capability")
    if rev is not None:
        rev = str(rev)
        if rev in REVISION_POLICIES:
            decl.revision_policy = rev
        else:
            decl.invalid["revision_policy"] = rev
    if cov is not None:
        cov = str(cov)
        if cov in COVERAGE_CAPABILITIES:
            decl.coverage_capability = cov
        else:
            decl.invalid["coverage_capability"] = cov
    return decl


def load_declarations(paths: Iterable[str | Path]) -> list[SourceDeclaration]:
    return sorted((declaration_from_manifest(p) for p in paths), key=lambda d: d.name)


def glob_manifests(root: str | Path) -> list[Path]:
    """`<root>/*/manifest.yaml`（spiders 布局）；兼容 .yml / .json。"""
    base = Path(root)
    out: list[Path] = []
    for pattern in ("*/manifest.yaml", "*/manifest.yml"):
        out.extend(sorted(base.glob(pattern)))
    return out


def inspect(declarations: list[SourceDeclaration]) -> dict[str, Any]:
    """把清单分成三组。默认**不**对存量做任何修改，也不生成文件。"""
    declared = [d for d in declarations if d.declared]
    pending = [d for d in declarations if d.missing_fields and not d.invalid]
    invalid = [d for d in declarations if d.invalid]
    return {
        "total": len(declarations),
        "declared": [d.to_dict() for d in declared],
        "pending": [d.to_dict() for d in pending],
        "invalid": [d.to_dict() for d in invalid],
        "counts": {"declared": len(declared), "pending": len(pending),
                   "invalid": len(invalid)},
    }


def probe_coverage_consistency(    read_fn: Callable[..., dict],
    *,
    concept_id: int,
    entity_type: str,
    entity_id: int,
    window: tuple[str, str] = ("1900-01-01", "1900-01-05"),
    declared_capability: str | None = None,
) -> dict[str, Any]:
    """用"数据必然不存在的窗口"打一次读面，检验声明是否与行为一致。

    ``read_fn`` 需接受 ``(concept_id, entity_type, entity_id, start, end)`` 并返回
    带 ``coverage`` 的字典（即 ``dispatch.read_range_with_coverage`` 的单概念形式）。

    判定：
      * 读面根本没返回 ``coverage``/``degraded`` → 行为不一致（声明无从谈起）
      * 声明 ``full`` 却在空窗口返回 ``missing == []`` → **撒谎**
      * 声明 ``none`` → 不参与覆盖核验（它本就不承诺）
    """
    got = read_fn(concept_id, entity_type, entity_id, window[0], window[1])
    problems: list[str] = []
    coverage = (got or {}).get("coverage")
    degraded = (got or {}).get("degraded")
    if not isinstance(coverage, dict) or "missing" not in coverage:
        problems.append("读面未返回 coverage.missing：无法自证覆盖（声明了也没人实现）")
    if not isinstance(degraded, dict) or "degraded" not in degraded:
        problems.append("读面未返回 degraded：无法区分'没有数据'与'取不到数据'")
    if isinstance(coverage, dict) and declared_capability == "full":
        if not coverage.get("missing"):
            problems.append(
                "声明 coverage_capability=full，但在必然为空的窗口返回缺失区间为空"
                f"（window={window[0]}..{window[1]}）——声明与行为不一致"
            )
    return {
        "concept_id": concept_id, "window": list(window),
        "declared_capability": declared_capability,
        "consistent": not problems,
        "problems": problems,
    }


# ------------------------------------------------------------------ 分类（存量的补声明前置）

#: 存量源按可观测信号分桶；分桶只用来**建议**声明值，不写回任何 manifest。
BUCKETS = ("price_like", "period_series", "undetermined")


def classify_manifest(data: dict[str, Any]) -> dict[str, Any]:
    """按 manifest 里的事实给一个桶 + 建议声明值（**不写回**）。

    判据只用可观测信号，不做语义猜测：

      * `price_like`     concepts 里出现 price/close → 上游会在分红后整段重写
                         → 建议 `batch_revision`（整批留痕，逐点不存）
      * `period_series`  声明了 `data_period` → 周期序列，官方事后修订常见
                         → 建议 `overwrite_with_history`
      * `undetermined`   两者都没有 → **不给建议**，列进待人工读的清单

    覆盖能力一律建议 `gaps_only`：只有真的实现了区间缺口返回的源才该声明 `full`，
    而这一点必须由 `probe_coverage_consistency` 验证，不能靠分桶推断。
    """
    concepts = data.get("concepts") or []
    codes = [str(c.get("concept") or "") for c in concepts if isinstance(c, dict)]
    price_like = any(("price" in c) or ("close" in c) for c in codes)
    has_period = bool(data.get("data_period"))

    if price_like:
        # 价格类**不等于**前复权序列。2026-11-09 逐桶复核时发现：本仓现有 14 个
        # 价格源全是大宗/CPI（斗南花卉、Kitco、生猪、FRED CPI…），修订是逐点的，
        # 该用 overwrite_with_history。`batch_revision` 只适用于"上游会整段重写"
        # 的前复权股票序列——本仓当前没有，因此这里不猜，只标一个待人工确认。
        bucket, suggested = "price_like", {
            "revision_policy": "overwrite_with_history",
            "coverage_capability": "gaps_only",
        }
    elif has_period:
        bucket, suggested = "period_series", {
            "revision_policy": "overwrite_with_history", "coverage_capability": "gaps_only"}
    else:
        bucket, suggested = "undetermined", {}

    return {
        "bucket": bucket,
        "suggested": suggested,
        "signals": {"price_concepts": price_like, "data_period": has_period},
        "needs_human_read": bucket == "undetermined",
        # 价格类要额外确认一件事：上游是否整段重写（前复权）。是则 batch_revision。
        "review_forward_adjustment": price_like,
    }


def classify_paths(paths: Iterable[str | Path]) -> dict[str, Any]:
    """分桶汇总；每个桶给出计数与前若干个源名，供人工复核。"""
    buckets: dict[str, list[dict[str, Any]]] = {b: [] for b in BUCKETS}
    for p in paths:
        data = _load_yaml(Path(p))
        info = classify_manifest(data)
        buckets[info["bucket"]].append({
            "name": str(data.get("name") or Path(p).parent.name),
            "path": str(p),
            "suggested": info["suggested"],
        })
    return {
        "total": sum(len(v) for v in buckets.values()),
        "counts": {b: len(v) for b, v in buckets.items()},
        "buckets": {b: sorted(v, key=lambda x: x["name"]) for b, v in buckets.items()},
        "note": ("分类只给建议，不写回 manifest；`undetermined` 必须人工读，"
                 "`full` 不得由分桶推断——只能由 probe_coverage_consistency 验证"),
    }
