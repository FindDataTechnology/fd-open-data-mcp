#!/usr/bin/env python3
"""Emit (never apply) the scheduling artifacts for schedule-activation.

Spec requirement "Emitted, not applied, scheduling artifacts": each source's
incremental schedule must follow its manifest functions' declared frequency,
the artifacts land in the output directory, and nothing is written to a cluster
or local scheduler.

Per-source cadence = the FINEST frequency among the source's bound concepts: a
tick must be at least as frequent as the fastest-moving concept it covers, so
one incremental run advances every cadence (the planner expands dates per
concept frequency, so the coarser ones simply do not fire each tick). A source
with no bound concepts is emitted with a null schedule and the reason.

Usage:
    python scripts/emit_scheduling_artifacts.py
    python scripts/emit_scheduling_artifacts.py --db-url sqlite:////tmp/x.db
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

from sqlalchemy import create_engine, text

DEFAULT_URL = "sqlite:////tmp/schedule_activation_trial.db"
OUTPUT_DIR = Path("/Users/chengsishi/finddata/scraw-fd-open-data-mcp/output/schedule-activation")

# Finest first. Off-the-hour minutes: a :00 tick collides with every other cron
# in the fleet (the coverage-expander uses 3,13,23,... for the same reason).
FREQ_CRON: dict[str, str] = {
    "daily": "17 6 * * *",
    "weekly": "23 6 * * 1",
    "monthly": "31 6 1 * *",
    "quarterly": "31 6 1 1,4,7,10 *",
    "yearly": "41 6 2 1 *",
}
FINEST_ORDER = ["daily", "weekly", "monthly", "quarterly", "yearly"]

# Bound concepts by source and declared frequency.
BOUND_FREQS_SQL = text("""
    SELECT s.name AS source, c.frequency AS frequency, COUNT(DISTINCT c.id) AS n
    FROM concept_bindings b
    JOIN concepts c    ON c.id = b.concept_id
    JOIN columns col   ON col.id = b.column_id
    JOIN functions f   ON f.id = col.function_id
    JOIN sources s     ON s.id = f.source_id
    GROUP BY 1, 2
""")


def derive_schedules(db_url: str) -> list[dict]:
    """One schedule entry per source, cadence = finest bound concept frequency."""
    eng = create_engine(db_url)
    rows: dict[str, dict[str, int]] = {}
    with eng.connect() as conn:
        for source, frequency, n in conn.execute(BOUND_FREQS_SQL):
            rows.setdefault(source, {})[frequency] = n
        all_sources = [r[0] for r in conn.execute(text("SELECT name FROM sources ORDER BY name"))]

    schedules: list[dict] = []
    # A registered source with no bound concepts is reported as unscheduled with
    # its reason rather than omitted — same refuse-with-reasons rule as the
    # binding report (a silently missing source reads as an oversight).
    for source in all_sources:
        if source not in rows:
            schedules.append({
                "source": source,
                "cron": None,
                "cadence": "unscheduled",
                "bound_concept_count": 0,
                "frequencies": [],
                "rationale": (
                    "no bound concepts — every manifest column was refused at "
                    "binding time, so there is nothing to harvest"
                ),
            })
    for source, freqs in sorted(rows.items()):
        finest = next((f for f in FINEST_ORDER if f in freqs), None)
        covered = sorted(freqs, key=lambda f: FINEST_ORDER.index(f)
                         if f in FINEST_ORDER else len(FINEST_ORDER))
        if finest is None:
            # Every bound concept is 'irregular' (as-of snapshots, no period axis).
            schedules.append({
                "source": source,
                "cron": None,
                "cadence": "irregular",
                "bound_concept_count": sum(freqs.values()),
                "frequencies": covered,
                "rationale": (
                    "bound concepts are all 'irregular' (as-of snapshots with no "
                    "period axis), so no cron cadence is derivable — run manually"
                ),
            })
            continue
        schedules.append({
            "source": source,
            "cron": FREQ_CRON[finest],
            "cadence": finest,
            "bound_concept_count": sum(freqs.values()),
            "frequencies": covered,
            "rationale": (
                f"finest declared frequency among this source's bound concepts is "
                f"'{finest}' (bound cadences: {', '.join(covered)}); a {finest} tick "
                f"is at least as frequent as every concept it covers, and the planner "
                f"expands dates per concept frequency so coarser ones only fire on "
                f"their own periods"
            ),
        })
    return schedules


def emit_schedules_json(schedules: list[dict]) -> Path:
    payload = {
        "change": "schedule-activation",
        "generated_from": "concept_bindings (provenance=schedule-activation)",
        "applied": False,
        "note": "artifacts only — no crontab/launchd/CronJob was created or modified",
        "schedules": schedules,
    }
    path = OUTPUT_DIR / "schedules.json"
    path.write_text(json.dumps(payload, indent=2) + "\n")
    return path


def emit_cronjob(schedules: list[dict]) -> Path:
    """Cluster CronJob template mirroring k8s/coverage-expander-cronjob.yaml."""
    akshare = next((s for s in schedules if s["cron"]), None)
    schedule = akshare["cron"] if akshare else "0 6 * * *"
    source = akshare["source"] if akshare else "akshare"
    cadence = akshare["cadence"] if akshare else "daily"
    path = OUTPUT_DIR / "cronjob-schedule-activation.yaml"
    path.write_text(f"""# schedule-activation — incremental harvest CronJob TEMPLATE (not applied).
#
# Emitted, not applied (spec: Emitted, not applied, scheduling artifacts).
# Mirror of k8s/coverage-expander-cronjob.yaml: same namespace, service account,
# image, secret-sourced env, kubeconfig volume and resource shape.
#
# Cadence rationale: {source}'s finest bound concept frequency is '{cadence}'
# (see schedules.json) — this tick is at least as frequent as every concept the
# source covers; the planner re-expands dates per concept frequency, so coarser
# cadences only fire on their own periods.
#
# To activate (OPERATOR action — this change does not):
#   kubectl apply -f cronjob-schedule-activation.yaml
#   kubectl patch cronjob schedule-activation -n fd-master -p '{{"spec":{{"suspend":false}}}}'
# Rollback:
#   kubectl delete cronjob schedule-activation -n fd-master
apiVersion: batch/v1
kind: CronJob
metadata:
  name: schedule-activation
  namespace: fd-master
  labels:
    app: schedule-activation
spec:
  schedule: "{schedule}"   # {cadence}, derived from bound concept frequency
  suspend: true            # TEMPLATE: operator opts in
  concurrencyPolicy: Forbid
  successfulJobsHistoryLimit: 3
  failedJobsHistoryLimit: 5
  jobTemplate:
    spec:
      template:
        spec:
          serviceAccountName: crawl-reconciler
          restartPolicy: OnFailure
          imagePullSecrets:
          - name: fd-harbor-pull
          containers:
          - name: incremental-crawl
            image: harbor.local/lawcraw_business/scraw-fd-open-data-mcp:latest
            imagePullPolicy: IfNotPresent
            command: ["scraw-fd-open-data-mcp", "crawl"]
            args: ["/plans/incremental_plan.json"]
            env:
            - name: FD_OPEN_DATA_MCP_DATABASE_URL
              valueFrom:
                secretKeyRef: {{name: fd-db-secret, key: DATABASE_URL}}
            - name: REDIS_URL
              valueFrom:
                secretKeyRef: {{name: fd-db-secret, key: REDIS_URL}}
            - name: PYTHONUNBUFFERED
              value: "1"
            volumeMounts:
            - name: plans
              mountPath: /plans
              readOnly: true
            resources:
              requests: {{memory: "256Mi", cpu: "100m"}}
              limits: {{memory: "1Gi", cpu: "1000m"}}
          volumes:
          # The generated incremental plan (design D2) — supply as a ConfigMap or
          # PVC; the operator decides which, this template only names the mount.
          - name: plans
            configMap:
              name: schedule-activation-plans
""")
    return path


def emit_launchd(schedules: list[dict]) -> Path:
    """Local launchd template (macOS). Same cadence as the cluster template."""
    akshare = next((s for s in schedules if s["cron"]), None)
    hour, minute = (6, 17) if not akshare else _cron_hm(akshare["cron"])
    path = OUTPUT_DIR / "com.finddata.schedule-activation.plist"
    path.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<!-- schedule-activation — local incremental harvest TEMPLATE (not loaded).
     Emitted, not applied. Install (OPERATOR action — this change does not):
       cp com.finddata.schedule-activation.plist ~/Library/LaunchAgents/
       launchctl load ~/Library/LaunchAgents/com.finddata.schedule-activation.plist
     Rollback:
       launchctl unload ~/Library/LaunchAgents/com.finddata.schedule-activation.plist
     Cadence follows the same finest-bound-frequency rule as schedules.json. -->
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.finddata.schedule-activation</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/chengsishi/finddata/scraw-fd-open-data-mcp/.venv/bin/scraw-fd-open-data-mcp</string>
    <string>crawl</string>
    <string>{OUTPUT_DIR}/incremental_plan.json</string>
  </array>
  <key>WorkingDirectory</key>
  <string>/Users/chengsishi/finddata/scraw-fd-open-data-mcp</string>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key><integer>{hour}</integer>
    <key>Minute</key><integer>{minute}</integer>
  </dict>
  <key>StandardOutPath</key>
  <string>{OUTPUT_DIR}/launchd.out.log</string>
  <key>StandardErrorPath</key>
  <string>{OUTPUT_DIR}/launchd.err.log</string>
</dict>
</plist>
""")
    return path


def _cron_hm(cron: str) -> tuple[int, int]:
    """(hour, minute) from a 'M H ...' cron expression."""
    minute, hour = cron.split()[:2]
    return int(hour), int(minute)


def verify_nothing_scheduled() -> list[str]:
    """Corroborate the spec scenario: nothing was applied to any scheduler."""
    findings: list[str] = []

    crontab = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    hit = [ln for ln in crontab.stdout.splitlines() if "schedule-activation" in ln]
    findings.append(f"crontab: {'LEAKED ' + str(hit) if hit else 'clean (no schedule-activation entry)'}")

    launchctl = subprocess.run(["launchctl", "list"], capture_output=True, text=True)
    hit = [ln for ln in launchctl.stdout.splitlines() if "schedule-activation" in ln]
    findings.append(f"launchd: {'LEAKED ' + str(hit) if hit else 'clean (com.finddata.schedule-activation not loaded)'}")

    kubectl = subprocess.run(
        ["kubectl", "get", "cronjob", "schedule-activation", "-n", "fd-master",
         "--request-timeout=8s"], capture_output=True, text=True)
    if "NotFound" in kubectl.stderr or "not found" in kubectl.stderr:
        findings.append("cluster: clean (CronJob schedule-activation not found in fd-master)")
    else:
        findings.append(f"cluster: unverified from this host ({kubectl.stderr.strip().splitlines()[-1] if kubectl.stderr.strip() else 'kubectl unavailable'})")
    return findings


def self_check(schedules: list[dict]) -> None:
    """The cadence derivation is the only non-trivial logic — check it."""
    by_source = {s["source"]: s for s in schedules}
    assert by_source, "no sources derived"
    assert "akshare" in by_source, "akshare has bindings and must appear"
    ak = by_source["akshare"]
    # daily is present among akshare's bound frequencies, so it must win.
    assert ak["cadence"] == "daily", f"expected daily, got {ak['cadence']}"
    assert ak["cron"] == FREQ_CRON["daily"], ak["cron"]
    assert "daily" in ak["frequencies"] and "yearly" in ak["frequencies"]
    # A source with only yearly bindings would pick yearly, not daily.
    only_yearly = derive_from({("x", "yearly"): 1})
    assert only_yearly[0]["cadence"] == "yearly", only_yearly
    # Irregular-only sources get no cron.
    only_irregular = derive_from({("y", "irregular"): 1})
    assert only_irregular[0]["cron"] is None, only_irregular
    print("self-check OK: cadence derivation (finest-wins, irregular -> no cron)")


def derive_from(freqs: dict[tuple[str, str], int]) -> list[dict]:
    """Pure form of the cadence rule, for checking without a DB."""
    grouped: dict[str, dict[str, int]] = {}
    for (source, frequency), n in freqs.items():
        grouped.setdefault(source, {})[frequency] = n
    out = []
    for source, f in sorted(grouped.items()):
        finest = next((x for x in FINEST_ORDER if x in f), None)
        out.append({"source": source, "cadence": finest or "irregular",
                    "cron": FREQ_CRON[finest] if finest else None})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db-url", default=os.environ.get("FD_OPEN_DATA_MCP_DATABASE_URL", DEFAULT_URL))
    ap.add_argument("--verify-only", action="store_true",
                    help="only re-check that nothing is scheduled")
    args = ap.parse_args()

    if not args.verify_only:
        schedules = derive_schedules(args.db_url)
        self_check(schedules)
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        for p in (emit_schedules_json(schedules),
                  emit_cronjob(schedules),
                  emit_launchd(schedules)):
            print(f"emitted {p}")
        print("\n=== per-source cadence ===")
        for s in schedules:
            print(f"  {s['source']:12s} {s['cadence']:10s} cron={s['cron']}"
                  f"  ({s['bound_concept_count']} bound concepts)")
            print(f"    {s['rationale']}")

    print("\n=== nothing-scheduled verification ===")
    for line in verify_nothing_scheduled():
        print(f"  {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
