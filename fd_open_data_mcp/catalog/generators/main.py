"""CLI entry point for catalog generator."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main():
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Generate protocol-compliant catalog.py from datasource metadata"
    )
    parser.add_argument(
        "--source", "-s",
        required=True,
        help="Source name (akshare, yfinance, edgar, wbgapi)"
    )
    parser.add_argument(
        "--input", "-i",
        required=True,
        help="Path to source metadata file (registry.db or seed.py)"
    )
    parser.add_argument(
        "--output", "-o",
        required=True,
        help="Output path for generated catalog.py"
    )
    parser.add_argument(
        "--label", "-l",
        default=None,
        help="Human-readable label for the datasource"
    )
    parser.add_argument(
        "--fetch", "-f",
        default=None,
        help="Fetch module reference (e.g., 'pkg.mod:func')"
    )

    args = parser.parse_args()

    # Dispatch based on source type
    if args.source == "akshare":
        from . import akshare_db
        catalog = akshare_db.generate(args.input)
    elif args.source == "yfinance":
        from . import yfinance_seed
        catalog = yfinance_seed.generate(args.input)
    elif args.source == "edgar":
        from . import edgar_seeds
        catalog = edgar_seeds.generate(args.input)
    elif args.source == "wbgapi":
        from . import wbgapi_seeds
        catalog = wbgapi_seeds.generate(args.input)
    else:
        print(f"Error: Unknown source '{args.source}'", file=sys.stderr)
        sys.exit(1)

    # Apply overrides
    if args.label:
        catalog.label = args.label
    if args.fetch:
        catalog.fetch = {"runner": args.fetch}

    # Write output
    from .writer import write_catalog_py
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_catalog_py(catalog, output_path)

    print(f"Generated {len(catalog.functions)} functions to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
