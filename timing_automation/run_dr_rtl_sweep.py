#!/usr/bin/env python3

"""
run_dr_rtl_sweep.py

Runs run_dr_rtl_one.py across all (or a chosen subset of) DR_RTL designs and
prints/writes a PASS/SKIP/FAIL summary. This is the batch entry point for the
20-design experiment; run_dr_rtl_one.py itself remains the single-design tool.

Each design gets its own subprocess invocation of run_dr_rtl_one.py, so one
design's failure never aborts the sweep -- the point of employing the OSS
hardening fix is precisely to turn FIFO/SPI/UART from SKIP/FAIL into PASS
without touching the other 17.

USAGE
-----
    # All 20 designs, hardening on (default), fresh run:
    python3 run_dr_rtl_sweep.py

    # Only the three designs the fix targets, verbose:
    python3 run_dr_rtl_sweep.py --designs FIFO SPI UART -v

    # Compare against the old (unhardened) behavior:
    python3 run_dr_rtl_sweep.py --no-harden --out sweep_baseline.json

    # Re-run only designs that did not PASS last time:
    python3 run_dr_rtl_sweep.py --resume-from sweep_results.json

OUTPUT
------
Writes a JSON summary (default: sweep_results.json) with one record per
design: status (PASS/SKIP/FAIL/ERROR), return code, elapsed time, and the
tail of stdout/stderr for anything that did not PASS. Prints a compact table
to stdout as it goes and a final summary at the end.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
RUN_ONE = SCRIPT_DIR / "run_dr_rtl_one.py"

FLOW_DIR = SCRIPT_DIR.parent
DR_RTL_METADATA = FLOW_DIR / "external" / "DR_RTL" / "syn_flow" / "design_all.json"

# Tail of captured output kept per non-PASS design, for the JSON summary.
OUTPUT_TAIL_CHARS = 4000

STATUS_BY_RC = {
    0: "PASS",
    2: "SKIP",
}


def load_design_names() -> List[str]:
    if not DR_RTL_METADATA.is_file():
        print(f"ERROR: DR_RTL metadata not found: {DR_RTL_METADATA}", file=sys.stderr)
        print(
            "Clone it first:\n"
            "  cd <ORFS>/flow\n"
            "  mkdir -p external\n"
            "  git clone --depth 1 https://github.com/hkust-zhiyao/DR_RTL.git "
            "external/DR_RTL",
            file=sys.stderr,
        )
        sys.exit(1)
    data = json.loads(DR_RTL_METADATA.read_text(encoding="utf-8"))
    return sorted(data, key=str.lower)


def run_one(
    design: str,
    clock_period: float,
    harden: bool,
    memory_max_bits: int,
    max_adaptations: int,
    max_die_side: float,
    extra_args: List[str],
    verbose: bool,
) -> Dict[str, object]:
    cmd = [
        sys.executable,
        str(RUN_ONE),
        "--design",
        design,
        "--clock-period",
        str(clock_period),
        "--synth-memory-max-bits",
        str(memory_max_bits),
        "--max-adaptations",
        str(max_adaptations),
        "--max-die-side",
        str(max_die_side),
    ]
    if not harden:
        cmd.append("--no-harden")
    cmd.extend(extra_args)

    start = time.time()

    if verbose:
        proc = subprocess.Popen(
            cmd,
            cwd=FLOW_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        lines: List[str] = []
        assert proc.stdout is not None
        for line in proc.stdout:
            print(f"  [{design}] {line}", end="")
            lines.append(line)
        rc = proc.wait()
        output = "".join(lines)
    else:
        proc = subprocess.run(cmd, cwd=FLOW_DIR, capture_output=True, text=True)
        rc = proc.returncode
        output = (proc.stdout or "") + (proc.stderr or "")

    elapsed = time.time() - start
    status = STATUS_BY_RC.get(rc, "FAIL" if rc == 1 else f"FAIL(rc={rc})")

    record: Dict[str, object] = {
        "design": design,
        "status": status,
        "returncode": rc,
        "elapsed_seconds": round(elapsed, 1),
    }
    if status != "PASS":
        record["output_tail"] = output[-OUTPUT_TAIL_CHARS:]

    return record


def print_row(record: Dict[str, object]) -> None:
    design = record["design"]
    status = record["status"]
    elapsed = record["elapsed_seconds"]
    marker = {"PASS": "PASS", "SKIP": "SKIP"}.get(status, status)
    print(f"  {design:<20} {marker:<14} {elapsed:>8.1f}s")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--designs",
        nargs="+",
        default=None,
        help="Subset of DR_RTL design names to run. Default: all 20.",
    )
    ap.add_argument("--clock-period", type=float, default=0.8)
    ap.add_argument(
        "--no-harden",
        action="store_true",
        help="Disable synthesis hardening for the whole sweep (baseline comparison).",
    )
    ap.add_argument("--synth-memory-max-bits", type=int, default=4096)
    ap.add_argument("--max-adaptations", type=int, default=2)
    ap.add_argument("--max-die-side", type=float, default=1000.0)
    ap.add_argument(
        "--force",
        action="store_true",
        help="Pass --force through to run_dr_rtl_one.py (restage RTL if changed).",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=Path("sweep_results.json"),
        help="Where to write the JSON summary. Default: sweep_results.json",
    )
    ap.add_argument(
        "--resume-from",
        type=Path,
        default=None,
        help=(
            "Path to a prior sweep JSON summary. Designs that PASSed there are "
            "skipped this run; everything else is retried."
        ),
    )
    ap.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Stream each design's run_dr_rtl_one.py output live, prefixed by design name.",
    )
    args = ap.parse_args()

    all_designs = load_design_names()
    designs = args.designs if args.designs else all_designs

    unknown = [d for d in designs if d not in all_designs]
    if unknown:
        print(f"ERROR: unknown design(s): {', '.join(unknown)}", file=sys.stderr)
        print(f"Available: {', '.join(all_designs)}", file=sys.stderr)
        return 1

    already_passed: set = set()
    if args.resume_from is not None:
        if args.resume_from.is_file():
            prior = json.loads(args.resume_from.read_text(encoding="utf-8"))
            for rec in prior.get("results", []):
                if rec.get("status") == "PASS":
                    already_passed.add(rec["design"])
            print(
                f"Resuming from {args.resume_from}: "
                f"{len(already_passed)} design(s) already PASSed, will skip."
            )
        else:
            print(
                f"WARNING: --resume-from file not found: {args.resume_from} "
                "(running full set)",
                file=sys.stderr,
            )

    extra_args: List[str] = []
    if args.force:
        extra_args.append("--force")

    harden = not args.no_harden

    print()
    print("=" * 72)
    print(f"DR_RTL SWEEP  ({len(designs)} design(s), harden={'ON' if harden else 'OFF'})")
    print("=" * 72)
    print(f"{'DESIGN':<20} {'STATUS':<14} {'TIME':>9}")
    print("-" * 72)

    results: List[Dict[str, object]] = []
    sweep_start = time.time()

    for design in designs:
        if design in already_passed:
            record = {
                "design": design,
                "status": "PASS",
                "returncode": 0,
                "elapsed_seconds": 0.0,
                "note": "skipped (already PASSed in --resume-from)",
            }
            results.append(record)
            print_row(record)
            continue

        record = run_one(
            design=design,
            clock_period=args.clock_period,
            harden=harden,
            memory_max_bits=args.synth_memory_max_bits,
            max_adaptations=args.max_adaptations,
            max_die_side=args.max_die_side,
            extra_args=extra_args,
            verbose=args.verbose,
        )
        results.append(record)
        print_row(record)

        # Persist incrementally so a long sweep can be inspected/resumed
        # even if it is interrupted partway through.
        write_summary(args.out, designs, harden, results, sweep_start)

    total_elapsed = time.time() - sweep_start
    write_summary(args.out, designs, harden, results, sweep_start, final=True)

    passed = [r for r in results if r["status"] == "PASS"]
    skipped = [r for r in results if r["status"] == "SKIP"]
    failed = [r for r in results if r["status"] not in ("PASS", "SKIP")]

    print("-" * 72)
    print(f"PASS: {len(passed)}/{len(results)}   SKIP: {len(skipped)}   FAIL: {len(failed)}")
    print(f"Total time: {total_elapsed:.1f}s")
    print(f"Summary written to: {args.out}")

    if skipped:
        print()
        print("SKIPped designs:")
        for r in skipped:
            print(f"  - {r['design']}")

    if failed:
        print()
        print("FAILed designs:")
        for r in failed:
            print(f"  - {r['design']} (rc={r['returncode']})")
        print()
        print(
            "See the JSON summary's output_tail per design, and the "
            "*_baseline_attempt_NN.log files under ai_runs/ for full logs."
        )

    return 0 if not failed else 1


def write_summary(
    out_path: Path,
    designs: List[str],
    harden: bool,
    results: List[Dict[str, object]],
    sweep_start: float,
    final: bool = False,
) -> None:
    payload = {
        "harden": harden,
        "designs_requested": designs,
        "results": results,
        "elapsed_seconds_so_far": round(time.time() - sweep_start, 1),
        "complete": final,
    }
    out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
