#!/usr/bin/env python3
"""Aggregate nEBULA per-attempt evidence into canonical JSON and CSV tables."""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any, Mapping

SCHEMA = "nebula_aggregate_results/1"

FIELDS = [
    "design", "attempt_number", "prompt_tokens", "completion_tokens", "total_tokens",
    "path_groups_considered", "optimization_mode", "candidate_source_hash",
    "normalized_synthesis_hash", "lint_result", "formal_equivalence_result",
    "latency_result", "ppa_result", "promotion_rejection_result", "rejection_reason",
    "primary_transformation", "proposed_transformations", "verified_latency_delta_cycles",
    "current_best_epoch", "model_call_number", "meaningful_iteration_number",
    "sta_netlist_compat_applied", "physical_failure_reason",
]


def nested(mapping: Mapping[str, Any] | None, *keys: str) -> Any:
    cur: Any = mapping or {}
    for key in keys:
        if not isinstance(cur, Mapping):
            return None
        cur = cur.get(key)
    return cur


def _sta_compat_applied(attempt: Mapping[str, Any]) -> bool:
    for section_name in ("synthesis", "ppa", "repeat_ppa"):
        section = attempt.get(section_name)
        if not isinstance(section, Mapping):
            continue
        for side in ("baseline", "candidate", "reference"):
            run = section.get(side)
            if not isinstance(run, Mapping):
                continue
            compat = run.get("sta_netlist_compat")
            if isinstance(compat, Mapping) and compat.get("applied") is True:
                return True
    return False


def _physical_failure_reason(attempt: Mapping[str, Any]) -> Any:
    for section_name in ("ppa", "synthesis", "repeat_ppa"):
        section = attempt.get(section_name)
        if not isinstance(section, Mapping):
            continue
        for side in ("candidate", "reference", "baseline"):
            run = section.get(side)
            if isinstance(run, Mapping) and run.get("failure_reason"):
                return run.get("failure_reason")
    return None


def row_for(design: str, attempt: Mapping[str, Any]) -> dict[str, Any]:
    usage = attempt.get("groq_usage") if isinstance(attempt.get("groq_usage"), Mapping) else {}
    guidance = attempt.get("guidance") if isinstance(attempt.get("guidance"), Mapping) else {}
    synthesis = attempt.get("synthesis") if isinstance(attempt.get("synthesis"), Mapping) else {}
    candidate_hash = attempt.get("candidate_bundle_sha256") or attempt.get("candidate_response_sha256")
    formal = attempt.get("formal") if isinstance(attempt.get("formal"), Mapping) else {}
    latency = attempt.get("latency_result") if isinstance(attempt.get("latency_result"), Mapping) else {}
    promotion = attempt.get("promotion") if isinstance(attempt.get("promotion"), Mapping) else {}
    ppa = attempt.get("ppa") if isinstance(attempt.get("ppa"), Mapping) else {}
    transforms = attempt.get("proposed_transformations") or []
    return {
        "design": design,
        "attempt_number": attempt.get("attempt_number") or attempt.get("attempt"),
        "prompt_tokens": usage.get("prompt_tokens") or usage.get("input_tokens"),
        "completion_tokens": usage.get("completion_tokens") or usage.get("output_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "path_groups_considered": guidance.get("path_groups_sent_to_llm") or guidance.get("path_groups_considered"),
        "optimization_mode": attempt.get("optimization_mode") or guidance.get("mode_used"),
        "candidate_source_hash": candidate_hash,
        "normalized_synthesis_hash": nested(synthesis, "candidate_hash", "sha256"),
        "lint_result": nested(attempt, "lint", "ok") if isinstance(attempt.get("lint"), Mapping) else attempt.get("lint_result"),
        "formal_equivalence_result": formal.get("status") or attempt.get("formal_classification"),
        "latency_result": latency.get("status") or ("passed" if latency.get("passed") else None),
        "ppa_result": "measured" if ppa else None,
        "promotion_rejection_result": "promoted" if attempt.get("status") == "promoted" else attempt.get("status"),
        "rejection_reason": attempt.get("rejection_reason") or attempt.get("reason") or promotion.get("reason"),
        "primary_transformation": attempt.get("primary_transformation"),
        "proposed_transformations": ";".join(str(x) for x in transforms) if isinstance(transforms, list) else transforms,
        "verified_latency_delta_cycles": latency.get("verified_total_latency_regression_cycles") or latency.get("verified_shift_cycles"),
        "current_best_epoch": attempt.get("current_best_epoch"),
        "model_call_number": attempt.get("model_call_number"),
        "meaningful_iteration_number": attempt.get("meaningful_iteration_number"),
        "sta_netlist_compat_applied": _sta_compat_applied(attempt),
        "physical_failure_reason": _physical_failure_reason(attempt),
    }


def collect(root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen = set()
    for path in sorted(root.rglob("result.json")):
        try:
            data = json.loads(path.read_text())
        except Exception:
            continue
        if not isinstance(data, Mapping) or not isinstance(data.get("attempts"), list):
            continue
        design = str(data.get("id") or path.parent.name)
        for attempt in data["attempts"]:
            if not isinstance(attempt, Mapping):
                continue
            key = (str(path.resolve()), attempt.get("attempt_number") or attempt.get("attempt"))
            if key in seen:
                continue
            seen.add(key)
            rows.append(row_for(design, attempt))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--output-prefix", type=Path, required=True)
    args = ap.parse_args()
    rows = collect(args.root.resolve())
    prefix = args.output_prefix.resolve()
    prefix.parent.mkdir(parents=True, exist_ok=True)
    json_path = prefix.with_suffix(".json")
    csv_path = prefix.with_suffix(".csv")
    json_path.write_text(json.dumps({"schema": SCHEMA, "attempts": rows}, indent=2, sort_keys=True) + "\n")
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k) for k in FIELDS})
    print(f"JSON: {json_path}")
    print(f"CSV:  {csv_path}")
    print(f"Attempts: {len(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
