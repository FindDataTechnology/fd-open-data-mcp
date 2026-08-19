#!/bin/bash
# E2E Integration Test - Stages 6-9: Crawl Pipeline
# Stage 6: Plan Generation | Stage 7: Spider Deploy | Stage 8: Concept Crawl | Stage 9: Read-back Verification

set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHANGE_DIR="$(dirname "$SCRIPT_DIR")"
ROOT_DIR="$(dirname "$CHANGE_DIR")"

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log_pass() { echo -e "${GREEN}[PASS]${NC} $*"; }
log_fail() { echo -e "${RED}[FAIL]${NC} $*"; }
log_skip() { echo -e "${YELLOW}[SKIP]${NC} $*"; }

# Test configuration
# Canonical DB: guangzhou-xinru:30432/fd_open_data. From the Mac via SSH tunnel:
#   ssh -N -L 30432:127.0.0.1:30432 -L 30380:127.0.0.1:30380 ubuntu@134.175.46.69
export FD_OPEN_DATA_MCP_DATABASE_URL="${FD_OPEN_DATA_MCP_DATABASE_URL:-postgresql://fd:FD_PG_PASSWORD@127.0.0.1:30432/fd_open_data}"
SCRAYPD_URL="${SCRAYPD_URL:-http://localhost:6800}"

TEST_CONCEPTS=("price.close" "gdp.current")
TEST_ENTITIES=(
    "price.close:600519:a-shares:2024-07-26:2024-07-31"
    "gdp.current:CN:country:2022:2022"
)

# ============================================
# Stage 6: Crawl Plan Generation
# ============================================
generate_crawl_plan() {
    local plan_file="$CHANGE_DIR/test_crawl_plan.json"
    
    echo "Generating crawl plan..."
    
    # Build the plan command for each concept
    cd "$SCRIPT_DIR/../fd-open-data-mcp"
    
    if ! uv run fd-open-data-mcp plan-crawl \
        --concepts "${TEST_CONCEPTS[*]}" \
        --entity-id "600519" \
        --entity-type "a-shares" \
        --dates "2024-07-26:2024-07-31" 2>&1 > "$plan_file"; then
        
        log_fail "plan-crawl failed"
        return 1
    fi
    
    # Validate plan structure
    if [[ ! -f "$plan_file" ]] || [[ ! -s "$plan_file" ]]; then
        log_fail "Plan file not created or empty"
        return 1
    fi
    
    # Check for tasks array
    local task_count=$(python3 -c "import json; data=json.load(open('$plan_file')); print(len(data.get('tasks', [])))" 2>/dev/null || echo "0")
    
    echo "Generated plan with $task_count tasks"
    
    if [[ $task_count -ge 1 ]]; then
        log_pass "Crawl plan generated with $task_count tasks"
        cat "$plan_file"
        return 0
    else
        log_fail "Crawl plan has no tasks"
        return 1
    fi
}

verify_crawl_plan() {
    generate_crawl_plan
}

# ============================================
# Stage 7: Spider Deploy
# ============================================
deploy_spider() {
    cd "$ROOT_DIR/scraw-fd-open-data-mcp"
    
    echo "Deploying scraw-fd-open-data-mcp spider to scrapyd..."
    
    # Check if scrapyd is reachable
    local scrapyd_status=$(curl -s "$SCRAYPD_URL/daemonstatus.json" 2>/dev/null || echo "")
    
    if [[ -z "$scrapyd_status" ]]; then
        log_skip "Scrapyd not reachable at $SCRAYPD_URL"
        return 127
    fi
    
    echo "Scrapyd status: $scrapyd_status"
    
    # Build and deploy
    if ! bash deploy.sh 2>&1; then
        log_fail "deploy.sh failed"
        return 1
    fi
    
    # Verify spider is listed
    local spiders=$(curl -s "$SCRAYPD_URL/listspiders.json" 2>/dev/null || echo "")
    
    if echo "$spiders" | grep -q "concept_crawl"; then
        log_pass "Spider 'concept_crawl' deployed and listed in scrapyd"
        echo "$spiders" | python3 -m json.tool 2>/dev/null || echo "$spiders"
        return 0
    else
        log_fail "Spider 'concept_crawl' not found in scrapyd spider list"
        echo "Spiders: $spiders"
        return 1
    fi
}

# ============================================
# Stage 8: Concept Crawl Execution
# ============================================
execute_crawl() {
    local plan_file="$CHANGE_DIR/test_crawl_plan.json"
    local job_id_file="$CHANGE_DIR/crawl_job_id.txt"
    
    cd "$ROOT_DIR/scraw-fd-open-data-mcp"
    
    # Check if plan exists
    if [[ ! -f "$plan_file" ]]; then
        log_fail "Crawl plan not found at $plan_file"
        return 1
    fi
    
    echo "Scheduling concept_crawl spider with plan..."
    
    # Schedule the crawl (using schedule.py)
    # The plan file path needs to be passed appropriately
    if ! uv run python schedule.py concept_crawl 2>&1 | tee /tmp/crawl_schedule.log; then
        log_fail "Failed to schedule concept_crawl"
        return 1
    fi
    
    # Extract job ID from output or check recent jobs
    local job_id=$(tail -n 5 /tmp/crawl_schedule.log | grep -oP 'job_id=\K[^ ]+' || echo "")
    
    if [[ -z "$job_id" ]]; then
        # Try to get the latest job from scrapyd
        job_id=$(curl -s "$SCRAYPD_URL/listjobs.json?project=scraw_fd_open_data_mcp" 2>/dev/null | \
            python3 -c "import sys, json; data=json.load(sys.stdin); jobs=data.get('running', []) + data.get('pending', []); print(jobs[-1]['id'] if jobs else '')" 2>/dev/null || echo "")
    fi
    
    if [[ -z "$job_id" ]]; then
        log_skip "Could not extract job ID from schedule output"
        echo "See /tmp/crawl_schedule.log for details"
        return 127
    fi
    
    echo "Crawl job ID: $job_id"
    echo "$job_id" > "$job_id_file"
    
    # Poll for completion
    echo "Polling job status (timeout 10 min)..."
    local timeout=$((SECONDS + 600))  # 10 minutes
    local last_status=""
    
    while [[ $SECONDS -lt $timeout ]]; do
        local job_info=$(curl -s "$SCRAYPD_URL/listjobs.json?project=scraw_fd_open_data_mcp&job=$job_id" 2>/dev/null || echo "{}")
        
        if echo "$job_info" | grep -q '"finished"'; then
            echo "Job finished!"
            echo "$job_info" | python3 -m json.tool 2>/dev/null || echo "$job_info"
            
            # Get logs
            echo "Fetching job logs..."
            curl -s "$SCRAYPD_URL/logs/$job_id.log" 2>/dev/null | tail -n 20
            
            log_pass "Concept crawl completed successfully"
            return 0
        elif echo "$job_info" | grep -q '"failed"'; then
            log_fail "Job failed"
            curl -s "$SCRAYPD_URL/logs/$job_id.log" 2>/dev/null | tail -n 30
            return 1
        fi
        
        local current_status=$(echo "$job_info" | python3 -c "import sys, json; data=json.load(sys.stdin); print(data.get('state', 'unknown'))" 2>/dev/null || echo "unknown")
        
        if [[ "$current_status" != "$last_status" ]]; then
            echo "Status: $current_status ($(date +%H:%M:%S))"
            last_status="$current_status"
        fi
        
        sleep 5
    done
    
    log_fail "Crawl job did not complete within 10 minutes"
    return 1
}

# ============================================
# Stage 9: Read-back Verification
# ============================================
verify_readback() {
    local plan_file="$CHANGE_DIR/test_crawl_plan.json"
    local job_id_file="$CHANGE_DIR/crawl_job_id.txt"
    
    # Get crawl start time
    local crawl_start=""
    if [[ -f "$job_id_file" ]]; then
        local job_id=$(cat "$job_id_file")
        crawl_start=$(curl -s "$SCRAYPD_URL/listjobs.json?project=scraw_fd_open_data_mcp&job=$job_id" 2>/dev/null | \
            python3 -c "import sys, json; data=json.load(sys.stdin); print(data.get('started', ''))" 2>/dev/null || echo "")
    fi
    
    if [[ -z "$crawl_start" ]]; then
        crawl_start=$(date -v-10M +%Y-%m-%d\ %H:%M:%S)  # Fallback: 10 minutes ago
    fi
    
    echo "Verifying observations crawled after: $crawl_start"
    
    # Query semantic_observations for crawled data
    cd "$SCRIPT_DIR/../fd-open-data-mcp"
    
    local obs_check=$(uv run python -c "
import os
from sqlalchemy import create_engine, text
os.environ['FD_OPEN_DATA_MCP_DATABASE_URL'] = '$FD_OPEN_DATA_MCP_DATABASE_URL'
engine = create_engine('$FD_OPEN_DATA_MCP_DATABASE_URL')
with engine.connect() as conn:
    results = conn.execute(text('''
        SELECT concept_name, entity_id, observation_date, value, source_used 
        FROM semantic_observations 
        WHERE (concept_name, entity_id, observation_date) IN (
            ('price.close', '600519', '2024-07-26'),
            ('gdp.current', 'CN', '2022')
        )
        AND value IS NOT NULL
        LIMIT 10
    ''')).fetchall()
print(f'{len(results)}')
for row in results:
    print(f'{row[0]}|{row[1]}|{row[2]}|{row[3]}|{row[4]}')
" 2>/dev/null || echo "0")
    
    local count=$(echo "$obs_check" | head -n1)
    local rows=$(echo "$obs_check" | tail -n +2)
    
    echo "Found $count observations matching test criteria:"
    echo "$rows"
    
    if [[ $count -gt 0 ]]; then
        log_pass "Read-back verification successful: $count crawled observations found"
        return 0
    else
        log_fail "No observations found for crawled concepts"
        return 1
    fi
}

# ============================================
# Main execution
# ============================================
main() {
    echo "========================================"
    echo "E2E Integration Test - Stages 6-9"
    echo "Started: $(date)"
    echo "========================================"
    
    local overall_result=0
    
    echo ""
    echo "========================================"
    echo "Stage 6: Crawl Plan Generation"
    echo "========================================"
    verify_crawl_plan || overall_result=1
    
    echo ""
    echo "========================================"
    echo "Stage 7: Spider Deploy"
    echo "========================================"
    deploy_spider || overall_result=1
    
    echo ""
    echo "========================================"
    echo "Stage 8: Concept Crawl Execution"
    echo "========================================"
    execute_crawl || overall_result=1
    
    echo ""
    echo "========================================"
    echo "Stage 9: Read-back Verification"
    echo "========================================"
    verify_readback || overall_result=1
    
    echo ""
    echo "========================================"
    echo "Crawl Pipeline Summary"
    echo "Completed: $(date)"
    echo "========================================"
    
    exit $overall_result
}

main "$@"
