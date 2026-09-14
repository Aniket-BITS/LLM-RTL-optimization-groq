"""Offline paired-guidance statistics. No tools, model calls, or RTL reads."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from statistics import median


def aggregate(results):
    attempts = [a for r in results for a in r.get("attempts", [])]
    calls = sum(bool(a.get("groq_call_attempted")) for a in attempts)
    accepted = sum(a.get("status") == "promoted" for a in attempts)
    comparable = [a for a in attempts if a.get("promotion", {}).get("comparable")]
    rate = lambda n, d: n / d if d else None
    metric_names = ("area_delta_pct", "power_delta_pct", "setup_slack_delta_ns", "hold_slack_delta_ns")
    metrics = {}
    for key in metric_names:
        values = [a["ppa_deltas"][key] for a in comparable if key in a.get("ppa_deltas", {})]
        metrics[key] = {
            "n": len(values),
            "median": median(values) if values else None,
            "best": (min(values) if key.endswith("pct") else max(values)) if values else None,
        }
    guidance_attempts = [a for r in results if r.get("arm") != "exploratory" for a in r.get("attempts", [])]
    fallback = sum(a.get("guidance", {}).get("mode_used") == "exploratory" for a in guidance_attempts)
    latency_results = [a.get("latency_result") for a in attempts if isinstance(a.get("latency_result"), dict)]
    verified_latency = [x for x in latency_results if x.get("status") == "verified" and x.get("passed") is True]
    prediction_errors = [x.get("prediction_error_cycles") for x in verified_latency
                         if isinstance(x.get("prediction_error_cycles"), int)]
    successful_designs = sum(
        (int(r.get("current_best_epoch") or 0) > 0) or r.get("status") == "promoted"
        for r in results
    )
    def compat_applied(attempt):
        for section_name in ("synthesis", "ppa", "repeat_ppa"):
            section = attempt.get(section_name)
            if not isinstance(section, dict):
                continue
            for side in ("baseline", "candidate", "reference"):
                run = section.get(side)
                if isinstance(run, dict):
                    compat = run.get("sta_netlist_compat")
                    if isinstance(compat, dict) and compat.get("applied") is True:
                        return True
        return False
    return {
        "design_runs": len(results),
        "attempts": len(attempts),
        "calls": calls,
        "accepted_candidates": accepted,
        "accepted_rate_per_call": rate(accepted, calls),
        "design_success_rate": rate(successful_designs, len(results)),
        "lint_pass_rate": rate(sum(a.get("lint", {}).get("ok") is True for a in attempts), calls),
        "formal_pass_rate": rate(sum(a.get("formal", {}).get("status") == "passed" for a in attempts), calls),
        "exact_source_duplicate_count": sum(a.get("status") == "exact_source_duplicate" for a in attempts),
        "non_executable_no_op_count": sum(a.get("status") == "non_executable_change_only" for a in attempts),
        "synthesis_no_op_rate": rate(sum(a.get("synthesis_no_op") is True for a in attempts), calls),
        "synthesis_duplicate_rate": rate(sum(a.get("synthesis_duplicate") is True for a in attempts), calls),
        "ppa_skipped_duplicate_count": sum(a.get("synthesis_duplicate") is True for a in attempts),
        "model_stop_recommendation_count": sum(a.get("model_decision") == "no_further_optimization" for a in attempts),
        "model_stop_accepted_count": sum(a.get("status") == "model_stop_accepted" for a in attempts),
        "model_stop_rejected_count": sum(a.get("status") == "stop_recommendation_not_supported" for a in attempts),
        "converged_designs": sum(bool(r.get("convergence")) for r in results),
        "convergence_rate": rate(sum(bool(r.get("convergence")) for r in results), len(results)),
        "promotion_count": accepted,
        "final_current_best_epoch_max": max((int(r.get("current_best_epoch") or 0) for r in results), default=0),
        "comparable_ppa_rate": rate(len(comparable), calls),
        "calls_per_accepted_candidate": rate(calls, accepted),
        "guidance_attempts": len(guidance_attempts),
        "fallback_count": fallback,
        "fallback_rate": rate(fallback, len(guidance_attempts)),
        "meaningful_iterations": sum(bool(a.get("meaningful_iteration")) for a in attempts),
        "latency_verified_count": len(verified_latency),
        "latency_budget_exceeded_count": sum(x.get("status") == "latency_budget_exceeded" for x in latency_results),
        "latency_relation_mismatch_count": sum(x.get("status") == "no_allowed_latency_relation" for x in latency_results),
        "formal_inconclusive_count": sum(x.get("status") in {"inconclusive_timeout", "tool_failure", "unsupported_contract"}
                                         for x in latency_results),
        "pipelining_candidate_count": sum(
            a.get("primary_transformation") == "pipeline_insertion"
            or "pipeline_insertion" in (a.get("proposed_transformations") or [])
            for a in attempts
        ),
        "latency_prediction_error_median_cycles": median(prediction_errors) if prediction_errors else None,
        "sta_netlist_compat_attempt_count": sum(compat_applied(a) for a in attempts),
        "physical_failure_reason_counts": {
            reason: sum(
                1 for a in attempts
                for section_name in ("synthesis", "ppa", "repeat_ppa")
                for section in [a.get(section_name)] if isinstance(section, dict)
                for side in ("candidate", "reference", "baseline")
                for run in [section.get(side)] if isinstance(run, dict)
                if run.get("failure_reason") == reason
            )
            for reason in sorted({
                run.get("failure_reason")
                for a in attempts
                for section_name in ("synthesis", "ppa", "repeat_ppa")
                for section in [a.get(section_name)] if isinstance(section, dict)
                for side in ("candidate", "reference", "baseline")
                for run in [section.get(side)] if isinstance(run, dict) and run.get("failure_reason")
            })
        },
        "metrics": metrics,
    }


def summarize(arms):
    return {"schema": "guided_experiment_summary/2", "arms": {
        arm: {"aggregate": aggregate(rows), "per_design": {
            design: aggregate([r for r in rows if r["id"] == design]) for design in sorted({r["id"] for r in rows})}}
        for arm, rows in arms.items()},
        "definitions": {"rates": "per attempted Groq call unless named design_success_rate or fallback_rate",
                        "metrics": "all comparable first PPA comparisons, including rejected candidates; repeat confirmation gates acceptance",
                        "best_direction": "area/power deltas lower is better; setup/hold deltas higher is better",
                        "missing": "null means no observations; never zero-filled"}}


def report(directory):
    arms = {}
    for arm in ("guided", "exploratory"):
        files = sorted((directory / arm).glob("repetition_*/*/result.json"))
        arms[arm] = [json.loads(path.read_text()) for path in files]
    summary = summarize(arms)
    # Match by repetition and design, and expose incomplete pairs explicitly.
    keys = {arm: {str(p.relative_to(directory / arm)): json.loads(p.read_text())
                  for p in sorted((directory / arm).glob("repetition_*/*/result.json"))} for arm in arms}
    expected = set()
    config_path = directory / "config.json"
    if config_path.is_file():
        config = json.loads(config_path.read_text())
        expected = {f"repetition_{rep:02d}/{design}/result.json" for rep in range(1, config.get("repetitions", 1) + 1)
                    for design in config.get("selected_ids", [])}
    all_keys = sorted(set(keys["guided"]) | set(keys["exploratory"]) | expected)
    summary["paired_runs"] = [{"key": k, "complete": all(k in keys[a] for a in arms),
                               "statuses": {a: keys[a].get(k, {}).get("status") for a in arms}} for k in all_keys]
    (directory / "experiment_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    lines = ["# Guided versus exploratory", "", "Rates use attempted calls; PPA statistics include comparable rejected candidates. Null means no observations.", "",
             "| Arm | Calls | Accepted | Calls/accepted | Fallback rate |", "|---|---:|---:|---:|---:|"]
    for arm, item in summary["arms"].items():
        a = item["aggregate"]
        lines.append(f"| {arm} | {a['calls']} | {a['accepted_candidates']} | {a['calls_per_accepted_candidate']} | {a['fallback_rate']} |")
    lines += ["", "Full per-design rates, medians, best measured deltas, and pair completeness:", "", "```json", json.dumps(summary, indent=2), "```", ""]
    (directory / "experiment_report.md").write_text("\n".join(lines))
    return summary


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("experiment", type=Path)
    args = ap.parse_args()
    if not args.experiment.is_dir():
        ap.error("experiment directory does not exist")
    report(args.experiment)
