#!/usr/bin/env python3
"""Run Groq-generated RTL optimization with immutable Golden and promoted Current Best.

Core architecture:
  * GOLDEN is immutable and is always the formal correctness reference.
  * CURRENT BEST starts as Golden and changes only after repeat-confirmed promotion.
  * The model chooses one editing base (golden or current_best) for each candidate.
  * Exact/source-only duplicates are rejected before formal.
  * Normalized synthesis equivalence remains backend-authoritative and skips PPA.
  * PPA always compares the candidate against the unchanged Current Best snapshot.
  * A model no_further_optimization response is advisory; backend convergence owns stop.

Latency-changing/pipelining verification is policy-controlled. Budget zero preserves
the validated cycle-exact path; positive budgets enable backend-proven fixed-cycle
temporal equivalence against frozen Golden before synthesis/PPA.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import shutil
import subprocess
import tempfile
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

import calibrate_pairs_v3 as cal
from build_optimizer_input import normalize_transformation_family
from design_manifest import DesignManifest, VerificationPolicy
from design_capabilities import analyze_design_capabilities
from latency_contract import build_latency_contract, latency_policy_for_prompt
from latency_verifier import cycle_exact_result, verify_fixed_latency
from evaluate_candidate import assess_guided_candidate
from extract_candidate import (
    extract_json_response, extract_optimizer_response, StopRecommendation,
    CandidateSourceBundleFileSetError, CandidateExtractionError,
)
from guidance_packet import packet_from_run
from lint_candidate import lint_candidate
from optimizer_history import model_history_context, guided_attempt_record, retry_feedback
from prompt_renderer import render_groq_prompt
from run_optimizer import call_groq
from source_bundle import SourceBundle
from yosys_structure import normalized_synthesis_hash, quote_yosys_token


ROOT = cal.ROOT
SCHEMA = ROOT / "timing_automation/response_schema_latency.json"
TIMING_GRID_NS = (0.60, 0.80, 1.00, 1.25, 1.50, 2.00, 2.50, 3.00)


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def mode_for(pair: Mapping[str, Any]) -> str:
    return "sequential_equivalence" if pair["baseline"]["edge_signals"] else "combinational_cec"


def make_manifest(pair: Mapping[str, Any], directory: Path, *, formal_timeout_s: int = 120) -> DesignManifest:
    source = Path(pair["baseline"]["source"])
    name = source.name
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / name
    if source.resolve() != destination.resolve():
        shutil.copy2(source, destination)
    manifest = DesignManifest(
        dataset="RTL-OPT",
        design_id=f"rtl_opt_groq_{pair['id']}",
        top=pair["baseline"]["top"],
        source_files=[name],
        language="verilog",
        verification=VerificationPolicy(
            backend="yosys", mode=mode_for(pair), options={"timeout_s": int(formal_timeout_s)}
        ),
        source_hashes={name: sha256(destination)},
        metadata={"rtl_opt_pair_id": pair["id"], "candidate_source": "groq"},
    )
    manifest.validate(require_source_hashes=True)
    manifest.write_json(directory / "golden_manifest.json")
    return manifest


def clone_bundle_snapshot(
    manifest: DesignManifest,
    base_dir: Path,
    destination: Path,
    *,
    role: str,
    epoch: int,
    metrics: Mapping[str, Any] | None,
    promoted_from_attempt: int | None,
    verified_total_latency_regression_cycles: int = 0,
) -> dict[str, Any]:
    """Copy one immutable SourceBundle snapshot and return its descriptor."""
    bundle = SourceBundle.from_manifest(manifest, base_dir, verify_manifest_hashes=True)
    destination.mkdir(parents=True, exist_ok=True)
    for source_name in manifest.source_files:
        target = destination / source_name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(bundle.get_source(source_name).content)

    data = copy.deepcopy(manifest.to_dict())
    data["source_hashes"] = {
        source_name: sha256(destination / source_name)
        for source_name in manifest.source_files
    }
    metadata = dict(data.get("metadata") or {})
    metadata.update({
        "snapshot_role": role,
        "current_best_epoch": epoch,
        "promoted_from_attempt": promoted_from_attempt,
    })
    data["metadata"] = metadata
    snapshot_manifest = DesignManifest.from_dict(data)
    manifest_path = destination / "design_manifest.json"
    snapshot_manifest.write_json(manifest_path)
    snapshot_bundle = SourceBundle.from_manifest(snapshot_manifest, destination, verify_manifest_hashes=True)
    identity_record = snapshot_bundle.identity_record(include_text=False)
    identity = f"{role}.epoch_{epoch:03d}.{identity_record['bundle_sha256'][:16]}"
    snapshot = {
        "schema": "nebula_design_snapshot/1",
        "role": role,
        "epoch": epoch,
        "identity": identity,
        "base_dir": str(destination.resolve()),
        "manifest": str(manifest_path.resolve()),
        "bundle_sha256": identity_record["bundle_sha256"],
        "executable_identity_sha256": identity_record["executable_identity_sha256"],
        "metrics": dict(metrics or {}),
        "promoted_from_attempt": promoted_from_attempt,
        "verified_total_latency_regression_cycles": int(verified_total_latency_regression_cycles),
    }
    write_json(destination / "snapshot.json", snapshot)
    return snapshot


def snapshot_manifest(snapshot: Mapping[str, Any]) -> DesignManifest:
    return DesignManifest.read_json(Path(str(snapshot["manifest"])))


def snapshot_bundle(snapshot: Mapping[str, Any]) -> SourceBundle:
    manifest = snapshot_manifest(snapshot)
    return SourceBundle.from_manifest(manifest, Path(str(snapshot["base_dir"])), verify_manifest_hashes=True)


def snapshot_primary_source(snapshot: Mapping[str, Any]) -> Path:
    manifest = snapshot_manifest(snapshot)
    return manifest.resolve_path(manifest.source_files[0], Path(str(snapshot["base_dir"])))


def candidate_bundle_sha256(manifest: DesignManifest, complete_sources: Mapping[str, bytes]) -> tuple[str, dict[str, str]]:
    """Compute the exact complete-SourceBundle identity before lint/Yosys.

    This is deliberately byte-exact.  Formatting/comment-only variants are not
    classified here; normalized synthesis remains the authority for source-different
    implementations that collapse to the same hardware.
    """
    source_hashes = {
        name: hashlib.sha256(complete_sources[name]).hexdigest()
        for name in manifest.source_files
    }
    payload = {
        "top": manifest.top,
        "language": manifest.language,
        "source_files": list(manifest.source_files),
        "source_hashes": source_hashes,
        "defines": list(manifest.defines),
        "parameters": dict(manifest.parameters),
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return digest, source_hashes


def prompt_snapshot(snapshot: Mapping[str, Any], *, include_text: bool, same_as_golden: bool = False) -> dict[str, Any]:
    bundle = snapshot_bundle(snapshot)
    source_bundle = bundle.identity_record(include_text=include_text)
    # Backend-only fingerprint aids are intentionally not exposed to the model.
    source_bundle.pop("executable_identity_sha256", None)
    source_bundle.pop("legacy_source_only_sha256", None)
    return {
        "role": snapshot["role"],
        "epoch": snapshot["epoch"],
        "design_identity": snapshot["identity"],
        "same_as_golden": bool(same_as_golden),
        "metrics": dict(snapshot.get("metrics") or {}),
        "source_bundle": source_bundle,
    }


def structural_effects_from_lint(lint: Mapping[str, Any]) -> dict[str, Any]:
    """Summarize backend-observed structural effects without trusting model intent."""
    checks = lint.get("checks") if isinstance(lint.get("checks"), Mapping) else {}
    diffs = lint.get("structure_differences") if isinstance(lint.get("structure_differences"), list) else []
    categories = {d.get("category") for d in diffs if isinstance(d, Mapping)}

    def state_count(value: Any) -> int | None:
        if not isinstance(value, Mapping):
            return None
        total = 0
        seen = False
        for cells in value.values():
            if isinstance(cells, list):
                total += len(cells)
                seen = True
        return total if seen else None

    golden_count = candidate_count = None
    for diff in diffs:
        if not isinstance(diff, Mapping) or diff.get("category") != "elaborated_state_boundary_changed":
            continue
        golden_count = state_count(diff.get("golden"))
        candidate_count = state_count(diff.get("candidate"))
        break
    delta = None
    if golden_count is not None and candidate_count is not None:
        delta = candidate_count - golden_count

    effects: dict[str, Any] = {
        "sequential_state_changed": bool(categories & {"state_boundary_changed", "elaborated_state_boundary_changed"}),
        "new_clock_domain_detected": checks.get("no_new_clock_domains") is False,
        "latch_introduced": checks.get("no_new_latches") is False,
        "clock_reset_behavior_changed": checks.get("clock_reset_behavior_unchanged") is False,
        "top_interface_changed": checks.get("top_interface_unchanged") is False,
    }
    if delta is not None:
        effects["sequential_state_cell_delta"] = delta
    return effects


def make_request(
    golden_snapshot: Mapping[str, Any],
    current_best_snapshot: Mapping[str, Any],
    current_best_evidence: Mapping[str, Any],
    guidance: Mapping[str, Any] | None,
    history: list[Mapping[str, Any]],
    search_summary: Mapping[str, Any],
    budgets: Mapping[str, Any],
    latency_policy: Mapping[str, Any],
    design_capabilities: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    golden_bundle = snapshot_bundle(golden_snapshot)
    current_bundle = snapshot_bundle(current_best_snapshot)
    same = golden_bundle.bundle_sha256() == current_bundle.bundle_sha256()
    primary = current_bundle.source_names()[0]
    return {
        "schema": "optimizer_request/2",
        "request_semantics": "golden_current_best_latency_aware/2",
        "response_schema_version": "3.3",
        "unit_id": "whole_design",
        "optimizer_mode": (guidance or {}).get("mode_used", "exploratory"),
        "guidance": guidance,
        "user_ppa_budgets": dict(budgets),
        "latency_policy": dict(latency_policy),
        "design_capabilities": dict(design_capabilities or {}),
        "golden": prompt_snapshot(golden_snapshot, include_text=True),
        "current_best": prompt_snapshot(current_best_snapshot, include_text=not same, same_as_golden=same),
        "previous_attempts": [dict(item) for item in history],
        "search_summary": dict(search_summary),
        "editing_base_policy": {
            "allowed": ["golden", "current_best"],
            "selection_owner": "LLM",
            "mixed_bases_allowed": False,
            "formal_reference": "golden",
            "ppa_comparison_reference": "current_best",
        },
        # Compatibility binding for deterministic lint; replaced with the selected
        # base immediately before materialization.
        "source_bundle": current_bundle.identity_record(include_text=False),
        "primary_diagnostic_focus": {"file": primary, "module": current_bundle.manifest.top},
        "recommended_focus_ranges": [],
        "recommended_pattern_ids": [],
        "candidate_contract": {
            "complete_verilog_required": True,
            "one_editing_base_required": True,
            "same_top_level_interface_required": True,
            "formal_equivalence_against_golden_required": True,
            "ppa_against_current_best_required": True,
            "latency_policy_controlled": True,
            "backend_verified_latency_required": True,
        },
        "current_best_evidence": dict(current_best_evidence),
    }


def lint_request_for_selected_base(request: Mapping[str, Any], selected_snapshot: Mapping[str, Any]) -> dict[str, Any]:
    selected_bundle = snapshot_bundle(selected_snapshot)
    materialization_request = copy.deepcopy(dict(request))
    materialization_request["source_bundle"] = selected_bundle.identity_record(include_text=False)
    materialization_request["primary_diagnostic_focus"] = {
        "file": selected_bundle.source_names()[0],
        "module": selected_bundle.manifest.top,
    }
    return materialization_request


def prompts(request: Mapping[str, Any], feedback: Mapping[str, Any] | None = None) -> tuple[str, str]:
    return render_groq_prompt(request, json.loads(SCHEMA.read_text()), feedback)[:2]


def timing_excerpt(metrics_path: str) -> list[str]:
    report = Path(metrics_path).parent
    report = ROOT / "reports" / report.relative_to(ROOT / "logs") / "5_global_route.rpt"
    if not report.is_file():
        return []
    lines = report.read_text(encoding="utf-8", errors="replace").splitlines()
    start = next((i for i, line in enumerate(lines) if "report_checks -path_delay max" in line), None)
    return lines[start:start + 48] if start is not None else []


def select_baseline(pair: dict[str, Any], directory: Path, *, physical_timeout_s: int = 900):
    pair = copy.deepcopy(pair)
    pair["reference"] = copy.deepcopy(pair["baseline"])
    formal_dir = directory / "baseline_elaboration"
    formal_dir.mkdir(parents=True, exist_ok=True)
    module = cal.v2.elaborate(pair, formal_dir)
    if module is None:
        return None, {"error": pair.get("qualification_reason", "golden elaboration failed")}
    sweep = []
    for period in TIMING_GRID_NS:
        recipe, _unused, _profile = cal.freeze(pair, directory / f"baseline_{period:g}", module, period, "baseline_selection")
        baseline = cal.physical(pair, directory / f"baseline_{period:g}", "baseline", recipe,
                                Path(pair["baseline"]["source"]), "baseline_selection", timeout_s=physical_timeout_s)
        sweep.append({"period_ns": period, "run": baseline})
        if cal.timing_clean(baseline):
            evidence = {
                "selected_period_ns": period,
                "baseline_metrics": baseline["metrics"],
                "worst_setup_path_excerpt": timing_excerpt(baseline["metrics_path"]),
                "timing_grid_policy": "first golden-baseline setup/hold-clean period",
            }
            return {"module": module, "period_ns": period, "sweep": sweep, "run": baseline}, evidence
    failure_reasons = {}
    for item in sweep:
        run = item.get("run") if isinstance(item, Mapping) else {}
        if isinstance(run, Mapping) and run.get("status") != "passed":
            reason = str(run.get("failure_reason") or "physical_stage_failed")
            failure_reasons[reason] = failure_reasons.get(reason, 0) + 1
    return None, {
        "timing_grid_ns": list(TIMING_GRID_NS),
        "sweep": sweep,
        "error": "golden_baseline_physical_flow_failed" if all((item.get("run") or {}).get("status") != "passed" for item in sweep) else "no golden-baseline setup/hold-clean period",
        "failure_reasons": failure_reasons,
    }


def user_budgets(args):
    return {key: getattr(args, key) for key in (
        "max_area_regression_pct", "max_power_regression_pct",
        "max_setup_slack_regression_ns", "max_hold_slack_regression_ns")}


def synthesis_hash(run, directory, side):
    """Hash the actual ORFS post-synthesis netlist, never the elaboration view."""
    try:
        if run.get("status") != "passed":
            raise ValueError("synthesis did not pass")
        # Implementation identity is derived from the raw Yosys synthesis
        # artifact.  Any STA-compatibility copy is a parser workaround only and
        # must never affect normalized-synthesis dedupe semantics.
        netlist = Path(run.get("synthesis_netlist_raw") or run["synthesis_netlist"])
        if not netlist.is_file():
            raise FileNotFoundError(f"raw synthesis netlist not found: {netlist}")
        target = directory / f"{side}.synthesis.json"
        script = directory / f"{side}.synthesis_hash.ys"
        script.write_text(
            f"read_verilog {quote_yosys_token(str(netlist))}\n"
            f"write_json {quote_yosys_token(str(target))}\n"
        )
        converted = cal.v2.run([cal.v2.YOSYS, "-s", script], directory / f"{side}.synthesis_hash.log", 120)
        if converted["exit_code"] != 0:
            raise ValueError("post-synthesis JSON conversion failed")
        artifact = normalized_synthesis_hash(json.loads(target.read_text()))
        artifact.update(status="passed", netlist=str(netlist), netlist_sha256=sha256(netlist))
    except Exception as exc:
        artifact = {"status": "unavailable", "reason": f"{type(exc).__name__}: {exc}"}
    write_json(directory / f"{side}.synthesis_hash.json", artifact)
    return artifact


def synthesis_hash_key(artifact: Mapping[str, Any]) -> str | None:
    if artifact.get("status") != "passed":
        return None
    digest = artifact.get("sha256")
    normalization = artifact.get("normalization")
    if not isinstance(digest, str) or not digest or not isinstance(normalization, str) or not normalization:
        return None
    return f"{normalization}:{digest}"


def synthesis_no_op(baseline_hash, candidate_hash):
    return (baseline_hash.get("status") == candidate_hash.get("status") == "passed"
            and bool(baseline_hash.get("sha256"))
            and baseline_hash.get("normalization") == candidate_hash.get("normalization")
            and baseline_hash["sha256"] == candidate_hash.get("sha256"))


def diagnose(run, source, top, directory, args, history=()):
    try:
        packet = packet_from_run(run, source, top, directory, ROOT, mode=args.optimizer_mode,
                                 history=history, max_groups=args.max_guidance_groups,
                                 max_excerpts=args.max_excerpts_per_group)
        if packet.get("schema") != "guidance_packet/1" or packet.get("mode_used") not in ("guided", "guided_evidence_only", "exploratory"):
            raise ValueError("malformed guidance packet")
        if any(not isinstance(packet.get(key), list) for key in ("groups", "recommendations", "prior_attempt_outcomes")):
            raise ValueError("malformed guidance lists")
        json.dumps(packet, allow_nan=False)
        return packet
    except Exception as exc:
        from guidance_packet import build_packet
        packet = build_packet([], None, None, {}, mode=args.optimizer_mode, history=history)
        packet["guidance_error"] = f"{type(exc).__name__}: {exc}"
        return packet


def _candidate_rejection_reason(row: Mapping[str, Any]) -> str | None:
    if row.get("status") == "promoted":
        return None
    explicit = row.get("rejection_reason") or row.get("reason")
    if explicit:
        return str(explicit)
    status = row.get("status")
    mapping = {
        "exact_source_duplicate": "exact_source_duplicate",
        "non_executable_change_only": "non_executable_change_only",
        "no_op_after_synthesis": "normalized_synthesis_no_op_current_best",
        "duplicate_after_synthesis": "duplicate_normalized_synthesis_hash",
        "lint_failed": "lint_failed",
        "not_proven": "formal_not_proven",
        "formal_timeout": "formal_timeout",
        "formal_tool_failure": "formal_tool_failure",
        "latency_contract_unsupported": "latency_contract_unsupported",
        "latency_budget_exceeded": "latency_budget_exceeded",
        "candidate_rejected": "invalid_candidate_response",
        "model_stop_accepted": "backend_convergence_confirmed_model_stop",
        "stop_recommendation_not_supported": "backend_convergence_not_met",
    }
    if status in mapping:
        return mapping[status]
    promotion = row.get("promotion") if isinstance(row.get("promotion"), Mapping) else {}
    return promotion.get("reason")


def compact_attempt_measurements(row: Mapping[str, Any], design: str) -> dict[str, Any]:
    synthesis = row.get("synthesis") if isinstance(row.get("synthesis"), Mapping) else {}
    candidate_hash = synthesis.get("candidate_hash") if isinstance(synthesis.get("candidate_hash"), Mapping) else {}
    promotion = row.get("promotion") if isinstance(row.get("promotion"), Mapping) else {}
    ppa = row.get("ppa") if isinstance(row.get("ppa"), Mapping) else {}
    usage = row.get("groq_usage") if isinstance(row.get("groq_usage"), Mapping) else {}
    return {
        "design": design,
        "attempt_number": row.get("attempt"),
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "path_groups_considered": row.get("guidance", {}).get("path_groups_considered", 0),
        "optimization_mode": row.get("guidance", {}).get("mode_used") or row.get("optimization_mode"),
        "candidate_response_hash": row.get("candidate_response_sha256"),
        "candidate_source_hash": row.get("candidate_source_sha256"),
        "normalized_synthesis_hash": candidate_hash.get("sha256"),
        "editing_base": row.get("editing_base"),
        "current_best_epoch": row.get("current_best_epoch"),
        "lint_result": {
            "ok": row.get("lint", {}).get("ok") if isinstance(row.get("lint"), Mapping) else None,
            "reasons": row.get("lint", {}).get("reasons", []) if isinstance(row.get("lint"), Mapping) else [],
        },
        "formal_equivalence_result": {
            "status": row.get("formal", {}).get("status") if isinstance(row.get("formal"), Mapping) else None,
            "reason": row.get("formal", {}).get("reason") if isinstance(row.get("formal"), Mapping) else None,
        },
        "latency_result": dict(row.get("latency_result") or {}),
        "ppa_result": {
            "available": bool(ppa),
            "comparable": promotion.get("comparable") if promotion else None,
            "deltas": row.get("ppa_deltas", {}),
            "comparison_reference": row.get("ppa_comparison_reference"),
            "duplicate_of_ppa_deltas": row.get("duplicate_of_ppa_deltas", {}),
        },
        "promotion_rejection_result": {
            "status": row.get("status"),
            "promoted": row.get("status") == "promoted",
            "keep": promotion.get("keep") if promotion else None,
            "synthesis_duplicate": bool(row.get("synthesis_duplicate", False)),
            "duplicate_of_attempt": row.get("duplicate_of_attempt"),
        },
        "rejection_reason": _candidate_rejection_reason(row),
    }


def _remember_rejected_synthesis_hash(row, global_seen, epoch_seen):
    promotion = row.get("promotion") if isinstance(row.get("promotion"), Mapping) else {}
    synthesis = row.get("synthesis") if isinstance(row.get("synthesis"), Mapping) else {}
    candidate_hash = synthesis.get("candidate_hash") if isinstance(synthesis.get("candidate_hash"), Mapping) else {}
    # Only a final, comparable policy rejection is safe convergence evidence.
    # Tool/provider/physical failures and repeat-confirmation infrastructure failures
    # must never poison the rejected-implementation set.
    if row.get("status") != "analyzed" or not promotion.get("comparable") or promotion.get("keep"):
        return False
    key = synthesis_hash_key(candidate_hash)
    if key is None:
        return False
    record = {
        "attempt": row.get("attempt"),
        "current_best_epoch": row.get("current_best_epoch"),
        "status": row.get("status"),
        "normalized_synthesis_hash": candidate_hash.get("sha256"),
        "normalization": candidate_hash.get("normalization"),
        "ppa_deltas": dict(row.get("ppa_deltas", {})),
        "promotion_reason": promotion.get("reason"),
        "attempted_transformation_family": row.get("attempted_transformation_family"),
        "synthesis_implementation_id": row.get("synthesis_implementation_id"),
    }
    global_seen.setdefault(key, record)
    epoch_seen.setdefault(key, record)
    return True


def convergence_state(attempts, epoch_seen, *, epoch, min_unique_rejected_syntheses, duplicate_limit):
    if duplicate_limit <= 0 or len(epoch_seen) < min_unique_rejected_syntheses:
        return None
    epoch_attempts = [a for a in attempts if a.get("current_best_epoch") == epoch]
    duplicates = [a for a in epoch_attempts if a.get("synthesis_duplicate") is True]
    if len(duplicates) < duplicate_limit or not epoch_attempts or epoch_attempts[-1].get("synthesis_duplicate") is not True:
        return None
    return {
        "status": "converged",
        "reason": "repeated_previously_rejected_synthesis_implementations",
        "current_best_epoch": epoch,
        "trigger_attempt": epoch_attempts[-1].get("attempt"),
        "unique_rejected_synthesis_hashes": len(epoch_seen),
        "duplicate_hits": len(duplicates),
        "min_unique_rejected_syntheses": min_unique_rejected_syntheses,
        "duplicate_limit": duplicate_limit,
        "duplicate_attempts": [a.get("attempt") for a in duplicates],
    }


def model_stop_convergence_evidence(attempts, epoch_seen, *, epoch, min_unique_rejected_syntheses, duplicate_limit):
    """Backend evidence for accepting an advisory model stop recommendation.

    The model is allowed to recommend stopping slightly before the automatic
    duplicate threshold, but only after the backend has measured at least the
    configured number of distinct rejected synthesized implementations and has
    observed at least one synthesis duplicate in the same Current-Best epoch.
    """
    if len(epoch_seen) < min_unique_rejected_syntheses:
        return None
    epoch_attempts = [a for a in attempts if a.get("current_best_epoch") == epoch]
    duplicates = [a for a in epoch_attempts if a.get("synthesis_duplicate") is True]
    if len(duplicates) < 1:
        return None
    return {
        "status": "converged",
        "reason": "backend_evidence_supports_model_stop_recommendation",
        "current_best_epoch": epoch,
        "unique_rejected_synthesis_hashes": len(epoch_seen),
        "duplicate_hits": len(duplicates),
        "min_unique_rejected_syntheses": min_unique_rejected_syntheses,
        "automatic_duplicate_limit": duplicate_limit,
        "model_stop_min_duplicate_hits": 1,
        "duplicate_attempts": [a.get("attempt") for a in duplicates],
    }


def _write_state_indexes(directory, candidate_index, synthesis_index, current_best):
    state = directory / "state"
    write_json(state / "candidate_identity_index.json", {"schema": "candidate_identity_index/1", "entries": candidate_index})
    write_json(state / "synthesis_implementation_index.json", {"schema": "synthesis_implementation_index/1", "entries": synthesis_index})
    write_json(state / "current_best_snapshot.json", dict(current_best))


def _write_run_checkpoint(directory: Path, checkpoint: Path, result: Mapping[str, Any]) -> None:
    """Atomically persist the result plus a compact restart/forensics marker.

    V1.4 exposes design-granular resume through rtl-opt-batch.  This marker makes
    partial single-design runs unambiguous and prevents a half-written attempt
    from being mistaken for completion.
    """
    write_json(checkpoint, result)
    attempts = result.get("attempts") if isinstance(result.get("attempts"), list) else []
    latest = attempts[-1] if attempts and isinstance(attempts[-1], Mapping) else {}
    state = {
        "schema": "rtl_opt_run_state/1",
        "design": result.get("id"),
        "status": result.get("status") or (latest.get("status") if latest else "running"),
        "model_call_count": result.get("model_call_count", 0),
        "meaningful_iteration_count": result.get("meaningful_iteration_count", 0),
        "last_attempt": latest.get("attempt_number") or latest.get("attempt"),
        "last_attempt_status": latest.get("status"),
        "current_best_epoch": latest.get("current_best_epoch", 0) if latest else 0,
        "checkpoint": str(checkpoint),
        "resume_scope": "design_granular_via_rtl_opt_batch",
    }
    write_json(directory / "state" / "run_state.json", state)


def run_one(pair: dict[str, Any], output: Path, args: argparse.Namespace, baseline_data=None) -> dict[str, Any]:
    pair = copy.deepcopy(pair)
    directory = output / pair["id"]
    directory.mkdir(parents=True, exist_ok=True)
    working_manifest = make_manifest(pair, directory, formal_timeout_s=args.formal_timeout_s)
    selected, golden_evidence = baseline_data if baseline_data is not None else select_baseline(pair, directory, physical_timeout_s=args.ppa_timeout_s)
    result = {
        "schema": "groq_result/5",
        "id": pair["id"],
        "arm": args.optimizer_mode,
        "groq": {"model": args.model},
        "user_ppa_budgets": user_budgets(args),
        "baseline_evidence": golden_evidence,
        "attempts": [],
        "model_call_count": 0,
        "meaningful_iteration_count": 0,
    }
    checkpoint = directory / "result.json"
    if selected is None:
        baseline_error = str((golden_evidence or {}).get("error") or "")
        result.update(
            status=("baseline_physical_flow_failed" if baseline_error == "golden_baseline_physical_flow_failed" else "no_baseline_feasible_period"),
            rejection_reason=baseline_error or "baseline_unavailable",
        )
        _write_run_checkpoint(directory, checkpoint, result)
        return result

    latency_contract = build_latency_contract(
        pair=pair, manifest=working_manifest, base_dir=directory,
        elaborated_module=selected["module"],
        max_latency_regression_cycles=args.max_latency_regression_cycles,
    )
    result["latency_contract"] = latency_contract.to_dict()
    design_capabilities = analyze_design_capabilities(
        pair=pair, manifest=working_manifest, base_dir=directory,
        elaborated_module=selected["module"], latency_contract=latency_contract.to_dict(),
    )
    result["design_capabilities"] = design_capabilities
    write_json(directory / "design_capabilities.json", design_capabilities)

    state_root = directory / "state"
    golden_snapshot = clone_bundle_snapshot(
        working_manifest, directory, state_root / "golden", role="golden", epoch=0,
        metrics=selected["run"].get("metrics", {}), promoted_from_attempt=None,
        verified_total_latency_regression_cycles=0,
    )
    golden_manifest = snapshot_manifest(golden_snapshot)
    current_best = clone_bundle_snapshot(
        golden_manifest, Path(golden_snapshot["base_dir"]), state_root / "current_best" / "epoch_000",
        role="current_best", epoch=0, metrics=selected["run"].get("metrics", {}), promoted_from_attempt=None,
        verified_total_latency_regression_cycles=0,
    )
    write_json(state_root / "golden_snapshot.json", golden_snapshot)
    (state_root / "current_best_history.jsonl").write_text(json.dumps(current_best, sort_keys=True) + "\n", encoding="utf-8")

    candidate_index: dict[str, dict[str, Any]] = {}
    synthesis_index: dict[str, dict[str, Any]] = {}
    rejected_synthesis_global: dict[str, dict[str, Any]] = {}
    rejected_synthesis_epoch: dict[str, dict[str, Any]] = {}

    current_best_run = selected["run"]
    current_best_evidence = {
        "selected_period_ns": selected["period_ns"],
        "metrics": current_best_run.get("metrics", {}),
        "source_role": "current_best",
        "current_best_epoch": 0,
        "verified_total_latency_regression_cycles": 0,
    }
    current_best_source = snapshot_primary_source(current_best)
    guidance = diagnose(current_best_run, current_best_source, golden_manifest.top, directory / "baseline_guidance", args)
    guidance.setdefault("evidence", {})["source_role"] = "current_best"
    guidance["evidence"]["current_best_epoch"] = 0
    write_json(directory / "baseline_guidance.json", guidance)
    feedback: Mapping[str, Any] = {}
    _write_state_indexes(directory, candidate_index, synthesis_index, current_best)

    for attempt in range(1, args.max_model_calls + 1):
        if result.get("convergence"):
            break
        if int(result.get("meaningful_iteration_count", 0)) >= args.max_meaningful_iterations:
            result["stopping_reason"] = "max_meaningful_iterations_reached"
            break
        attempt_dir = directory / f"attempt_{attempt:02d}"
        attempt_dir.mkdir(parents=True, exist_ok=True)
        epoch_at_start = int(current_best["epoch"])
        model_history, search_summary = model_history_context(
            result["attempts"], recent_limit=args.model_history_limit
        )
        active_latency_policy = latency_policy_for_prompt(
            latency_contract,
            current_best_total_latency_regression_cycles=int(
                current_best.get("verified_total_latency_regression_cycles", 0)
            ),
        )
        request = make_request(
            golden_snapshot, current_best, current_best_evidence, guidance,
            model_history, search_summary, user_budgets(args), active_latency_policy,
            design_capabilities,
        )
        schema = json.loads(SCHEMA.read_text())
        try:
            system, user, sent_guidance, budget = render_groq_prompt(
                request, schema, feedback, args.max_guidance_chars
            )
        except Exception as exc:
            from guidance_packet import build_packet
            fallback = build_packet([], None, None, {}, mode=args.optimizer_mode)
            fallback["guidance_error"] = f"prompt_guidance_error: {type(exc).__name__}: {exc}"
            request["guidance"] = fallback
            system, user, sent_guidance, budget = render_groq_prompt(
                request, schema, feedback, args.max_guidance_chars
            )
        attempt_guidance = copy.deepcopy(guidance)
        attempt_guidance.update(sent_guidance)
        attempt_guidance["prompt_compaction"] = budget
        attempt_guidance["path_groups_sent_to_llm"] = sent_guidance.get("path_groups_sent_to_llm", 0)
        attempt_guidance["mode_used"] = sent_guidance["mode_used"]
        request["optimizer_mode"] = sent_guidance["mode_used"]
        request["guidance"] = sent_guidance
        write_json(attempt_dir / "guidance.json", attempt_guidance)
        write_json(attempt_dir / "request.json", request)
        write_json(attempt_dir / "prompt.json", {"system": system, "user": user, "budget": budget})

        row: dict[str, Any] = {
            "attempt": attempt,
            "attempt_number": attempt,
            "status": "started",
            "model_decision": None,
            "current_best_epoch": epoch_at_start,
            "current_best_identity": current_best["identity"],
            "guidance": attempt_guidance,
            "optimization_mode": sent_guidance["mode_used"],
            "prompt_artifact": str(attempt_dir / "prompt.json"),
            "synthesis_no_op": False,
            "synthesis_duplicate": False,
            "ppa_deltas": {},
            "transformation_family_history": [guided_attempt_record(r) for r in result["attempts"]],
            "model_call_number": attempt,
            "meaningful_iteration": False,
            "meaningful_iteration_number": None,
        }
        result["attempts"].append(row)
        _write_run_checkpoint(directory, checkpoint, result)

        try:
            row["groq_call_attempted"] = True
            result["model_call_count"] = int(result.get("model_call_count", 0)) + 1
            groq_result = call_groq(
                system_prompt=system, user_prompt=user, model=args.model,
                max_tokens=args.max_tokens, reasoning_effort=args.reasoning_effort,
                temperature=args.temperature, response_schema_path=SCHEMA,
                return_usage=True,
            )
            raw, usage = groq_result
            row["groq_usage"] = usage
            row["candidate_response_sha256"] = hashlib.sha256(raw.encode("utf-8")).hexdigest()
            (attempt_dir / "groq_candidate.json").write_text(raw + "\n", encoding="utf-8")
            if str(usage.get("finish_reason") or "").lower() == "length":
                row.update(
                    status="model_output_truncated",
                    reason=(
                        f"provider finish_reason=length: response reached max_tokens={args.max_tokens} "
                        "before a complete schema-valid JSON candidate was produced"
                    ),
                    rejection_reason="model_output_truncated",
                )
                feedback = retry_feedback(row)
                row["retry_feedback"] = feedback
                write_json(attempt_dir / "feedback.json", feedback)
                write_json(attempt_dir / "history.json", guided_attempt_record(row))
                _write_run_checkpoint(directory, checkpoint, result)
                continue
        except Exception as exc:
            row.update(status="groq_failed", reason=f"{type(exc).__name__}: {exc}")
            feedback = retry_feedback(row)
            row["retry_feedback"] = feedback
            write_json(attempt_dir / "feedback.json", feedback)
            write_json(attempt_dir / "history.json", guided_attempt_record(row))
            _write_run_checkpoint(directory, checkpoint, result)
            continue

        try:
            payload = extract_json_response(raw)
            decision = payload.get("decision", "propose_candidate")
            row["model_decision"] = decision

            if decision == "no_further_optimization":
                parsed_stop = extract_optimizer_response(raw, bundle=None)
                if not isinstance(parsed_stop, StopRecommendation):
                    raise ValueError("stop response did not parse as StopRecommendation")
                row["model_stop_rationale"] = parsed_stop.rationale
                convergence = model_stop_convergence_evidence(
                    result["attempts"], rejected_synthesis_epoch,
                    epoch=epoch_at_start,
                    min_unique_rejected_syntheses=args.convergence_min_unique_syntheses,
                    duplicate_limit=args.convergence_duplicate_limit,
                )
                if convergence is not None:
                    row["status"] = "model_stop_accepted"
                    result["convergence"] = {**convergence, "model_recommended_stop": True,
                                             "model_rationale": parsed_stop.rationale}
                else:
                    row["status"] = "stop_recommendation_not_supported"
                continue

            if decision != "propose_candidate":
                raise ValueError(f"unsupported model decision {decision!r}")

            editing_base = payload.get("editing_base")
            if editing_base == "golden":
                selected_snapshot = golden_snapshot
            elif editing_base == "current_best":
                selected_snapshot = current_best
            else:
                raise ValueError("candidate editing_base must be golden or current_best")
            row["editing_base"] = editing_base
            # Backend resolves the selected snapshot identity.  The LLM chooses only
            # the role (golden/current_best); it does not echo trusted identity/hash
            # values back into the response.
            expected_identity = selected_snapshot["identity"]
            row["editing_base_identity"] = expected_identity

            family = payload.get("chosen_pattern_id")
            if isinstance(family, str) and family.strip():
                row.update(
                    attempted_transformation_family=normalize_transformation_family(family),
                    family_provenance="candidate.chosen_pattern_id",
                )
            primary_transformation = payload.get("primary_transformation")
            proposed_transformations = list(payload.get("proposed_transformations") or [])
            if isinstance(primary_transformation, str) and primary_transformation:
                proposed_transformations = [primary_transformation] + [
                    item for item in proposed_transformations if item != primary_transformation
                ]
            row["primary_transformation"] = primary_transformation
            row["proposed_transformations"] = proposed_transformations
            row["predicted_latency_change_cycles"] = payload.get("predicted_latency_change_cycles")
            row["sequential_changes"] = payload.get("sequential_changes") or []

            selected_manifest = snapshot_manifest(selected_snapshot)
            selected_bundle = snapshot_bundle(selected_snapshot)
            try:
                parsed = extract_optimizer_response(raw, bundle=selected_bundle)
            except CandidateSourceBundleFileSetError as exc:
                row.update(
                    status="candidate_rejected",
                    rejection_reason="source_bundle_file_set_changed",
                    reason=str(exc),
                    source_bundle_file_set={
                        "expected": list(exc.expected_files),
                        "returned": list(exc.returned_files),
                        "missing": list(exc.missing_files),
                        "unexpected": list(exc.unexpected_files),
                    },
                    code_bug=False,
                )
                continue
            if isinstance(parsed, StopRecommendation):
                raise ValueError("candidate branch parsed as stop recommendation")
            if parsed.complete_sources is None:
                raise ValueError("latency-aware candidate must provide a complete source bundle")

            # HARD exact-copy gate before lint/Yosys/formal.  Record every fully
            # specified candidate immediately, including candidates that later fail lint.
            candidate_bundle_sha, candidate_source_hashes = candidate_bundle_sha256(
                selected_manifest, parsed.complete_sources
            )
            row["candidate_source_sha256"] = candidate_bundle_sha
            row["candidate_source_hashes"] = candidate_source_hashes
            golden_bundle = snapshot_bundle(golden_snapshot)
            current_bundle = snapshot_bundle(current_best)
            duplicate_record = None
            if candidate_bundle_sha == current_bundle.bundle_sha256():
                duplicate_record = {"duplicate": True, "duplicate_of": "current_best",
                                    "current_best_epoch": current_best["epoch"]}
            elif candidate_bundle_sha == golden_bundle.bundle_sha256():
                duplicate_record = {"duplicate": True, "duplicate_of": "golden"}
            elif candidate_bundle_sha in candidate_index:
                prior = candidate_index[candidate_bundle_sha]
                duplicate_record = {"duplicate": True, "duplicate_of": "prior_candidate",
                                    "duplicate_of_attempt": prior.get("attempt")}
            if duplicate_record is not None:
                row["exact_source_duplicate"] = duplicate_record
                row["duplicate_of_attempt"] = duplicate_record.get("duplicate_of_attempt")
                row["status"] = "exact_source_duplicate"
                continue

            candidate_index[candidate_bundle_sha] = {
                "attempt": attempt, "editing_base": editing_base,
                "current_best_epoch": epoch_at_start,
            }
            _write_state_indexes(directory, candidate_index, synthesis_index, current_best)

            materialization_request = lint_request_for_selected_base(request, selected_snapshot)
            lint = lint_candidate(
                manifest=selected_manifest,
                base_dir=Path(str(selected_snapshot["base_dir"])),
                request=materialization_request,
                candidate_text=raw,
                yosys_exe=str(cal.v2.YOSYS),
                candidate_out_dir=attempt_dir / "candidate",
            )
            row["lint"] = lint
            row["structural_effects"] = structural_effects_from_lint(lint)
            write_json(attempt_dir / "lint.json", lint)
            if not lint.get("ok"):
                row["status"] = "lint_failed"
                continue

            candidate_manifest = DesignManifest.read_json(attempt_dir / "candidate/design_manifest.json")
            candidate_bundle = SourceBundle.from_manifest(candidate_manifest, attempt_dir / "candidate", verify_manifest_hashes=True)
            if candidate_bundle.bundle_sha256() != candidate_bundle_sha:
                raise ValueError("materialized candidate identity differs from pre-lint complete-source identity")

            result["meaningful_iteration_count"] = int(result.get("meaningful_iteration_count", 0)) + 1
            row["meaningful_iteration"] = True
            row["meaningful_iteration_number"] = result["meaningful_iteration_count"]

            # Formal is ALWAYS frozen Golden versus materialized Candidate.
            golden_source = snapshot_primary_source(golden_snapshot)
            candidate_source = candidate_manifest.resolve_path(candidate_manifest.source_files[0], attempt_dir / "candidate")
            groq_pair = copy.deepcopy(pair)
            groq_pair["baseline"] = {**groq_pair["baseline"], "source": str(golden_source), "top": golden_manifest.top}
            groq_pair["reference"] = {"source": str(candidate_source), "top": golden_manifest.top}
            row["formal_reference"] = golden_snapshot["identity"]
            formal_dir = attempt_dir / "formal"
            formal_dir.mkdir(parents=True, exist_ok=True)
            module = cal.v2.elaborate(groq_pair, formal_dir)
            if module is None:
                row.update(status="candidate_elaboration_failed", reason=groq_pair.get("qualification_reason"))
                continue

            current_best_latency = int(current_best.get("verified_total_latency_regression_cycles", 0))
            if args.max_latency_regression_cycles == 0 or not latency_contract.supported:
                proof = cal.v2.verify(groq_pair, formal_dir)
                latency_result = cycle_exact_result(
                    proof, current_best_total_latency_regression_cycles=current_best_latency
                )
                if args.max_latency_regression_cycles > 0 and not latency_contract.supported:
                    latency_result.update(
                        verification_mode="cycle_exact_fallback_unsupported_latency_contract",
                        latency_change_supported=False,
                        requested_max_latency_regression_cycles=args.max_latency_regression_cycles,
                        latency_contract_reason=latency_contract.reason,
                    )
            else:
                latency_result = verify_fixed_latency(
                    golden_manifest=golden_manifest,
                    golden_base_dir=Path(str(golden_snapshot["base_dir"])),
                    candidate_manifest=candidate_manifest,
                    candidate_base_dir=attempt_dir / "candidate",
                    elaborated_golden_module=selected["module"],
                    contract=latency_contract,
                    output_dir=attempt_dir / "latency",
                    yosys_exe=cal.v2.YOSYS,
                    run_command=cal.v2.run,
                    current_best_total_latency_regression_cycles=current_best_latency,
                    predicted_latency_change_cycles=row.get("predicted_latency_change_cycles"),
                    timeout_s=int(golden_manifest.verification.options.get("timeout_s", 120)),
                )
                proof = {
                    "status": "passed" if latency_result.get("passed") else (
                        "timeout" if latency_result.get("status") == "inconclusive_timeout" else "unproven"
                    ),
                    "mode": "fixed_cycle_latency",
                    "verified_shift_cycles": latency_result.get("verified_shift_cycles"),
                    "reason": latency_result.get("reason"),
                }
            row["latency_result"] = latency_result
            write_json(attempt_dir / "latency_result.json", latency_result)
            row["formal"] = proof
            row["formal_classification"] = latency_result.get("formal_status") or latency_result.get("status")
            if not latency_result.get("passed"):
                status = latency_result.get("status")
                if status == "inconclusive_timeout":
                    row["status"] = "formal_timeout"
                elif status == "tool_failure":
                    row["status"] = "formal_tool_failure"
                elif status == "unsupported_contract":
                    row["status"] = "latency_contract_unsupported"
                elif status == "latency_budget_exceeded":
                    row["status"] = "latency_budget_exceeded"
                else:
                    row["status"] = "not_proven"
                row["reason"] = latency_result.get("reason")
                continue

            # PPA baseline is ALWAYS the unchanged Current Best snapshot at attempt start.
            physical_dir = attempt_dir / "paired_recipe"
            recipe, physical_candidate, _profile = cal.freeze(
                groq_pair, physical_dir, selected["module"], selected["period_ns"], "groq_paired"
            )
            current_best_source = snapshot_primary_source(current_best)
            row["ppa_comparison_reference"] = current_best["identity"]
            synth_b = cal.physical(groq_pair, physical_dir, "baseline", recipe, current_best_source,
                                   f"attempt{attempt}", stage="synth", timeout_s=args.ppa_timeout_s)
            synth_c = cal.physical(groq_pair, physical_dir, "reference", recipe, physical_candidate,
                                   f"attempt{attempt}", stage="synth", timeout_s=args.ppa_timeout_s)
            bh = synthesis_hash(synth_b, physical_dir, "baseline")
            ch = synthesis_hash(synth_c, physical_dir, "candidate")
            row["synthesis"] = {"baseline": synth_b, "candidate": synth_c,
                                "baseline_hash": bh, "candidate_hash": ch}

            baseline_key = synthesis_hash_key(bh)
            candidate_key = synthesis_hash_key(ch)

            # Every accepted Current Best is already a fully evaluated implementation.
            # Register its synthesis identity so a later source-different candidate cannot
            # revisit an older promoted/baseline topology after Current Best advances.
            if baseline_key is not None and baseline_key not in synthesis_index:
                synthesis_index[baseline_key] = {
                    "implementation_id": f"synth_impl_{len(synthesis_index) + 1:03d}",
                    "first_attempt": None,
                    "first_current_best_epoch": epoch_at_start,
                    "normalized_synthesis_hash": bh.get("sha256"),
                    "normalization": bh.get("normalization"),
                    "fully_evaluated": True,
                    "accepted_reference": True,
                    "reference_identity": current_best["identity"],
                }

            if candidate_key is not None and candidate_key not in synthesis_index:
                synthesis_index[candidate_key] = {
                    "implementation_id": f"synth_impl_{len(synthesis_index) + 1:03d}",
                    "first_attempt": attempt,
                    "first_current_best_epoch": epoch_at_start,
                    "normalized_synthesis_hash": ch.get("sha256"),
                    "normalization": ch.get("normalization"),
                    "fully_evaluated": False,
                }
            if candidate_key is not None:
                row["synthesis_implementation_id"] = synthesis_index[candidate_key]["implementation_id"]

            row["synthesis_no_op"] = synthesis_no_op(bh, ch)
            if row["synthesis_no_op"]:
                row["status"] = "no_op_after_synthesis"
                _write_state_indexes(directory, candidate_index, synthesis_index, current_best)
                continue

            prior_impl = synthesis_index.get(candidate_key) if candidate_key is not None else None
            if prior_impl is not None and prior_impl.get("fully_evaluated") is True:
                row.update(
                    status="duplicate_after_synthesis",
                    synthesis_duplicate=True,
                    duplicate_of_attempt=prior_impl.get("first_fully_evaluated_attempt") or prior_impl.get("first_attempt"),
                    duplicate_of_status=prior_impl.get("last_status") or ("accepted_reference" if prior_impl.get("accepted_reference") else None),
                    duplicate_of_ppa_deltas=dict(prior_impl.get("ppa_deltas", {})),
                    synthesis_implementation_id=prior_impl.get("implementation_id"),
                    reason=(
                        "normalized synthesis implementation was already fully evaluated"
                        + (f" in attempt {prior_impl.get('first_fully_evaluated_attempt') or prior_impl.get('first_attempt')}"
                           if (prior_impl.get("first_fully_evaluated_attempt") or prior_impl.get("first_attempt"))
                           else " as an accepted Current-Best reference")
                    ),
                )
                prior_impl.setdefault("duplicate_attempts", []).append(attempt)
                continue

            baseline = cal.physical(groq_pair, physical_dir, "baseline", recipe, current_best_source, f"attempt{attempt}", timeout_s=args.ppa_timeout_s)
            candidate = cal.physical(groq_pair, physical_dir, "reference", recipe, physical_candidate, f"attempt{attempt}", timeout_s=args.ppa_timeout_s)
            promotion = assess_guided_candidate(
                baseline, candidate, proof, user_budgets(args),
                comparison_reference=current_best["identity"],
                latency_result=row.get("latency_result"),
            )
            row.update(
                status="promoted" if promotion["keep"] else "analyzed" if promotion["comparable"] else "physical_flow_failed",
                ppa={"baseline": baseline, "candidate": candidate},
                promotion=promotion,
                ppa_deltas=promotion.get("deltas", {}),
            )

            if promotion["keep"]:
                rb = cal.physical(groq_pair, physical_dir, "baseline", recipe, current_best_source, f"attempt{attempt}", repeat=1, timeout_s=args.ppa_timeout_s)
                rc = cal.physical(groq_pair, physical_dir, "reference", recipe, physical_candidate, f"attempt{attempt}", repeat=1, timeout_s=args.ppa_timeout_s)
                repeat = assess_guided_candidate(
                    rb, rc, proof, user_budgets(args),
                    comparison_reference=current_best["identity"],
                    latency_result=row.get("latency_result"),
                )
                row.update(repeat_ppa={"baseline": rb, "candidate": rc}, repeat_promotion=repeat)
                if not repeat["keep"]:
                    row.update(status="analyzed" if repeat["comparable"] else "physical_flow_failed")
                    row["promotion"] = {**promotion, "keep": False, "reason": "repeat_did_not_confirm"}
                else:
                    # Promotion changes Current Best; Golden remains frozen.  Duplicate
                    # databases persist globally, but convergence evidence resets per epoch.
                    new_epoch = epoch_at_start + 1
                    promoted_metrics = rc.get("metrics", candidate.get("metrics", {}))
                    current_best = clone_bundle_snapshot(
                        candidate_manifest, attempt_dir / "candidate",
                        state_root / "current_best" / f"epoch_{new_epoch:03d}",
                        role="current_best", epoch=new_epoch,
                        metrics=promoted_metrics, promoted_from_attempt=attempt,
                        verified_total_latency_regression_cycles=int(
                            row.get("latency_result", {}).get("verified_total_latency_regression_cycles", 0)
                        ),
                    )
                    with (state_root / "current_best_history.jsonl").open("a", encoding="utf-8") as f:
                        f.write(json.dumps(current_best, sort_keys=True) + "\n")
                    current_best_run = rc
                    current_best_source = snapshot_primary_source(current_best)
                    current_best_evidence = {
                        "selected_period_ns": selected["period_ns"],
                        "metrics": promoted_metrics,
                        "source_role": "current_best",
                        "current_best_epoch": new_epoch,
                        "promoted_from_attempt": attempt,
                        "verified_total_latency_regression_cycles": int(
                            current_best.get("verified_total_latency_regression_cycles", 0)
                        ),
                    }
                    guidance = diagnose(
                        current_best_run, current_best_source, golden_manifest.top,
                        attempt_dir / "next_current_best_guidance", args,
                        history=(),
                    )
                    guidance.setdefault("evidence", {})["source_role"] = "current_best"
                    guidance["evidence"]["current_best_epoch"] = new_epoch
                    write_json(attempt_dir / "next_current_best_guidance.json", guidance)
                    rejected_synthesis_epoch = {}
                    result["promotion_count"] = int(result.get("promotion_count", 0)) + 1

            if candidate_key in synthesis_index:
                final_promotion = row.get("promotion") if isinstance(row.get("promotion"), Mapping) else {}
                repeat_promotion = row.get("repeat_promotion") if isinstance(row.get("repeat_promotion"), Mapping) else None
                fully_evaluated = bool(
                    final_promotion.get("comparable")
                    and row.get("status") in {"analyzed", "promoted"}
                    and (repeat_promotion is None or repeat_promotion.get("comparable"))
                )
                synthesis_index[candidate_key].update(
                    last_status=row.get("status"),
                    ppa_deltas=dict(row.get("ppa_deltas", {})),
                    promoted=row.get("status") == "promoted",
                    fully_evaluated=fully_evaluated,
                )
                if fully_evaluated:
                    synthesis_index[candidate_key].setdefault("first_fully_evaluated_attempt", attempt)
            if row.get("status") != "promoted":
                row["synthesis_hash_recorded_for_dedupe"] = _remember_rejected_synthesis_hash(
                    row, rejected_synthesis_global, rejected_synthesis_epoch
                )

            _write_state_indexes(directory, candidate_index, synthesis_index, current_best)

        except CandidateExtractionError as exc:
            row.update(
                status="candidate_rejected",
                rejection_reason="invalid_candidate_response",
                reason=f"{type(exc).__name__}: {exc}",
                code_bug=False,
            )
        except Exception as exc:
            is_infra = isinstance(exc, (OSError, subprocess.SubprocessError, TimeoutError, MemoryError))
            row.update(
                status="infrastructure_failed",
                reason=f"{type(exc).__name__}: {exc}",
                traceback=traceback.format_exc(),
                code_bug=not is_infra,
            )
        finally:
            row["rejection_reason"] = _candidate_rejection_reason(row)
            feedback = retry_feedback(row)
            row["retry_feedback"] = feedback
            write_json(attempt_dir / "feedback.json", feedback)
            write_json(attempt_dir / "history.json", guided_attempt_record(row))
            _write_state_indexes(directory, candidate_index, synthesis_index, current_best)
            # Automatic convergence is scoped to the active Current-Best epoch.
            # Doing this in finally ensures early-exit duplicate branches are counted.
            if not result.get("convergence"):
                current_epoch = int(current_best["epoch"])
                convergence = convergence_state(
                    result["attempts"], rejected_synthesis_epoch,
                    epoch=current_epoch,
                    min_unique_rejected_syntheses=args.convergence_min_unique_syntheses,
                    duplicate_limit=args.convergence_duplicate_limit,
                )
                if convergence is not None:
                    result["convergence"] = convergence
            _write_run_checkpoint(directory, checkpoint, result)

        if result.get("convergence"):
            break

    result["golden_snapshot"] = golden_snapshot
    result["final_current_best"] = current_best
    result["current_best_epoch"] = current_best["epoch"]
    result["final_verified_total_latency_regression_cycles"] = int(
        current_best.get("verified_total_latency_regression_cycles", 0)
    )
    result["rejected_synthesis_hash_history"] = sorted(
        rejected_synthesis_global.values(), key=lambda item: int(item.get("attempt") or 0)
    )
    result["evaluation_records"] = [compact_attempt_measurements(r, pair["id"]) for r in result["attempts"]]
    if result.get("convergence"):
        final_status = "converged"
    elif int(current_best["epoch"]) > 0:
        final_status = "promoted"
    elif result["attempts"]:
        final_status = result["attempts"][-1]["status"]
    else:
        final_status = "no_attempts"
    result.update(status=final_status, period_ns=selected["period_ns"])
    from report_guided_experiment import aggregate
    result["statistics"] = aggregate([result])
    _write_run_checkpoint(directory, checkpoint, result)
    return result


def self_test() -> None:
    """Retained entry point; external/project tests are not run during packaging."""
    from test_guided_rtl_opt import run_tests
    run_tests()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--output", type=Path, help="new immutable run directory (default: timestamp plus unique ID)")
    ap.add_argument("--pairs", nargs="*", help="RTL-OPT pair ids; defaults to the first ten corpus entries")
    ap.add_argument("--model", default=os.environ.get("OPTIMIZER_MODEL", "openai/gpt-oss-120b"))
    ap.add_argument("--max-tokens", type=int, default=6500)
    ap.add_argument("--reasoning-effort", choices=("low", "medium", "high"), default="medium")
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--max-attempts", type=int, default=5,
                    help="backward-compatible default for model-call and meaningful-iteration limits")
    ap.add_argument("--max-model-calls", type=int, default=None,
                    help="hard cap on provider/model invocations; defaults to --max-attempts")
    ap.add_argument("--max-meaningful-iterations", type=int, default=None,
                    help="cap on novel structurally valid candidates reaching semantic verification; defaults to --max-attempts")
    ap.add_argument("--max-latency-regression-cycles", type=int, default=0,
                    help="maximum backend-verified total latency regression from frozen Golden; 0 preserves cycle-exact behavior")
    ap.add_argument("--convergence-duplicate-limit", type=int, default=2,
                    help="early-stop duplicate-hit threshold after enough unique rejected syntheses; 0 disables")
    ap.add_argument("--convergence-min-unique-syntheses", type=int, default=2,
                    help="minimum distinct comparable rejected syntheses in the current-best epoch before convergence can stop")
    ap.add_argument("--optimizer-mode", choices=("auto", "guided", "exploratory"), default="auto")
    ap.add_argument("--guidance-source", choices=("timing-report",), default="timing-report")
    ap.add_argument("--max-guidance-groups", type=int, default=3)
    ap.add_argument("--max-excerpts-per-group", type=int, default=2)
    ap.add_argument("--max-guidance-chars", type=int, default=6000)
    ap.add_argument("--model-history-limit", type=int, default=5,
                    help="maximum recent meaningful attempts sent verbatim to the model; older history is aggregated")
    ap.add_argument("--formal-timeout-s", type=int, default=120,
                    help="per temporal/cycle-exact formal invocation timeout in seconds")
    ap.add_argument("--ppa-timeout-s", type=int, default=900,
                    help="per ORFS synthesis/physical stage timeout in seconds")
    ap.add_argument("--max-area-regression-pct", type=float, default=0.5)
    ap.add_argument("--max-power-regression-pct", type=float, default=0.5)
    ap.add_argument("--max-setup-slack-regression-ns", type=float, default=0.0)
    ap.add_argument("--max-hold-slack-regression-ns", type=float, default=0.0)
    ap.add_argument("--experiment", choices=("single", "paired-guidance"), default="single")
    ap.add_argument("--repetitions", type=int, default=1)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        self_test()
        return 0
    if args.max_model_calls is None:
        args.max_model_calls = args.max_attempts
    if args.max_meaningful_iterations is None:
        args.max_meaningful_iterations = args.max_attempts
    for name in ("max_attempts", "max_model_calls", "max_meaningful_iterations", "max_guidance_groups", "max_excerpts_per_group", "repetitions", "max_tokens",
                 "model_history_limit", "convergence_min_unique_syntheses", "formal_timeout_s", "ppa_timeout_s"):
        if getattr(args, name) < 1:
            ap.error(name.replace("_", "-") + " must be positive")
    if args.convergence_duplicate_limit < 0:
        ap.error("--convergence-duplicate-limit must be nonnegative (0 disables)")
    if args.max_latency_regression_cycles < 0:
        ap.error("--max-latency-regression-cycles must be nonnegative")
    if args.max_guidance_chars < 512:
        ap.error("--max-guidance-chars must be at least 512")
    if not math.isfinite(args.temperature) or not 0 <= args.temperature <= 2:
        ap.error("--temperature must be finite and in [0, 2]")
    if any(not math.isfinite(v) for v in user_budgets(args).values()):
        ap.error("acceptance budgets must be finite")
    if any(v < 0 for v in user_budgets(args).values()):
        ap.error("area/power/setup/hold regression limits must be nonnegative")
    if not os.environ.get("GROQ_API_KEY"):
        ap.error("GROQ_API_KEY is not set; source env.sh and ensure .env.local is present")

    output = (args.output or ROOT / "ai_runs" / (
        "rtl_opt_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex[:8]
    )).resolve()
    if output.exists():
        ap.error("--output already exists; use a new directory to preserve immutable experiment arms")
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "config.json", {
        **{k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
        "output": str(output),
        "runner_sha256": sha256(Path(__file__)),
        "state_policy": "immutable Golden; promoted-only Current Best; formal vs Golden; PPA vs Current Best",
        "latency_changing_support": True,
        "latency_support_scope": "single-clock fixed nonnegative design-level cycle shift; backend-proven against frozen Golden",
    })

    manifest = cal.prepare(output / "inputs")
    ids = args.pairs or [p["id"] for p in manifest["pairs"][:10]]
    selected_pairs = [dict(p) for p in manifest["pairs"] if p["id"] in set(ids)]
    if len(selected_pairs) != len(set(ids)):
        ap.error("one or more --pairs ids are not in the RTL-OPT corpus")
    config = json.loads((output / "config.json").read_text())
    config["selected_ids"] = ids
    write_json(output / "config.json", config)

    from report_guided_experiment import report, summarize
    arms = {"guided": [], "exploratory": []} if args.experiment == "paired-guidance" else {args.optimizer_mode: []}
    for repetition in range(1, args.repetitions + 1):
        for pair in selected_pairs:
            baseline_data = select_baseline(
                copy.deepcopy(pair), output / "baselines" / f"repetition_{repetition:02d}" / pair["id"],
                physical_timeout_s=args.ppa_timeout_s,
            )
            order = list(arms)
            if repetition % 2 == 0:
                order.reverse()
            for arm in order:
                arm_args = copy.copy(args)
                arm_args.optimizer_mode = arm
                arm_dir = output / arm / f"repetition_{repetition:02d}"
                row = run_one(pair, arm_dir, arm_args, baseline_data=baseline_data)
                row["repetition"] = repetition
                write_json(arm_dir / pair["id"] / "result.json", row)
                arms[arm].append(row)
                write_json(arm_dir / "groq_summary.json", {
                    "selected": ids,
                    "results": [r for r in arms[arm] if r["repetition"] == repetition],
                })
                write_json(output / "groq_summary.json", {
                    "selected": ids,
                    "results": [r for rows in arms.values() for r in rows],
                    "statistics": summarize(arms),
                })
    if args.experiment == "paired-guidance":
        report(output)
    print(f"Results: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
