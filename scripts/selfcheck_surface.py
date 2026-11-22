#!/usr/bin/env python3
"""tool-surface manifest 双向 selfcheck（mcp-surface-contract）。

fd-open-data-mcp 有主面（87 工具，本仓默认 server）与公开只读切面（
`tool-surface.public.json`，FD_OPEN_DATA_FACE=public，14 工具）两套。本脚本只断言
**主面** manifest（仓根 `tool-surface.json`）与默认 server 的 `list_tools()` 双向一致；
公开切面的闭合由 market-public 部署形态另行核。

用法（在 fd-open-data-mcp 下）：python3 scripts/selfcheck_surface.py
退出码：0 通过 / 1 失败 / 2 判不出（依赖未装）。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MANIFEST = REPO / "tool-surface.json"


def fail(problems, code=1):
    print("SELFCHECK FAIL:")
    for p in problems:
        print(f"  - {p}")
    sys.exit(code)


def main() -> None:
    if not MANIFEST.exists():
        fail([f"manifest 缺失：{MANIFEST}（契约：缺失即未上牌）"])
    m = json.loads(MANIFEST.read_text(encoding="utf-8"))
    levels: dict[str, str] = {}
    for g in m.get("tool_groups", []):
        level = g.get("level")
        if level not in ("commercial", "internal"):
            fail([f"tool_groups.{g.get('group')} 的 level 非法：{level!r}"])
        for t in g.get("tools", []):
            if t in levels and levels[t] != level:
                fail([f"工具 {t} 在多个 group 中级别冲突"])
            levels[t] = level

    sys.path.insert(0, str(REPO))
    try:
        from fd_open_data_mcp.server import mcp
    except Exception as e:  # noqa: BLE001
        fail([f"判不出：入口 fd_open_data_mcp.server.mcp 无法导入（{type(e).__name__}: {e}）"], code=2)

    runtime_set = {t.name for t in asyncio.run(mcp.list_tools())}
    declared = set(levels)
    problems = []
    if declared - runtime_set:
        problems.append(f"manifest 多声明（代码未注册）：{sorted(declared - runtime_set)}")
    if runtime_set - declared:
        problems.append(f"代码多注册（manifest 未声明）：{sorted(runtime_set - declared)}")
    rules = m.get("rules", {})
    if set(rules.get("commercial_tools", [])) != {t for t, lv in levels.items() if lv == "commercial"}:
        problems.append("rules.commercial_tools 与组级 level=commercial 的工具集不一致")
    if problems:
        fail(problems)
    print(f"SELFCHECK OK：{len(declared)} 工具，commercial={len([t for t in levels if levels[t]=='commercial'])}")


if __name__ == "__main__":
    main()
