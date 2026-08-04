"""YFinance seed.py parser."""
from __future__ import annotations

from pathlib import Path

from .base import CatalogManifest, GeneratorError
from .akshare_db import _parse_params, _parse_columns


def generate(input_path: str) -> CatalogManifest:
    """Generate catalog from fd-yfinance core/seed.py file.

    Args:
        input_path: Path to seed.py file containing REGISTRY dict

    Returns:
        CatalogManifest with parsed functions
    """
    from .base import parse_python_dict

    seed_path = Path(input_path)
    if not seed_path.exists():
        raise FileNotFoundError(f"Seed file not found at {seed_path}")

    registry = parse_python_dict(str(seed_path))

    manifest = CatalogManifest(
        name="fd-yfinance",
        label="yfinance (Yahoo Finance)",
        source_url="https://github.com/ranaroussi/yfinance",
    )

    # Parse each registry entry
    for cmd, info in registry.items():
        # Extract columns from info["cols"] if present
        columns = []
        params = []

        if isinstance(info, dict):
            if "columns" in info:
                columns = [{"name": c, "type": "str", "description": ""} for c in info["columns"]]

            # Try to infer parameters from common patterns
            if isinstance(info.get("params"), list):
                params = [
                    {"name": p, "type": "str", "required": False, "description": ""}
                    for p in info["params"]
                ]

        function_spec = {
            "command": cmd,
            "category": "finance",
            "description": info.get("description", f"{cmd} - Yahoo Finance data"),
            "frequency": "on_demand",
            "parameters": params,
            "columns": columns,
        }

        manifest.functions.append({
            **function_spec,
            "command": function_spec["command"],
            "category": function_spec["category"],
            "description": function_spec["description"],
            "frequency": function_spec["frequency"],
            "parameters": function_spec["parameters"],
            "columns": function_spec["columns"],
        })

    return manifest
