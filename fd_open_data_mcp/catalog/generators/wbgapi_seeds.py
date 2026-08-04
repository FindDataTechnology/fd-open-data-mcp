"""WBGAPI seeds parser."""
from __future__ import annotations

from pathlib import Path

from .base import CatalogManifest, GeneratorError


def generate(input_path: str) -> CatalogManifest:
    """Generate catalog from fd-wbgapi seeds/*.py files.

    Args:
        input_path: Directory containing seed files or path to specific file

    Returns:
        CatalogManifest with parsed functions
    """
    from .base import parse_python_dict

    # Handle both directory and single file
    input_path = Path(input_path)

    manifest = CatalogManifest(
        name="fd-wbgapi",
        label="World Bank data API (wbgapi)",
        source_url="https://data.worldbank.org/",
    )

    seed_files = []
    if input_path.is_dir():
        seed_files = list(input_path.glob("*.py"))
    else:
        seed_files = [input_path]

    all_commands = {}
    for seed_file in seed_files:
        try:
            registry = parse_python_dict(str(seed_file))
            for cmd, info in registry.items():
                all_commands[cmd] = info
        except GeneratorError as e:
            print(f"Warning: Failed to parse {seed_file}: {e}", flush=True)
            continue

    for cmd, info in all_commands.items():
        columns = []
        params = []

        if isinstance(info, dict):
            if "columns" in info:
                # Handle both list of dicts and list of strings
                for c in info["columns"]:
                    if isinstance(c, dict):
                        columns.append({
                            "name": c.get("name", ""),
                            "type": c.get("type", "str"),
                            "description": c.get("description", ""),
                        })
                    else:
                        columns.append({"name": str(c), "type": "str", "description": ""})

            if "parameters" in info:
                for p in info["parameters"]:
                    if isinstance(p, dict):
                        params.append({
                            "name": p.get("name", ""),
                            "type": p.get("type", "str"),
                            "required": p.get("required", False),
                            "description": p.get("description", ""),
                        })
                    else:
                        params.append({"name": str(p), "type": "str", "required": False, "description": ""})

        function_spec = {
            "command": cmd,
            "category": info.get("category", "macro_indicators"),
            "description": info.get("description", f"{cmd} - World Bank indicator"),
            "frequency": info.get("frequency", "yearly"),
            "parameters": params,
            "columns": columns,
        }

        manifest.functions.append(function_spec)

    return manifest
