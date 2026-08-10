"""Catalog output writer."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

from .base import CatalogManifest, FunctionParamSpec, FunctionColumnSpec


def write_catalog_py(catalog: CatalogManifest, output_path: str | Path):
    """Write a protocol-compliant catalog.py file.

    Args:
        catalog: The catalog manifest to write
        output_path: Path to output file
    """
    # Build functions list as Python code
    functions_lines = []
    for func in catalog.functions:
        # Handle both dict and object formats for backward compatibility
        if isinstance(func, dict):
            func_dict = func
        else:
            func_dict = _func_to_dict(func)

        # Format as Python dict literal
        func_str = _format_dict(func_dict, indent=8)
        functions_lines.append(func_str)

    functions_text = ",\n".join(functions_lines)

    # Build concepts and entities
    concepts_str = "[]"
    entities_str = "[]"

    # Generate the full file
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    source_code = f'''"""{catalog.label} datasource manifest conforming to fd-open-data-protocol.

Auto-generated on {timestamp}. DO NOT EDIT MANUALLY - edit enrichments/concepts.py instead.
"""
from __future__ import annotations


CATALOG = {{
    "version": "1",
    "name": "{catalog.name}",
    "label": "{catalog.label}",
    "source_url": "{catalog.source_url}",
    "ranking_seed": [{catalog.ranking_seed[0]}, {catalog.ranking_seed[1]}],
    "scanner_mode": "{catalog.scanner_mode}",
    "functions": [
{functions_text}
    ],
    "concepts": {concepts_str},  # TODO: add concept hints here
    "entities": {entities_str},  # TODO: add entity coverage declarations
    "fetch": {{"runner": "{catalog.fetch.get('runner', 'default')}"}}
}}
'''

    with open(output_path, "w") as f:
        f.write(source_code)


def _func_to_dict(func) -> dict:
    """Convert FunctionSpec to dictionary."""
    return {
        "command": func.command,
        "category": func.category,
        "description": func.description,
        "frequency": func.frequency,
        "parameters": [{"name": p.name, "type": p.type_, "required": p.required, "description": p.description} for p in func.parameters],
        "columns": [{"name": c.name, "type": c.type_, "description": c.description} for c in func.columns],
    }


def _format_dict(d: dict, indent: int = 0) -> str:
    """Format a dictionary as Python code with proper indentation."""
    indent_str = " " * indent
    lines = ["{"]
    for key, value in d.items():
        if isinstance(value, str):
            value_str = f'"{value}"'
        elif isinstance(value, bool):
            value_str = str(value).capitalize()  # True/False (Python booleans)
        elif isinstance(value, (int, float)):
            value_str = str(value)
        elif isinstance(value, list):
            value_str = _format_list(value, indent + 4)
        else:
            value_str = repr(value)
        lines.append(f'{indent_str}    "{key}": {value_str},')
    lines.append(f"{indent_str}}}")
    return "\n".join(lines)


def _format_list(lst: list, indent: int = 0) -> str:
    """Format a list as Python code with proper indentation."""
    if not lst:
        return "[]"

    indent_str = " " * indent
    lines = ["["]
    for item in lst:
        if isinstance(item, dict):
            item_str = _format_dict(item, indent + 4)
        elif isinstance(item, str):
            item_str = f'"{item}"'
        else:
            item_str = repr(item)
        lines.append(f"{indent_str}    {item_str},")
    lines.append(f"{indent_str}]")
    return "\n".join(lines)
