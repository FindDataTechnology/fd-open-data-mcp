#!/usr/bin/env python3
"""Robust financial data consumer with proper error handling."""
import os
import sys
import time
import redis
import json
import traceback
from sqlalchemy import create_engine, text

REDIS_URL = os.environ.get("REDIS_URL", "redis://fd-open-redis.scraw:6379/0")
DB_URL = os.environ.get(
    "FD_OPEN_DATA_MCP_DATABASE_URL",
    "postgresql+psycopg2://postgres:admin123@fd-open-pg.scraw:5432/postgres"
)
QUEUE_KEY = "financial_crawl:tasks"
PROGRESS_KEY = "financial_crawl:progress"
POD_ID = os.environ.get("POD_NAME", "unknown")

def execute_task(task):
    """Execute a single task and write results to DB."""
    source = task["source"]
    func_name = task["function_name"]
    
    from fd_open_data_mcp.fetch.runner import run_upstream
    
    try:
        df = run_upstream(source, func_name, task)
        
        # Parse DataFrame and extract observations
        observations = []
        
        if source == "akshare":
            field_mapping = {
                "annual_bs": [("TOTAL_ASSETS", 242), ("TOTAL_LIABILITIES", 243), ("TOTAL_EQUITY", 244)],
                "annual_is": [("OPERATE_INCOME", 239), ("NETPROFIT", 240)],
                "annual_cf": [("NETCASH_OPERATE", 241)],
                "quarterly_bs": [("TOTAL_ASSETS", 242), ("TOTAL_LIABILITIES", 243), ("TOTAL_EQUITY", 244)],
                "quarterly_is": [("OPERATE_INCOME", 239), ("NETPROFIT", 240)],
                "quarterly_cf": [("NETCASH_OPERATE", 241)],
            }
            
            if func_name in field_mapping:
                for _, row in df.iterrows():
                    report_date = str(row.get('REPORT_DATE', ''))[:10]
                    if not report_date:
                        continue
                    
                    for field, concept_id in field_mapping[func_name]:
                        val = row.get(field)
                        if val is not None and str(val) != 'nan':
                            entity_id = row.get('SECUCODE') or row.get('SECURITY_CODE')
                            if entity_id:
                                observations.append({
                                    "entity_id": int(entity_id) if isinstance(entity_id, str) else entity_id,
                                    "date": report_date,
                                    "concept_id": concept_id,
                                    "value": str(val),
                                    "source": "akshare",
                                })
        
        elif source == "yfinance":
            field_mapping = {
                "balance_sheet": [("Total Assets", 242), ("Total Liab Net Debt", 243), ("Stockholders Equity", 244)],
                "income": [("Total Revenue", 239), ("Net Income", 240)],
                "cashflow": [("Total Cash From Operating Activities", 241)],
            }
            
            if func_name in field_mapping:
                dates = [str(d)[:10] for d in df.columns if hasattr(d, '__str__')]
                
                for yf_field, concept_id in field_mapping[func_name]:
                    if yf_field not in df.index:
                        continue
                    
                    values = df.loc[yf_field]
                    for i, dt in enumerate(dates):
                        if i < len(values) and values.iloc[i] is not None and str(values.iloc[i]) != 'nan':
                            observations.append({
                                "entity_id": int(getattr(df.index[0], 'name', 0)),
                                "date": dt,
                                "concept_id": concept_id,
                                "value": str(values.iloc[i]),
                                "source": "yfinance",
                            })
        else:
            return 0
        
        # Write observations to DB
        engine = create_engine(DB_URL)
        for obs in observations:
            try:
                with engine.begin() as conn:
                    conn.execute(text("""
                        INSERT INTO semantic_observations
                        (concept_id, entity_type, entity_id, date, value, unit, source_used, fetched_at)
                        VALUES (:c,'stock',:e,:d,:v,'currency',:s,NOW())
                        ON CONFLICT DO NOTHING
                    """), obs)
            except Exception as e:
                print(f"[{POD_ID}] Failed to write observation: {e}")
        
        return len(observations)
    
    except Exception as e:
        print(f"[{POD_ID}] Task failed ({source}/{func_name}): {type(e).__name__}: {e}")
        traceback.print_exc()
        return 0

def main():
    print(f"[{POD_ID}] Starting consumer...")
    print(f"Redis: {REDIS_URL}")
    print(f"DB: {DB_URL}")
    
    # Connect to Redis with retries
    max_retries = 10
    r = None
    for i in range(max_retries):
        try:
            r = redis.from_url(REDIS_URL, socket_connect_timeout=2)
            r.ping()
            print(f"[{POD_ID}] Connected to Redis after {i+1} attempts")
            break
        except Exception as e:
            print(f"[{POD_ID}] Attempt {i+1}/{max_retries} - Redis connect failed: {e}")
            time.sleep(2)
    
    if not r:
        print(f"[{POD_ID}] FAILED to connect to Redis after {max_retries} attempts. Exiting.")
        sys.exit(1)
    
    processed = 0
    errors = 0
    last_check = time.time()
    check_interval = 10  # Seconds between queue checks
    
    print(f"[{POD_ID}] Ready to process tasks...")
    
    while True:
        current_time = time.time()
        
        # Check if queue is empty periodically (every check_interval seconds)
        if current_time - last_check >= check_interval:
            queue_size = 0
            try:
                queue_size = r.llen(QUEUE_KEY)
            except Exception as e:
                print(f"[{POD_ID}] Queue size check failed: {e}")
            
            if queue_size == 0:
                print(f"[{POD_ID}] Queue empty, sleeping {check_interval}s... Processed: {processed}, Errors: {errors}")
                time.sleep(check_interval)
                last_check = current_time
                continue
        
        # Non-blocking poll with very short timeout
        try:
            result = r.brpop(QUEUE_KEY, timeout=1)
            
            if result is None:
                last_check = current_time
                continue
            
            task_json = result[1]
            task = json.loads(task_json)
            
            obs_count = execute_task(task)
            
            if obs_count > 0:
                processed += 1
                if processed % 20 == 0:
                    print(f"[{POD_ID}] ✓ Processed {processed} tasks, wrote {obs_count} observations")
            else:
                errors += 1
                
        except json.JSONDecodeError as e:
            print(f"[{POD_ID}] JSON decode error: {e}")
            errors += 1
        except Exception as e:
            print(f"[{POD_ID}] Error processing task: {type(e).__name__}: {e}")
            errors += 1

if __name__ == "__main__":
    main()
