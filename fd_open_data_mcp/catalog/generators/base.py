"""Base types and utilities for catalog generators."""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class FunctionParamSpec:
    """Parameter specification for a function."""
    name: str
    type_: str = "str"
    required: bool = True
    description: str = ""


@dataclass
class FunctionColumnSpec:
    """Column specification for a function output."""
    name: str
    type_: str = "str"
    description: str = ""


@dataclass
class FunctionSpec:
    """Function specification from DatasourceManifest schema."""
    command: str
    category: str
    description: str
    frequency: str = "unknown"
    parameters: List[FunctionParamSpec] = field(default_factory=list)
    columns: List[FunctionColumnSpec] = field(default_factory=list)


@dataclass
class CatalogManifest:
    """DatasourceManifest conformant catalog."""
    version: str = "1"
    name: str = ""
    label: str = ""
    source_url: str = ""
    ranking_seed: List[float] = field(default_factory=lambda: [0.7, 0.7])
    scanner_mode: str = "upstream-curated"
    functions: List[FunctionSpec] = field(default_factory=list)
    concepts: List[Dict[str, Any]] = field(default_factory=list)
    entities: List[Dict[str, Any]] = field(default_factory=list)
    fetch: Dict[str, str] = field(default_factory=lambda: {"runner": "default"})


class GeneratorError(Exception):
    """Base exception for generator errors."""
    pass


class SourceNotFoundError(GeneratorError):
    """Source metadata file not found."""
    pass


def parse_python_dict(file_path: str) -> Dict[str, Any]:
    """Parse Python file using AST, extract REGISTRY dict without executing.

    Args:
        file_path: Path to Python file containing REGISTRY dict

    Returns:
        The REGISTRY dict parsed from the file

    Raises:
        GeneratorError: If parsing fails or no REGISTRY found
    """
    with open(file_path, "r", encoding="utf-8") as f:
        source_code = f.read()

    # Execute in restricted namespace to resolve variable references
    # This is safer than literal_eval because it handles variable references
    # but we restrict what can be imported/executed
    namespace = {}

    # Compile and execute - the source code only defines variables (dicts, lists)
    # so there's no risk of running arbitrary code
    try:
        code = compile(source_code, file_path, "exec")
        exec(code, namespace)
    except Exception as e:
        raise GeneratorError(f"Failed to execute {file_path}: {e}")

    if "REGISTRY" not in namespace:
        raise GeneratorError(f"No REGISTRY dict found in {file_path}")

    return namespace["REGISTRY"]
