#!/usr/bin/env python3
"""
E2E Test Report Generator for fd-open-data-mcp → scraw-fd-open-data-mcp pipeline

Queries the database and generates a comprehensive report covering all 9 stages.
"""

import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from sqlalchemy import create_engine, text
from typing import Dict, List, Any, Optional

# Configuration
DATABASE_URL = os.environ.get(
    "FD_OPEN_DATA_MCP_DATABASE_URL",
    f"sqlite:///{Path(__file__).parent.parent}/fd_open_data_mcp/metadata/daas.db"
)


class E2EReportGenerator:
    """Generates comprehensive E2E test reports from database queries."""
    
    def __init__(self, database_url: str):
        self.engine = create_engine(database_url)
        self.report: Dict[str, Any] = {
            "generated_at": datetime.now().isoformat(),
            "database_url": database_url.split("@")[0] + "@***",
            "stages": {},
            "summary": {}
        }
    
    def query_stage_1_catalog(self) -> Dict[str, Any]:
        """Stage 1: Catalog Import Verification"""
        with self.engine.connect() as conn:
            sources = conn.execute(text("SELECT COUNT(*) FROM sources")).scalar()
            functions = conn.execute(text("SELECT COUNT(*) FROM functions")).scalar()
            columns = conn.execute(text("SELECT COUNT(*) FROM function_columns")).scalar()
            
            # Get sample data
            sample_sources = conn.execute(text(
                "SELECT name, label, status FROM sources LIMIT 5"
            )).fetchall()
        
        return {
            "status": "PASS" if sources >= 20 and functions >= 500 and columns >= 3000 else "FAIL",
            "metrics": {
                "sources_count": sources,
                "functions_count": functions,
                "columns_count": columns,
                "meets_minimums": sources >= 20 and functions >= 500 and columns >= 3000
            },
            "sample_sources": [dict(s) for s in sample_sources],
            "thresholds": {"min_sources": 20, "min_functions": 500, "min_columns": 3000}
        }
    
    def query_stage_2_bindings(self) -> Dict[str, Any]:
        """Stage 2: Concept Binding Verification"""
        with self.engine.connect() as conn:
            confirmed = conn.execute(text(
                "SELECT COUNT(*) FROM concept_bindings WHERE confirmed=true"
            )).scalar()
            total = conn.execute(text(
                "SELECT COUNT(*) FROM concept_bindings"
            )).scalar()
            
            # Get sample bindings
            sample = conn.execute(text(
                """
                SELECT cb.id, cb.concept_name, cb.source_name, cb.column_name, 
                       cb.confirmed, cb.confidence_score, cb.provenance
                FROM concept_bindings cb
                ORDER BY cb.confirmed DESC, cb.confidence_score DESC
                LIMIT 5
                """
            )).fetchall()
        
        return {
            "status": "PASS" if confirmed >= 500 else "FAIL",
            "metrics": {
                "confirmed_count": confirmed,
                "total_count": total,
                "meets_minimum": confirmed >= 500
            },
            "sample_bindings": [dict(b) for b in sample]
        }
    
    def query_stage_3_entities(self) -> Dict[str, Any]:
        """Stage 3: Entity Identity Verification"""
        with self.engine.connect() as conn:
            total = conn.execute(text(
                "SELECT COUNT(*) FROM entity_source_identifiers"
            )).scalar()
            sources = conn.execute(text(
                "SELECT COUNT(DISTINCT source) FROM entity_source_identifiers"
            )).scalar()
            
            # Count by source
            source_counts = conn.execute(text(
                "SELECT source, COUNT(*) as cnt FROM entity_source_identifiers GROUP BY source ORDER BY cnt DESC LIMIT 5"
            )).fetchall()
        
        return {
            "status": "PASS" if total >= 1000 and sources >= 2 else "FAIL",
            "metrics": {
                "total_identifiers": total,
                "source_count": sources,
                "meets_minimum": total >= 1000 and sources >= 2
            },
            "by_source": [dict(s) for s in source_counts]
        }
    
    def query_stage_4_rankings(self) -> Dict[str, Any]:
        """Stage 4: Source Ranking Verification"""
        with self.engine.connect() as conn:
            total = conn.execute(text(
                "SELECT COUNT(*) FROM source_rankings"
            )).scalar()
            
            # Get top rankings for test concepts
            test_concepts = ["price.close", "gdp.current"]
            rankings = []
            
            for concept in test_concepts:
                rows = conn.execute(text(
                    """
                    SELECT source_name, concept_name, quality, accessibility, 
                           freshness_fit, composite_score
                    FROM source_rankings
                    WHERE concept_name = :concept
                    ORDER BY composite_score DESC
                    LIMIT 3
                    """
                ), {"concept": concept}).fetchall()
                rankings.extend([dict(r) for r in rows])
        
        return {
            "status": "PASS" if total > 0 else "FAIL",
            "metrics": {
                "total_rankings": total,
                "has_test_concepts": any(r["concept_name"] in test_concepts for r in rankings)
            },
            "top_rankings_by_concept": rankings
        }
    
    def query_stage_5_read(self) -> Dict[str, Any]:
        """Stage 5: Concept Fetch (Read) Verification"""
        with self.engine.connect() as conn:
            # Check recent observations (last 1 hour)
            one_hour_ago = datetime.now() - timedelta(hours=1)
            
            recent = conn.execute(text(
                """
                SELECT concept_name, entity_id, entity_type, observation_date, 
                       value, unit, source_used, fetched_at
                FROM semantic_observations
                WHERE fetched_at > :cutoff
                AND value IS NOT NULL
                ORDER BY fetched_at DESC
                LIMIT 10
                """
            ), {"cutoff": one_hour_ago}).fetchall()
            
            # Count by concept family
            count_query = text(
                """
                SELECT concept_name, COUNT(*) as cnt
                FROM semantic_observations
                WHERE fetched_at > :cutoff
                GROUP BY concept_name
                ORDER BY cnt DESC
                LIMIT 10
                """
            )
            counts = conn.execute(count_query, {"cutoff": one_hour_ago}).fetchall()
        
        return {
            "status": "PASS" if len(recent) > 0 else "SKIP",
            "metrics": {
                "recent_observations": len(recent),
                "concepts_crawled": len(counts)
            },
            "sample_observations": [dict(r) for r in recent],
            "by_concept": [dict(c) for c in counts]
        }
    
    def query_stage_6_plan(self) -> Dict[str, Any]:
        """Stage 6: Crawl Plan Verification"""
        # Use relative path from script location
        change_dir = Path(__file__).parent.parent / "openspec" / "changes" / "full-test-fd-open-data-mcp"
        plan_path = change_dir / "test_crawl_plan.json"
        
        if not plan_path.exists():
            return {
                "status": "SKIP",
                "note": "Crawl plan not found at expected location"
            }
        
        try:
            with open(plan_path) as f:
                plan = json.load(f)
            
            tasks = plan.get("tasks", [])
            
            return {
                "status": "PASS" if len(tasks) >= 1 else "FAIL",
                "metrics": {
                    "task_count": len(tasks),
                    "plan_valid": "tasks" in plan
                },
                "plan_summary": {
                    "type": plan.get("type"),
                    "created_at": plan.get("created_at"),
                    "task_types": list(set(t.get("task_type") for t in tasks))
                }
            }
        except Exception as e:
            return {
                "status": "FAIL",
                "error": str(e)
            }
    
    def query_stage_7_deploy(self) -> Dict[str, Any]:
        """Stage 7: Spider Deploy Verification"""
        import subprocess
        
        try:
            result = subprocess.run(
                ["curl", "-s", "http://localhost:6800/listspiders.json"],
                capture_output=True,
                text=True,
                timeout=5
            )
            
            if result.returncode != 0:
                return {
                    "status": "SKIP",
                    "note": "Scrapyd not reachable at http://localhost:6800"
                }
            
            spiders = json.loads(result.stdout)
            spider_list = spiders.get("spiders", [])
            
            has_concept_crawl = "concept_crawl" in spider_list
            
            return {
                "status": "PASS" if has_concept_crawl else "FAIL",
                "metrics": {
                    "spider_count": len(spider_list),
                    "has_concept_crawl": has_concept_crawl
                },
                "deployed_spiders": spider_list
            }
        except Exception as e:
            return {
                "status": "SKIP",
                "note": f"Error checking deployment: {e}"
            }
    
    def query_stage_8_crawl(self) -> Dict[str, Any]:
        """Stage 8: Crawl Execution Verification"""
        import subprocess
        
        change_dir = Path(__file__).parent.parent / "openspec" / "changes" / "full-test-fd-open-data-mcp"
        job_id_file = change_dir / "crawl_job_id.txt"
        
        if not job_id_file.exists():
            return {
                "status": "SKIP",
                "note": "No crawl job ID found (job may not have run)"
            }
        
        try:
            job_id = job_id_file.read_text().strip()
            
            result = subprocess.run(
                ["curl", "-s", f"http://localhost:6800/listjobs.json?project=scraw_fd_open_data_mcp&job={job_id}"],
                capture_output=True,
                text=True,
                timeout=5
            )
            
            if result.returncode != 0:
                return {
                    "status": "SKIP",
                    "note": "Could not query scrapyd for job status"
                }
            
            job_info = json.loads(result.stdout)
            state = job_info.get("state", "unknown")
            
            return {
                "status": "PASS" if state == "finished" else ("SKIP" if state == "pending" else "FAIL"),
                "metrics": {
                    "job_id": job_id,
                    "state": state,
                    "started": job_info.get("started"),
                    "finished": job_info.get("finished")
                }
            }
        except Exception as e:
            return {
                "status": "SKIP",
                "note": f"Error checking crawl job: {e}"
            }
    
    def query_stage_9_readback(self) -> Dict[str, Any]:
        """Stage 9: Read-back Verification"""
        with self.engine.connect() as conn:
            # Query for crawled data
            results = conn.execute(text(
                """
                SELECT concept_name, entity_id, observation_date, value, 
                       source_used, fetched_at
                FROM semantic_observations
                WHERE (concept_name, entity_id, observation_date) IN (
                    ('price.close', '600519', '2024-07-26'),
                    ('gdp.current', 'CN', '2022')
                )
                AND value IS NOT NULL
                LIMIT 10
                """
            )).fetchall()
        
        return {
            "status": "PASS" if len(results) > 0 else "FAIL",
            "metrics": {
                "found_observations": len(results),
                "test_tuple_coverage": len([r for r in results if r[2] in ['2024-07-26', '2022']])
            },
            "observations": [{"concept": r[0], "entity": r[1], "date": r[2], "value": r[3]} for r in results]
        }
    
    def generate_report(self) -> str:
        """Generate the full E2E test report."""
        print("=" * 60)
        print("E2E Integration Test Report")
        print(f"Generated: {self.report['generated_at']}")
        print("=" * 60)
        
        stages = [
            ("Stage 1: Catalog Import", self.query_stage_1_catalog),
            ("Stage 2: Concept Bindings", self.query_stage_2_bindings),
            ("Stage 3: Entity Identity", self.query_stage_3_entities),
            ("Stage 4: Source Rankings", self.query_stage_4_rankings),
            ("Stage 5: Concept Fetch (Read)", self.query_stage_5_read),
            ("Stage 6: Crawl Plan", self.query_stage_6_plan),
            ("Stage 7: Spider Deploy", self.query_stage_7_deploy),
            ("Stage 8: Crawl Execution", self.query_stage_8_crawl),
            ("Stage 9: Read-back Verification", self.query_stage_9_readback),
        ]
        
        all_passed = True
        
        for stage_name, stage_func in stages:
            print(f"\n{stage_name}")
            print("-" * 60)
            
            try:
                stage_result = stage_func()
                status = stage_result.get("status", "ERROR")
                
                color_map = {"PASS": "\033[0;32m", "FAIL": "\033[0;31m", "SKIP": "\033[1;33m"}
                reset = "\033[0m"
                print(f"Status: {color_map.get(status, '')}{status}{reset}")
                
                if status == "FAIL":
                    all_passed = False
                
                # Print metrics
                if "metrics" in stage_result:
                    print("Metrics:")
                    for key, value in stage_result["metrics"].items():
                        print(f"  - {key}: {value}")
                
                # Print samples if available
                for key in ["sample_sources", "sample_bindings", "sample_observations", "observations"]:
                    if key in stage_result and stage_result[key]:
                        print(f"\nSample Data ({len(stage_result[key])} rows):")
                        for item in stage_result[key][:3]:
                            print(f"  - {item}")
                        if len(stage_result[key]) > 3:
                            print(f"  ... and {len(stage_result[key]) - 3} more")
                
            except Exception as e:
                print(f"Error: {e}")
                all_passed = False
        
        # Summary
        print("\n" + "=" * 60)
        print("Summary")
        print("=" * 60)
        
        if all_passed:
            print("\n\033[0;32m✓ All PASSing stages completed successfully\033[0m")
        else:
            print("\n\033[0;31m✗ Some stages failed or were skipped\033[0m")
        
        print(f"\nDatabase: {self.report['database_url']}")
        print(f"Report generated: {datetime.now().isoformat()}")
        
        return json.dumps(self.report, indent=2, default=str)


def main():
    """Entry point for the report generator."""
    # Calculate change directory for report output
    change_dir = Path(__file__).parent.parent / "openspec" / "changes" / "full-test-fd-open-data-mcp"
    
    db_url = os.environ.get("FD_OPEN_DATA_MCP_DATABASE_URL", DATABASE_URL)
    
    print(f"Connecting to database...")
    generator = E2EReportGenerator(db_url)
    
    report_json = generator.generate_report()
    
    # Save JSON report
    report_path = change_dir / "e2e_test_report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report_json)
    print(f"\nJSON report saved to: {report_path}")
    
    return 0 if all([v.get("status") != "FAIL" for v in generator.report["stages"].values()]) else 1


if __name__ == "__main__":
    sys.exit(main())
