"""AkShare registry.db parser."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .base import CatalogManifest, FunctionParamSpec, FunctionColumnSpec


def generate(input_path: str) -> CatalogManifest:
    """Generate catalog from akshare registry.db file.

    Args:
        input_path: Path to registry.db SQLite database

    Returns:
        CatalogManifest with all functions parsed
    """
    db_path = Path(input_path)
    if not db_path.exists():
        raise FileNotFoundError(f"Registry not found at {db_path}")

    manifest = CatalogManifest(
        name="fd-akshare",
        label="AKShare (Chinese financial data)",
        source_url="https://github.com/AkFamily/akshare",
    )

    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()

        # Read functions table
        cursor.execute("SELECT command, description, category FROM functions")
        for cmd, desc, category in cursor.fetchall():
            params = _parse_params(cursor, cmd)
            columns = _parse_columns(cursor, cmd)

            function_spec = {
                "command": cmd,
                "category": category or "other",
                "description": desc or "",
                "frequency": "unknown",
                "parameters": params,
                "columns": columns,
            }

            try:
                manifest.functions.append({**manifest.functions[-1], **function_spec})
            except:
                pass

    return manifest


def _parse_params(cursor, command: str) -> list:
    """Parse parameter definitions from DB."""
    try:
        cursor.execute(
            "SELECT param_name, param_type, is_required, param_desc FROM function_params WHERE command = ?",
            (command,)
        )
        return [
            {
                "name": row[0],
                "type": row[1] or "str",
                "required": bool(row[2]),
                "description": row[3] or "",
            } for row in cursor.fetchall()
        ]
    except Exception:
        return []


def _parse_columns(cursor, command: str) -> list:
    """Parse column definitions from DB."""
    try:
        cursor.execute(
            "SELECT column_name, data_type, description FROM function_columns WHERE command = ?",
            (command,)
        )
        return [
            {
                "name": row[0],
                "type": row[1] or "str",
                "description": row[2] or "",
            } for row in cursor.fetchall()
        ]
    except Exception:
        return []
