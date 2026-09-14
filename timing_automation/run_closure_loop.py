#!/usr/bin/env python3
"""Mode-aware nEBULA candidate loop over one trusted analysis packet.

This driver is deliberately conservative.  It can compare multiple independent
candidates against one analysed baseline (``--chain baseline``).  In
``--chain accept`` mode it stops immediately after a promotion and returns the
promoted manifest, because continuing with the old timing diagnosis would be
unsound: the critical path may have moved.  Re-run ``run_analysis.py`` on the
promoted RTL, then start/resume the loop with the new optimizer input.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from closure_policy import ClosurePolicy, evaluate_stop
    from optimizer_history import tried_patterns
except ImportError:
    from timing_automation.closure_policy import ClosurePolicy, evaluate_stop
    from timing_automation.optimizer_history import tried_patterns

SCRIPT_DIR = Path(__file__).resolve().parent
SCHEMA = "nebula_closure_loop/1"


def load_json(path: Path) -> Dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"expected JSON object: {path}")
    return value


def write_json(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def run(cmd: List[str], *, cwd: Path) -> int:
    print("\n$ " + " ".join(cmd))
    return subprocess.run(cmd, cwd=str(cwd)).returncode


def request_ready(unit: Dict[str, Any]) -> bool:
    e = unit.get("eligibility") or {}
    return e.get("request_ready", e.get("optimization_ready")) is True


def unit_worst_slack(unit: Dict[str, Any]) -> float:
    v = (unit.get("timing_summary") or {}).get("worst_slack_ns")
    try:
        return float(v)
    except (TypeError, ValueError):
        return float("inf")


def select_unit(packet: Dict[str, Any], explicit: Optional[str]) -> Dict[str, Any]:
    units = [u for u in packet.get("optimization_units", []) if isinstance(u, dict) and request_ready(u)]
    if explicit:
        for u in units:
            if u.get("unit_id") == explicit:
                return u
        raise RuntimeError(f"request-ready unit not found: {explicit}")
    if not units:
        raise RuntimeError("optimizer packet has no request-ready unit")
    return sorted(units, key=lambda u: (unit_worst_slack(u), str(u.get("unit_id"))))[0]


def metrics_from_result(result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    p = result.get("metrics")
    if not isinstance(p, str):
        return None
    path = Path(p)
    if not path.is_file():
        return None
    obj = load_json(path)
    metrics = obj.get("metrics")
    return dict(metrics) if isinstance(metrics, dict) else None


def main() -> int:
    ap = argparse.ArgumentParser(description="Run guided/exploratory nEBULA candidate attempts against one trusted analysis packet.")
    ap.add_argument("--optimizer-input", required=True)
    ap.add_argument("--golden-manifest", required=True)
    ap.add_argument("--golden-base-dir", required=True)
    ap.add_argument("--physical-recipe", required=True)
    ap.add_argument("--baseline-metrics", required=True)
    ap.add_argument("--flow-dir", default=".")
    ap.add_argument("--ai-runs")
    ap.add_argument("--patterns-dir")
    ap.add_argument("--unit-id")
    ap.add_argument("--max-attempts", type=int, default=3)  # intentionally 3 here; run_rtl_opt_groq.py uses 5
    ap.add_argument("--max-flows", type=int, default=3)
    ap.add_argument("--target-wns", type=float, default=0.0)
    ap.add_argument("--min-gain-ns", type=float, default=0.005)
    ap.add_argument("--noise-floor-wns-ns", type=float, default=0.0)
    ap.add_argument("--chain", choices=("baseline", "accept"), default="baseline")
    ap.add_argument("--model", default=os.environ.get("OPTIMIZER_MODEL", "openai/gpt-oss-120b"))
    ap.add_argument("--max-tokens", type=int, default=4096)
    ap.add_argument("--reasoning-effort", choices=("low", "medium", "high"), default="medium")
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--history-file")
    ap.add_argument("--output")
    ap.add_argument("--no-orfs", action="store_true")
    ap.add_argument("--yes", action="store_true", help="Acknowledge physical-flow budget.")
    args = ap.parse_args()

    flow = Path(args.flow_dir).resolve()
    ai_runs = Path(args.ai_runs).resolve() if args.ai_runs else flow / "ai_runs"
    ai_runs.mkdir(parents=True, exist_ok=True)
    packet_path = Path(args.optimizer_input).resolve()
    packet = load_json(packet_path)
    validation = packet.get("validation") or {}
    if validation.get("packet_valid", validation.get("optimizer_ready")) is not True:
        raise RuntimeError("optimizer input failed the hard trust boundary")
    if validation.get("request_ready", validation.get("optimizer_ready")) is not True:
        raise RuntimeError("optimizer input has no request-ready work")

    unit = select_unit(packet, args.unit_id)
    unit_id = str(unit["unit_id"])
    history = Path(args.history_file).resolve() if args.history_file else ai_runs / "optimizer_history.jsonl"
    out_path = Path(args.output).resolve() if args.output else ai_runs / "closure_results.json"

    if not args.no_orfs and not args.yes:
        raise RuntimeError(f"physical closure may run up to {args.max_flows} candidate flows; pass --yes to acknowledge")

    baseline_obj = load_json(Path(args.baseline_metrics).resolve())
    baseline_metrics = baseline_obj.get("metrics") if isinstance(baseline_obj.get("metrics"), dict) else baseline_obj
    policy = ClosurePolicy(
        target_wns_ns=args.target_wns,
        min_gain_ns=args.min_gain_ns,
        max_flows=args.max_flows,
        noise_floor_wns_ns=args.noise_floor_wns_ns,
    )

    document: Dict[str, Any] = {
        "schema": SCHEMA,
        "optimizer_input": str(packet_path),
        "unit_id": unit_id,
        "initial_optimizer_mode": unit.get("optimizer_mode", "guided"),
        "chain": args.chain,
        "attempts": [],
        "flows_used": 0,
        "stop_reason": None,
    }
    promoted_metrics: List[Dict[str, Any]] = []

    for iteration in range(1, args.max_attempts + 1):
        decision = evaluate_stop(
            baseline_metrics,
            promoted_metrics,
            policy,
            flows_used=document["flows_used"],
            has_requestable_problem=True,
        )
        if decision.stop:
            document["stop_reason"] = decision.reason
            break

        prefix = ai_runs / f"closure_{unit_id}_iter{iteration:02d}"
        recommendations = prefix.with_suffix(".patterns.json")
        request_path = prefix.with_suffix(".request.json")
        tag = f"closure_{unit_id}_iter{iteration:02d}"
        mode = str(unit.get("optimizer_mode") or "guided")
        eligible_arg: List[str] = []

        if mode == "guided":
            excluded = sorted(tried_patterns(history, unit_id))
            cmd = [
                sys.executable, str(SCRIPT_DIR / "filter_patterns.py"),
                "--optimizer-input", str(packet_path), "--unit-id", unit_id,
                "--output", str(recommendations),
            ]
            if args.patterns_dir:
                cmd += ["--patterns-dir", str(Path(args.patterns_dir).resolve())]
            if excluded:
                cmd += ["--exclude-patterns", ",".join(excluded)]
            if run(cmd, cwd=flow) != 0:
                raise RuntimeError("pattern recommendation stage failed")
            rec = load_json(recommendations)
            if not rec.get("exhausted"):
                eligible_arg = ["--eligible", str(recommendations)]
            else:
                print("Guided recommendations exhausted; falling back to exploratory request.")

        cmd = [
            sys.executable, str(SCRIPT_DIR / "build_request.py"),
            "--optimizer-input", str(packet_path), "--unit-id", unit_id,
            "--golden-manifest", str(Path(args.golden_manifest).resolve()),
            "--golden-base-dir", str(Path(args.golden_base_dir).resolve()),
            "--history", str(history), "--iteration-number", str(iteration),
            "--output", str(request_path),
        ] + eligible_arg
        if run(cmd, cwd=flow) != 0:
            raise RuntimeError("request construction failed")

        actual_request = load_json(request_path)
        actual_mode = str(actual_request.get("optimizer_mode") or mode)
        optimizer_cmd = [
            sys.executable, str(SCRIPT_DIR / "run_optimizer.py"),
            "--request", str(request_path),
            "--golden-manifest", str(Path(args.golden_manifest).resolve()),
            "--golden-base-dir", str(Path(args.golden_base_dir).resolve()),
            "--physical-recipe", str(Path(args.physical_recipe).resolve()),
            "--baseline-metrics", str(Path(args.baseline_metrics).resolve()),
            "--flow-dir", str(flow), "--ai-runs", str(ai_runs), "--tag", tag,
            "--history-file", str(history), "--model", args.model,
            "--max-tokens", str(args.max_tokens), "--reasoning-effort", args.reasoning_effort,
            "--temperature", str(args.temperature),
        ]
        if args.no_orfs:
            optimizer_cmd.append("--no-orfs")
        rc = run(optimizer_cmd, cwd=flow)
        result_path = ai_runs / f"{tag}.result.json"
        if not result_path.is_file():
            raise RuntimeError(f"optimizer did not create result artifact (exit {rc}): {result_path}")
        result = load_json(result_path)
        verdict = str(result.get("verdict") or "error")
        row = {
            "iteration": iteration,
            "optimizer_mode": actual_mode,
            "request": str(request_path),
            "result": str(result_path),
            "verdict": verdict,
            "note": result.get("note"),
            "exit_code": rc,
        }
        metrics = metrics_from_result(result)
        if metrics:
            row["metrics"] = metrics
        document["attempts"].append(row)

        if verdict == "error":
            document["stop_reason"] = "infrastructure_error"
            break
        if not args.no_orfs and result.get("metrics"):
            document["flows_used"] += 1
        if verdict == "promote":
            if metrics:
                promoted_metrics.append(metrics)
            if args.chain == "accept":
                document["stop_reason"] = "reanalyze_promoted_rtl"
                document["promoted_candidate_bundle"] = result.get("promoted_candidate_bundle")
                break
        elif verdict == "equivalent_no_ppa":
            document["stop_reason"] = "formal_only_complete"
            break

    if document["stop_reason"] is None:
        document["stop_reason"] = "attempts_exhausted"
    write_json(out_path, document)
    print("\n" + "=" * 72)
    print("nEBULA CLOSURE LOOP")
    print("=" * 72)
    print(f"Unit       : {unit_id}")
    print(f"Attempts   : {len(document['attempts'])}")
    print(f"Flows used : {document['flows_used']}")
    print(f"Stop       : {document['stop_reason']}")
    print(f"Results    : {out_path}")
    print("=" * 72)
    return 0 if document["stop_reason"] != "infrastructure_error" else 2


if __name__ == "__main__":
    raise SystemExit(main())
