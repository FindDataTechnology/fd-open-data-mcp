#!/bin/bash
# Manual PyPI upload script
set -e

cd "$(dirname "$0")/.."

echo "Building fd-open-data-mcp..."
python -m build

echo "Checking distributions..."
twine check dist/*

echo "Uploading to PyPI..."
export TWINE_USERNAME=__token__
export TWINE_PASSWORD=${PYPI_API_KEY}
twine upload dist/*

echo "✅ Successfully uploaded to PyPI!"
