#!/usr/bin/env python3
"""Append compact immutable optimizer-iteration records to JSONL history."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Set, Sequence, Tuple


def guided_attempt_record(row):
    """Compact backend truth suitable for audit and LLM retry history."""
    status = row.get("status", "unknown")
    promotion = row.get("promotion") if isinstance(row.get("promotion"), Mapping) else {}
    comparable = bool(promotion.get("comparable"))
    infrastructure = status in {
        "groq_failed", "model_output_truncated", "physical_flow_failed", "synthesis_failed",
        "infrastructure_failed", "formal_timeout", "formal_tool_failure"
    }
    synthesis = row.get("synthesis") if isinstance(row.get("synthesis"), Mapping) else {}
    candidate_hash = synthesis.get("candidate_hash") if isinstance(synthesis.get("candidate_hash"), Mapping) else {}
    exact_duplicate = row.get("exact_source_duplicate") if isinstance(row.get("exact_source_duplicate"), Mapping) else {}
    return {
        "attempt": row.get("attempt"),
        "outcome_type": status,
        "decision": row.get("model_decision", "propose_candidate"),
        "editing_base": row.get("editing_base"),
        "attempted_transformation_family": row.get("attempted_transformation_family"),
        "primary_transformation": row.get("primary_transformation"),
        "proposed_transformations": list(row.get("proposed_transformations") or []),
        "structural_effects": dict(row.get("structural_effects") or {}),
        "family_provenance": row.get("family_provenance"),
        "evidence_fingerprint": row.get("guidance", {}).get("evidence_fingerprint"),
        "source_duplicate": bool(exact_duplicate.get("duplicate")),
        "duplicate_of_attempt": row.get("duplicate_of_attempt") or exact_duplicate.get("duplicate_of_attempt"),
        "synthesis_implementation": row.get("synthesis_implementation_id"),
        "normalized_synthesis_hash": candidate_hash.get("sha256"),
        "synthesis_duplicate": bool(row.get("synthesis_duplicate", False)),
        "formal_result": row.get("formal_classification") or row.get("formal", {}).get("status"),
        "predicted_latency_change_cycles": row.get("predicted_latency_change_cycles"),
        "verified_latency_change_cycles": (row.get("latency_result") or {}).get("iteration_latency_delta_cycles"),
        "verified_total_latency_regression_cycles": (row.get("latency_result") or {}).get("verified_total_latency_regression_cycles"),
        "latency_result": (row.get("latency_result") or {}).get("status"),
        "meaningful_iteration": bool(row.get("meaningful_iteration", False)),
        "meaningful_iteration_number": row.get("meaningful_iteration_number"),
        "ppa_deltas": row.get("ppa_deltas", {}),
        "comparable": comparable,
        "promoted": status == "promoted",
        "rejection_reason": row.get("rejection_reason") or row.get("reason") or promotion.get("reason"),
        "infrastructure_failure": infrastructure,
        "rejected_transformation_family": bool(row.get("attempted_transformation_family")) and
            not infrastructure and (
                status in {"no_op_after_synthesis", "duplicate_after_synthesis", "exact_source_duplicate", "non_executable_change_only"}
                or (comparable and status != "promoted")
            ),
    }


def _compact_ppa_deltas(value: Mapping[str, Any] | None) -> Dict[str, Any]:
    value = value if isinstance(value, Mapping) else {}
    keys = (
        "setup_slack_delta_ns", "hold_slack_delta_ns",
        "area_delta_pct", "power_delta_pct",
    )
    return {key: value.get(key) for key in keys if value.get(key) is not None}


def compact_attempt_for_model(row):
    """Return only previous-attempt facts useful for model search strategy.

    Raw RTL, source hashes, normalized synthesis hashes, logs and backend databases
    are intentionally omitted. Transformation labels remain advisory; verified
    latency/PPA outcomes are backend facts.
    """
    record = guided_attempt_record(row)
    compact = {
        key: record.get(key)
        for key in (
            "attempt", "decision", "editing_base", "primary_transformation",
            "proposed_transformations", "attempted_transformation_family",
            "source_duplicate", "synthesis_implementation", "formal_result",
            "predicted_latency_change_cycles", "verified_latency_change_cycles",
            "verified_total_latency_regression_cycles", "latency_result",
            "meaningful_iteration_number", "promoted", "outcome_type", "rejection_reason",
        )
        if record.get(key) not in (None, {}, [])
    }
    ppa = _compact_ppa_deltas(record.get("ppa_deltas"))
    if ppa:
        compact["ppa_deltas"] = ppa
    effects = record.get("structural_effects")
    if effects:
        compact["structural_effects"] = effects
    return compact


def model_history_context(
    rows: Sequence[Mapping[str, Any]],
    *,
    recent_limit: int = 5,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Return bounded recent meaningful attempts plus a tiny all-history summary."""
    recent_limit = max(1, int(recent_limit))
    meaningful_rows = [row for row in rows if bool(row.get("meaningful_iteration"))]
    recent = [compact_attempt_for_model(row) for row in meaningful_rows[-recent_limit:]]

    explored = set()
    pipeline_attempts = 0
    latency_neutral_attempts = 0
    latency_changing_attempts = 0
    best_neutral_setup = None
    best_latency_setup = None

    for row in meaningful_rows:
        primary = row.get("primary_transformation")
        proposed = list(row.get("proposed_transformations") or [])
        legacy = row.get("optimization_class")
        if isinstance(primary, str) and primary:
            explored.add(primary)
        for item in proposed:
            if isinstance(item, str) and item:
                explored.add(item)
        if not proposed and isinstance(legacy, str) and legacy:
            legacy_map = {"pipelining": "pipeline_insertion", "retiming": "register_retiming"}
            explored.add(legacy_map.get(legacy, legacy))
            proposed = [legacy_map.get(legacy, legacy)]
        if "pipeline_insertion" in proposed or primary == "pipeline_insertion":
            pipeline_attempts += 1

        lr = row.get("latency_result") if isinstance(row.get("latency_result"), Mapping) else {}
        delta = lr.get("iteration_latency_delta_cycles")
        setup = (row.get("ppa_deltas") or {}).get("setup_slack_delta_ns")
        if delta == 0:
            latency_neutral_attempts += 1
            if isinstance(setup, (int, float)):
                best_neutral_setup = setup if best_neutral_setup is None else max(best_neutral_setup, setup)
        elif isinstance(delta, int) and delta > 0:
            latency_changing_attempts += 1
            if isinstance(setup, (int, float)):
                best_latency_setup = setup if best_latency_setup is None else max(best_latency_setup, setup)

    summary: Dict[str, Any] = {
        "model_calls": len(rows),
        "meaningful_attempts": len(meaningful_rows),
        "recent_meaningful_attempts_sent": len(recent),
        "latency_neutral_attempts": latency_neutral_attempts,
        "latency_changing_attempts": latency_changing_attempts,
        "pipeline_insertion_attempts": pipeline_attempts,
        "explored_transformations": sorted(explored),
    }
    if best_neutral_setup is not None:
        summary["best_latency_neutral_setup_gain_ns"] = best_neutral_setup
    if best_latency_setup is not None:
        summary["best_latency_changing_setup_gain_ns"] = best_latency_setup
    return recent, summary


def retry_feedback(row):
    record = guided_attempt_record(row)
    status = row.get("status")
    if status == "duplicate_after_synthesis":
        instruction = (
            "This RTL was source-different but synthesized to an implementation already fully evaluated "
            f"({row.get('synthesis_implementation_id') or 'previous implementation'}; first attempt {row.get('duplicate_of_attempt')}). "
            "Use a materially different structural strategy."
        )
    elif status == "no_op_after_synthesis":
        instruction = "Use a materially different implementation strategy; synthesis produced the same implementation as the Current Best reference."
    elif status == "exact_source_duplicate":
        instruction = "Do not return Golden, Current Best, or any prior candidate unchanged. Produce a real executable RTL change."
    elif status == "non_executable_change_only":
        instruction = "Comments/whitespace/formatting-only changes are not optimizations. Change executable RTL structure."
    elif status == "stop_recommendation_not_supported":
        instruction = "Backend convergence evidence does not yet justify stopping. Propose a materially different unexplored implementation strategy."
    elif status == "model_output_truncated":
        instruction = (
            "The previous response hit the completion-token limit and was incomplete. Return one complete, "
            "concise candidate. Do not expand arithmetic or simple logic into exhaustive case/lookup tables "
            "or other source-code blowups; choose a compact structural RTL implementation."
        )
    elif status == "latency_budget_exceeded":
        lr = row.get("latency_result") if isinstance(row.get("latency_result"), Mapping) else {}
        instruction = (
            "The candidate was temporally equivalent, but backend-verified total latency regression "
            f"({lr.get('verified_total_latency_regression_cycles')}) exceeded the Golden-relative budget "
            f"({lr.get('max_latency_regression_cycles')}). Reduce/remove pipeline depth or choose a same-latency strategy."
        )
    elif status == "not_proven" and (row.get("latency_result") or {}).get("status") == "no_allowed_latency_relation":
        instruction = (
            "No tested fixed-cycle temporal relation to frozen Golden was proven. Check pipeline alignment, reset/warm-up behavior, "
            "and functional logic; do not rely on the predicted latency value."
        )
    elif status in {"formal_timeout", "formal_tool_failure", "latency_contract_unsupported"}:
        instruction = (
            "The backend did not establish a semantic rejection for this transformation. Treat this as inconclusive/tool/contract feedback, "
            "not proof that the transformation family is invalid; prefer a simpler candidate within the supported single-clock fixed-latency contract."
        )
    elif status == "candidate_rejected" and row.get("rejection_reason") == "source_bundle_file_set_changed":
        fileset = row.get("source_bundle_file_set") if isinstance(row.get("source_bundle_file_set"), Mapping) else {}
        unexpected = fileset.get("unexpected") or []
        missing = fileset.get("missing") or []
        detail = []
        if unexpected:
            detail.append("unexpected files: " + ", ".join(str(x) for x in unexpected))
        if missing:
            detail.append("missing files: " + ", ".join(str(x) for x in missing))
        suffix = (" (" + "; ".join(detail) + ")") if detail else ""
        instruction = (
            "This complete-source candidate format does not permit changing the selected editing-base file set"
            + suffix + ". Return exactly the existing source files. If you need a helper module, place the complete "
            "helper module inside an existing returned .v file, or choose a different implementation."
        )
    else:
        instruction = (
            "Use the reported validity/formal/PPA outcome to choose the next materially different equivalent rewrite. "
            "Infrastructure failures do not reject a transformation family."
        )
    return {
        **record,
        "lint": row.get("lint", {}).get("reasons", []),
        "elaboration": row.get("reason"),
        "formal": row.get("formal", {}),
        "latency_result": row.get("latency_result", {}),
        "synthesis_no_op": row.get("synthesis_no_op"),
        "promotion": row.get("promotion", {}),
        "instruction": instruction,
    }


class OptimizerHistoryError(RuntimeError):
    pass


def read_history(path: Path | str, unit_id: Optional[str] = None) -> List[Dict[str, Any]]:
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        return []
    records: List[Dict[str, Any]] = []
    for lineno, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise OptimizerHistoryError(f"invalid JSONL {p}:{lineno}: {exc}") from exc
        if not isinstance(value, dict):
            raise OptimizerHistoryError(f"history record {p}:{lineno} is not an object")
        if unit_id is None or value.get("unit_id") == unit_id:
            records.append(value)
    return records


def _is_infrastructure_failure(record: Mapping[str, Any]) -> bool:
    cls = str(record.get("failure_class") or record.get("rejection_class") or "").lower()
    verdict = str(record.get("verdict") or "").lower()
    reason = str(record.get("reason") or "").lower()
    if verdict in {"error", "tool_error", "infrastructure_error"}:
        return True
    return cls in {"infrastructure", "tool", "provider", "timeout"} or reason.startswith("infrastructure")


def tried_patterns(path: Path | str, unit_id: str) -> Set[str]:
    """Patterns rejected for candidate/formal/PPA reasons, excluding infrastructure failures."""
    out: Set[str] = set()
    for record in read_history(path, unit_id):
        verdict = str(record.get("verdict") or "").lower()
        if verdict not in {"reject", "rejected", "disproven", "not_proven"}:
            continue
        if _is_infrastructure_failure(record):
            continue
        pattern = record.get("chosen_pattern_id")
        if isinstance(pattern, str) and pattern:
            out.add(pattern)
    return out


def _load_json_if_present(path: Optional[str]) -> Dict[str, Any]:
    if not isinstance(path, str) or not path:
        return {}
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        return {}
    try:
        v = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return v if isinstance(v, dict) else {}


def _extract_metrics(transaction: Mapping[str, Any]) -> Dict[str, Any]:
    artifacts = transaction.get("artifacts") or {}
    metrics_obj = _load_json_if_present(artifacts.get("candidate_metrics"))
    metrics = metrics_obj.get("metrics") if isinstance(metrics_obj.get("metrics"), dict) else metrics_obj
    return {
        k: metrics.get(k)
        for k in ("wns_ns", "tns_ns", "area_um2", "power_mw")
        if isinstance(metrics, Mapping) and metrics.get(k) is not None
    }


def _extract_pattern(transaction: Mapping[str, Any]) -> Optional[str]:
    candidate = transaction.get("candidate") or {}
    payload = _load_json_if_present(candidate.get("artifact"))
    value = payload.get("chosen_pattern_id")
    return value if isinstance(value, str) and value else None


def history_record_from_transaction(transaction: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    hist = transaction.get("iteration_history") or {}
    path = hist.get("path")
    iteration = hist.get("iteration_number")
    unit_id = transaction.get("unit_id")
    if not isinstance(path, str) or not path:
        return None
    if not isinstance(iteration, int) or iteration < 1:
        return None

    context = transaction.get("context_budget") or {}
    record: Dict[str, Any] = {
        "schema": "optimizer_history_record/1",
        "iteration": iteration,
        "tag": transaction.get("tag"),
        "unit_id": unit_id,
        "chosen_pattern_id": _extract_pattern(transaction),
        "optimizer_mode": transaction.get("optimizer_mode") or "guided",
        "diagnostic_confidence": transaction.get("diagnostic_confidence"),
        "routing_reason": transaction.get("routing_reason"),
        "source_bundle_sha256": transaction.get("source_bundle_sha256"),
        "verdict": transaction.get("verdict"),
        "reason": transaction.get("note"),
        "failure_class": transaction.get("failure_class"),
        "diagnosis_summary": transaction.get("diagnosis_summary"),
        "closure_iteration": transaction.get("closure_iteration") or iteration,
        "chain_parent_tag": transaction.get("chain_parent_tag"),
        "metrics": _extract_metrics(transaction),
        "context_usage": {
            "estimated_input_tokens": context.get("estimated_input_tokens"),
            "completion_reserve_tokens": context.get("completion_reserve_tokens"),
            "estimated_total_with_completion_reserve": context.get(
                "estimated_total_with_completion_reserve"
            ),
            "cumulative_input_tokens_including_current_estimate": context.get(
                "cumulative_input_tokens_including_current_estimate"
            ),
        },
    }
    return record


def append_history_record_from_transaction(transaction: Mapping[str, Any]) -> bool:
    record = history_record_from_transaction(transaction)
    if record is None:
        return False

    path = Path(transaction["iteration_history"]["path"]).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)

    # Append-only and idempotent by transaction tag.
    tag = record.get("tag")
    if path.is_file() and isinstance(tag, str) and tag:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                old = json.loads(line)
            except Exception:
                continue
            if isinstance(old, dict) and old.get("tag") == tag:
                return False

    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n")
    return True


def run_self_test() -> None:
    import tempfile
    with tempfile.TemporaryDirectory(prefix="nebula_history_") as tmp:
        root = Path(tmp)
        cand = root / "cand.json"
        met = root / "metrics.json"
        hist = root / "history.jsonl"
        cand.write_text(json.dumps({"chosen_pattern_id": "arith.csla_split"}), encoding="utf-8")
        met.write_text(json.dumps({"metrics": {"wns_ns": 0.2, "tns_ns": 0.0}}), encoding="utf-8")
        txn = {
            "tag": "t1", "unit_id": "unit_000", "verdict": "reject", "note": "wns regression",
            "candidate": {"artifact": str(cand)},
            "artifacts": {"candidate_metrics": str(met)},
            "iteration_history": {"path": str(hist), "iteration_number": 1},
            "context_budget": {"estimated_input_tokens": 2500, "completion_reserve_tokens": 2048,
                               "estimated_total_with_completion_reserve": 4548,
                               "cumulative_input_tokens_including_current_estimate": 2500},
        }
        assert append_history_record_from_transaction(txn) is True
        assert append_history_record_from_transaction(txn) is False
        row = json.loads(hist.read_text().strip())
        assert row["iteration"] == 1
        assert row["chosen_pattern_id"] == "arith.csla_split"
        assert row["metrics"]["wns_ns"] == 0.2
        assert row["context_usage"]["estimated_input_tokens"] == 2500
        assert read_history(hist, "unit_000")[0]["tag"] == "t1"
        assert tried_patterns(hist, "unit_000") == {"arith.csla_split"}
        with hist.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"unit_id":"unit_000","verdict":"error","failure_class":"infrastructure","chosen_pattern_id":"mux.hoist_before_arith"}) + "\n")
        assert tried_patterns(hist, "unit_000") == {"arith.csla_split"}
        print("PASS: optimizer_history self-test")
        print("PASS: compact append-only iteration record emitted")
        print("PASS: duplicate transaction tag is not appended twice")


if __name__ == "__main__":
    run_self_test()
