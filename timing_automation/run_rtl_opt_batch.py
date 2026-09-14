#!/usr/bin/env python3
"""Fault-isolated multi-design wrapper for rtl-opt-groq.

Each design runs in its own subprocess/output directory so one baseline/tool
failure cannot terminate the rest of a benchmark sweep.  Resume is intentionally
design-granular: completed designs are skipped; failed/incomplete designs can be
archived and retried without destroying evidence.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = "rtl_opt_batch/1"
ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "timing_automation" / "run_rtl_opt_groq.py"


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def summary_status(directory: Path) -> tuple[bool, str | None]:
    summary = directory / "groq_summary.json"
    if not summary.is_file():
        return False, None
    try:
        data = json.loads(summary.read_text())
    except Exception:
        return False, "corrupt_summary"
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list) or not results:
        return False, "empty_summary"
    statuses = [str(r.get("status") or "unknown") for r in results if isinstance(r, dict)]
    # A summary is complete even if the design legitimately failed baseline or
    # exhausted search; retry_failed controls whether such outcomes are rerun.
    return True, statuses[-1] if statuses else "unknown"


def archive_existing(path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = path.with_name(path.name + f".previous_{stamp}")
    counter = 1
    while target.exists():
        target = path.with_name(path.name + f".previous_{stamp}_{counter}")
        counter += 1
    path.rename(target)
    return target


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--pairs", nargs="+", required=True)
    ap.add_argument("--resume", action="store_true", help="skip completed design directories")
    ap.add_argument("--retry-failed", action="store_true", help="archive and rerun completed non-success designs")
    ap.add_argument("--max-design-runtime-min", type=float, default=0.0,
                    help="subprocess wall-clock limit per design; 0 disables")
    args, passthrough = ap.parse_known_args()
    root = args.output.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    state_path = root / "batch_state.json"
    state = {
        "schema": SCHEMA,
        "pairs": list(args.pairs),
        "results": {},
        "passthrough_args": passthrough,
    }
    if state_path.is_file() and args.resume:
        try:
            old = json.loads(state_path.read_text())
            if isinstance(old.get("results"), dict):
                state["results"].update(old["results"])
        except Exception:
            pass
    write_json(state_path, state)

    timeout = args.max_design_runtime_min * 60 if args.max_design_runtime_min > 0 else None
    overall_rc = 0
    for pair in args.pairs:
        design_dir = root / pair
        complete, prior_status = summary_status(design_dir)
        retryable_failure = complete and prior_status not in {"promoted", "converged", "max_attempts", "max_meaningful_iterations_reached"}
        if complete and args.resume and not (args.retry_failed and retryable_failure):
            state["results"][pair] = {"status": "skipped_completed", "prior_status": prior_status, "output": str(design_dir)}
            write_json(state_path, state)
            continue
        if design_dir.exists():
            if args.retry_failed or not complete:
                archived = archive_existing(design_dir)
            else:
                raise SystemExit(f"output exists for {pair}: {design_dir}; use --resume or --retry-failed")
        else:
            archived = None

        cmd = [sys.executable, str(RUNNER), "--output", str(design_dir), "--pairs", pair, *passthrough]
        record = {"status": "running", "command": cmd, "output": str(design_dir)}
        if archived is not None:
            record["archived_previous"] = str(archived)
        state["results"][pair] = record
        write_json(state_path, state)
        try:
            proc = subprocess.run(cmd, cwd=str(ROOT), timeout=timeout, env=os.environ.copy())
            complete, final_status = summary_status(design_dir)
            record.update(
                status="completed" if proc.returncode == 0 and complete else "failed",
                returncode=proc.returncode,
                final_status=final_status,
            )
            if proc.returncode != 0:
                overall_rc = 1
        except subprocess.TimeoutExpired:
            record.update(status="design_timeout", timeout_seconds=timeout)
            overall_rc = 1
        except Exception as exc:
            record.update(status="runner_exception", reason=f"{type(exc).__name__}: {exc}")
            overall_rc = 1
        write_json(state_path, state)

    # Aggregate opportunistically; failure to aggregate must not erase run state.
    aggregate = ROOT / "timing_automation" / "aggregate_results.py"
    if aggregate.is_file():
        subprocess.run([sys.executable, str(aggregate), "--root", str(root), "--output-prefix", str(root / "aggregate_results")], cwd=str(ROOT), check=False)
    print(f"Batch results: {root}")
    return overall_rc


if __name__ == "__main__":
    raise SystemExit(main())
