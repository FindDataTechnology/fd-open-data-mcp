#!/bin/bash
# Unified E2E Integration Test Runner for fd-open-data-mcp → scraw-fd-open-data-mcp pipeline
# Combines stages 1-5 (MCP CLI) and stages 6-9 (Crawl pipeline)

set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHANGE_DIR="$(dirname "$SCRIPT_DIR")/../openspec/changes/full-test-fd-open-data-mcp"

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m'

echo "========================================"
echo "E2E Integration Test - Unified Runner"
echo "Started: $(date)"
echo "========================================"

# Ensure scripts directory exists
mkdir -p "$SCRIPT_DIR"

OVERALL_RESULT=0

# Stage 1-5
echo ""
echo "========================================"
echo "PHASE 1: MCP CLI Pipeline (Stages 1-5)"
echo "========================================"
bash "$SCRIPT_DIR/e2e_test_stages_1_5.sh" || OVERALL_RESULT=$?

if [[ $OVERALL_RESULT -eq 0 ]]; then
    echo -e "\n${GREEN}✓ Phase 1 completed successfully${NC}"
else
    echo -e "\n${RED}✗ Phase 1 failed or skipped${NC}"
fi

# Stage 6-9
echo ""
echo "========================================"
echo "PHASE 2: Crawl Pipeline (Stages 6-9)"
echo "========================================"
bash "$SCRIPT_DIR/e2e_test_stages_6_9.sh" || OVERALL_RESULT=$?

if [[ $OVERALL_RESULT -eq 0 ]]; then
    echo -e "\n${GREEN}✓ Phase 2 completed successfully${NC}"
else
    echo -e "\n${YELLOW}⚠ Phase 2 has issues (may be due to missing scrapyd/redis)${NC}"
fi

# Generate report
echo ""
echo "========================================"
echo "Generating Report"
echo "========================================"
cd "$SCRIPT_DIR"
uv run python e2e_report.py || echo "Report generation encountered issues"

echo ""
echo "========================================"
echo "Final Summary"
echo "========================================"
echo "Completed: $(date)"
echo ""

if [[ $OVERALL_RESULT -eq 0 ]]; then
    echo -e "${GREEN}✓ ALL STAGES PASSED!${NC}"
else
    echo -e "${RED}✗ Some stages failed or were skipped${NC}"
    echo "Check the generated report at:"
    echo "  ${YELLOW}$CHANGE_DIR/e2e_test_report.json${NC}"
fi

exit $OVERALL_RESULT
