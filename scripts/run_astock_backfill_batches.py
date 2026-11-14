#!/usr/bin/env python3
"""Checkpointed batch runner around bulk_ingest_astock_daily.py (2.2 backfill).

Slices a symbol list into batches, runs the ingest script per batch, records
per-batch checkpoints (boundaries, rows, duration, exit code) into a JSON
file, and resumes by skipping batches already marked done. A failed batch
stops the loop (explicit failure) — re-running the same command resumes from
the failed batch; already-done batches are skipped via the script's own
existing-key idempotency even if re-executed.

Usage:
    python run_astock_backfill_batches.py \
        --db-url <master dsn> \
        --symbols-file /path/symbols.txt \
        --start 2026-07-11 --end 2026-11-05 \
        --phase whitelist \
        --checkpoint /path/astock-restore/checkpoint-whitelist.json \
        --log-dir /path/astock-restore/logs \
        [--batch-size 50] [--sleep-s 20] [--limit-batches N] [--dry]
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
INGEST = os.path.join(HERE, "bulk_ingest_astock_daily.py")
PY = os.environ.get("INGEST_PY", sys.executable)

DONE_RE = re.compile(r"done: (\d+) rows")
FAIL_RE = re.compile(r"^FAILED: (.+)$", re.M)


def load_symbols(path: str) -> list[str]:
    out: list[str] = []
    for line in open(path, encoding="utf-8"):
        c = line.strip()
        if c:
            out.append(c.zfill(6))
    return sorted(set(out))


def load_checkpoint(path: str, phase: str) -> dict:
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {"phase": phase, "batches": {}}


def save_checkpoint(path: str, cp: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cp, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


def acquire_writer_lock(path: str):
    """Enforce one-astock-writer-at-a-time.

    The 2026-11-08 03:44 freeze was caused by *two* writers hitting the same 4GB
    shard through master's FDW: this whitelist backfill and the universe-mode
    policy 2659 job. Rows are byte-idempotent either way, but the shard is not
    (no swap headroom, zero memory limits) — so a second runner must refuse to
    start rather than pile on.
    """
    import fcntl
    fh = open(path, "w", encoding="utf-8")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        raise SystemExit(
            f"another astock writer holds {path} — refusing to start. "
            "Only one writer may touch the shard at a time (postmortem P0').")
    fh.write(str(os.getpid()))
    fh.flush()
    return fh  # keep the handle alive for the process lifetime


def effective_batch_size(cp: dict, cli_size: int) -> int:
    """Batch size to use, preferring the checkpoint's stored value on resume.

    The batch index IS the checkpoint key (``cp["batches"]["9"]``), so changing
    the size mid-phase renumbers every key. Concretely: the whitelist checkpoint
    was built 50-wide (key 9 = codes[450:500]); resuming it with the new
    throttle default of 10 would read key 9 as "codes[90:100] done" and either
    re-run or, worse, skip ranges. Resume continuity wins over the new default;
    a fresh checkpoint still gets the throttle.
    """
    stored = (cp.get("plan") or {}).get("batch_size")
    return int(stored) if stored else cli_size


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-url", required=True)
    ap.add_argument("--symbols-file", required=True)
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--phase", required=True)
    ap.add_argument("--checkpoint", required=True)
    ap.add_argument("--log-dir", required=True)
    ap.add_argument("--batch-size", type=int, default=10,
                    help="symbols per batch. Default 10 is the postmortem P0' "
                         "write-throttle for the 4GB shards (50-wide batches plus a "
                         "concurrent universe job is what froze xinru3 on 2026-11-08)")
    ap.add_argument("--sleep-s", type=int, default=30,
                    help="pause between batches (default 30s: give the shard's "
                         "page cache time to settle)")
    ap.add_argument("--lock-file",
                    help="single-writer lock (default <checkpoint>.lock); a second "
                         "runner aborts instead of double-loading the shard")
    ap.add_argument("--limit-batches", type=int, default=0,
                    help="run at most N pending batches this invocation")
    ap.add_argument("--allow-new-series", action="store_true",
                    help="透传给 ingest：显式代码即使家族里没有行也建序列"
                         "（新股扩面，见 openspec add-astock-new-series-flag）")
    ap.add_argument("--batch-timeout", type=int, default=1800,
                    help="hard wall-clock cap per batch (s). A dead shard makes "
                         "the child block inside an FDW COMMIT forever: libpq's "
                         "default keepalive is 2h and statement_timeout does not "
                         "cover COMMIT. Measured 2026-11-08: batch 010 sat 17min "
                         "at 0%% CPU this way. 0 disables the cap.")
    ap.add_argument("--dry", action="store_true", help="print plan only")
    args = ap.parse_args()

    lock = acquire_writer_lock(args.lock_file or (args.checkpoint + ".lock"))

    codes = load_symbols(args.symbols_file)
    cp = load_checkpoint(args.checkpoint, args.phase)
    cp.setdefault("batches", {})
    bs = effective_batch_size(cp, args.batch_size)
    if args.allow_new_series:
        print("new-series mode: ingest gets --allow-new-series", flush=True)
    if bs != args.batch_size:
        print(f"note: checkpoint was built with --batch-size {bs}; keeping it "
              f"(ignoring {args.batch_size}) — the batch index IS the checkpoint "
              f"key, so renumbering would skip or repeat symbol ranges", flush=True)
    batches = [codes[i:i + bs] for i in range(0, len(codes), bs)]
    cp["plan"] = {"n_symbols": len(codes), "batch_size": bs,
                  "n_batches": len(batches), "start": args.start,
                  "end": args.end, "symbols_file": args.symbols_file}
    os.makedirs(args.log_dir, exist_ok=True)

    pending = [(i, b) for i, b in enumerate(batches)
               if cp["batches"].get(str(i), {}).get("status") != "done"]
    print(f"plan: {len(codes)} symbols -> {len(batches)} batches "
          f"({len(batches) - len(pending)} done, {len(pending)} pending)")
    if args.dry:
        for i, b in pending[:5]:
            print(f"  pending batch {i:03d}: {b[0]}..{b[-1]} ({len(b)})")
        return 0

    ran = 0
    for i, batch in pending:
        if args.limit_batches and ran >= args.limit_batches:
            print(f"stopping: --limit-batches {args.limit_batches} reached")
            break
        key = str(i)
        syms = ",".join(batch)
        log_path = os.path.join(args.log_dir,
                                f"{args.phase}-batch{i:03d}.log")
        cmd = [PY, INGEST, "--db-url", args.db_url,
               "--symbols", syms,
               "--start", args.start, "--end", args.end]
        if args.allow_new_series:
            cmd.append("--allow-new-series")
        env = dict(os.environ)
        env.update({"http_proxy": "", "https_proxy": "",
                    "HTTP_PROXY": "", "HTTPS_PROXY": "",
                    "FD_PROXY_POOL": "off", "PYTHONUNBUFFERED": "1"})
        t0 = time.time()
        print(f"[batch {i:03d}/{len(batches) - 1}] {batch[0]}..{batch[-1]} "
              f"({len(batch)} symbols) -> {log_path}", flush=True)
        timed_out = False
        rc = 0
        with open(log_path, "w", encoding="utf-8") as lf:
            try:
                proc = subprocess.run(cmd, stdout=lf, stderr=subprocess.STDOUT,
                                      env=env,
                                      timeout=args.batch_timeout or None)
                rc = proc.returncode
            except subprocess.TimeoutExpired:
                # subprocess.run already killed the child; its DB session goes
                # away with the socket. Record an explicit failure so the next
                # invocation resumes here instead of hanging forever.
                timed_out = True
                rc = 124
        dur = round(time.time() - t0, 1)
        out = open(log_path, encoding="utf-8").read()
        m = DONE_RE.search(out)
        rows = int(m.group(1)) if m else None
        fails = FAIL_RE.findall(out)
        entry = {"first": batch[0], "last": batch[-1], "n": len(batch),
                 "exit": rc, "rows": rows,
                 "n_failed_legs": len(fails), "duration_s": dur,
                 "finished_at": datetime.datetime.now().isoformat(timespec="seconds"),
                 "log": log_path}
        if timed_out:
            entry["reason"] = f"timeout after {args.batch_timeout}s"
        cp["batches"][key] = entry
        if rc != 0:
            entry["status"] = "failed"
            save_checkpoint(args.checkpoint, cp)
            if timed_out:
                print(f"[batch {i:03d}] TIMEOUT after {dur}s "
                      f"(cap {args.batch_timeout}s) — child killed, checkpoint "
                      f"saved; check shard reachability, then re-run to resume",
                      flush=True)
                # A dead shard fails every remaining batch the same way; stop
                # and let a human look instead of burning the whole cap N times.
                return 2
            print(f"[batch {i:03d}] FAILED exit={rc} "
                  f"after {dur}s — checkpoint saved; fix and re-run to resume",
                  flush=True)
            return 1
        entry["status"] = "done"
        save_checkpoint(args.checkpoint, cp)
        ran += 1
        print(f"[batch {i:03d}] done rows={rows} failed_legs={len(fails)} "
              f"{dur}s", flush=True)
        if i != batches[-1] and args.sleep_s:
            time.sleep(args.sleep_s)

    done = sum(1 for e in cp["batches"].values() if e.get("status") == "done")
    print(f"phase '{args.phase}': {done}/{len(batches)} batches done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
