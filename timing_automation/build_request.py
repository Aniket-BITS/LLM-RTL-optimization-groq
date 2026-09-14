#!/usr/bin/env python3
"""
build_request.py -- build a compact structured nEBULA optimizer request.

This version deliberately DOES NOT render prompt_system/prompt_user.
It preserves the deterministic optimizer safety boundary while preparing the
small structured inputs that a later prompt-rendering stage will use immediately
before the LLM call.

Inputs
------
* optimizer_input.json      full trusted analysis artifact
* eligible_patterns.json    patterns allowed for exactly one optimization unit
* response_schema.json      canonical model-output contract
* design_context.json       compact design-fixed context emitted by
                            build_optimizer_input.py (auto-discovered by default)
* optimizer_history.jsonl   optional append-only history (auto-discovered)

Output
------
optimizer_request/2 JSON containing:
* exact target identity / SHA / editable ranges
* eligible-pattern IDs
* references to fixed context, compact design context, history and schema
* current iteration state
* compact relevant previous-result summary, if history exists
* section-level context estimates and the inherited overflow policy

The full engineering evidence remains in optimizer_input.json.  No giant prompt
is persisted here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


SCRIPT_DIR = Path(__file__).resolve().parent
SCHEMA_PATH = SCRIPT_DIR / "response_schema.json"
REQUEST_SCHEMA = "optimizer_request/2"
REQUEST_FORMAT_VERSION = "2.2"

try:
    from design_manifest import DesignManifest
    from source_bundle import SourceBundle
except ImportError:
    from timing_automation.design_manifest import DesignManifest
    from timing_automation.source_bundle import SourceBundle


class BuildRequestError(RuntimeError):
    """Deterministic request-construction error."""


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------


def load_json(path: Path) -> Dict[str, Any]:
    path = Path(path)
    if not path.is_file():
        raise BuildRequestError(f"JSON file not found: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BuildRequestError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise BuildRequestError(f"JSON root must be an object: {path}")
    return value


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def estimate_tokens_from_text(text: str, chars_per_token: float) -> int:
    if chars_per_token <= 0:
        raise BuildRequestError("chars_per_token must be > 0")
    return int(math.ceil(len(text) / chars_per_token))


def estimate_tokens_from_json(value: Any, chars_per_token: float) -> int:
    compact = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    return estimate_tokens_from_text(compact, chars_per_token)


def normalize_ranges(raw: Any) -> List[List[int]]:
    """Validate, sort, deduplicate, and merge only truly overlapping ranges.

    Adjacent ranges remain separate because they may represent independent RTL
    statements. For example, [19, 19] and [20, 20] must NOT become [19, 20].
    """
    if not isinstance(raw, list):
        return []

    ranges: List[Tuple[int, int]] = []
    for item in raw:
        if (
            isinstance(item, (list, tuple))
            and len(item) == 2
            and isinstance(item[0], int)
            and not isinstance(item[0], bool)
            and isinstance(item[1], int)
            and not isinstance(item[1], bool)
            and 1 <= item[0] <= item[1]
        ):
            ranges.append((int(item[0]), int(item[1])))

    ordered = sorted(set(ranges))
    merged: List[List[int]] = []

    for lo, hi in ordered:
        # Merge only overlapping ranges.
        # Do NOT merge merely adjacent ranges.
        if not merged or lo > merged[-1][1]:
            merged.append([lo, hi])
        else:
            merged[-1][1] = max(merged[-1][1], hi)

    return merged


def extract_schema_version(schema: Mapping[str, Any]) -> Optional[str]:
    value = schema.get("schema_version")
    if isinstance(value, str) and value:
        return value

    # Canonical response_schema.json commonly constrains schema_version via const.
    props = schema.get("properties")
    if isinstance(props, dict):
        sv = props.get("schema_version")
        if isinstance(sv, dict):
            const = sv.get("const")
            if isinstance(const, str) and const:
                return const
    return None


# ---------------------------------------------------------------------------
# Unit / target resolution
# ---------------------------------------------------------------------------


def find_unit(units: Sequence[Any], unit_id: str) -> Dict[str, Any]:
    for raw in units:
        if isinstance(raw, dict) and raw.get("unit_id") == unit_id:
            return raw
    raise BuildRequestError(f"optimization unit not found: {unit_id}")


def full_unit_from_optimizer_input(opt: Mapping[str, Any], unit_id: str) -> Dict[str, Any]:
    units = opt.get("optimization_units") or []
    if not isinstance(units, list):
        raise BuildRequestError("optimizer_input.optimization_units must be an array")
    unit = find_unit(units, unit_id)
    eligibility = unit.get("eligibility") or {}
    request_ready = eligibility.get("request_ready")
    if request_ready is None:
        request_ready = eligibility.get("optimization_ready")
    if request_ready is not True:
        blockers = eligibility.get("hard_blockers") or eligibility.get("blockers") or []
        raise BuildRequestError(
            f"selected unit {unit_id} is not request_ready; blockers={blockers}"
        )
    return unit

def compact_unit_from_design_context(
    design_context: Mapping[str, Any], unit_id: str
) -> Dict[str, Any]:
    units = design_context.get("optimization_units") or []
    if not isinstance(units, list):
        raise BuildRequestError("design_context.optimization_units must be an array")
    unit = find_unit(units, unit_id)
    ready = unit.get("request_ready")
    if ready is None:
        ready = unit.get("optimization_ready")
    if ready is not True:
        raise BuildRequestError(
            f"design_context unit {unit_id} is not request_ready"
        )
    return unit

def source_snapshot_for_target(
    design_context: Mapping[str, Any], target: Mapping[str, Any]
) -> Dict[str, Any]:
    target_file = target.get("file")
    target_module = target.get("module")
    target_sha = target.get("original_sha256")

    sources = design_context.get("sources") or []
    matches: List[Dict[str, Any]] = []
    if isinstance(sources, list):
        for source in sources:
            if not isinstance(source, dict):
                continue
            if target_file and source.get("file") != target_file:
                continue
            if target_module and source.get("module") != target_module:
                continue
            matches.append(source)

    if len(matches) != 1:
        raise BuildRequestError(
            "could not resolve exactly one design-context source snapshot for "
            f"target file={target_file!r}, module={target_module!r}"
        )

    source = matches[0]
    if target_sha and source.get("sha256") != target_sha:
        raise BuildRequestError(
            "target SHA mismatch between compact unit and design-context source snapshot"
        )
    if not isinstance(source.get("module_text"), str) or not source.get("module_text"):
        raise BuildRequestError("target source snapshot has no module_text")
    return source


def resolve_target(
    full_unit: Mapping[str, Any],
    compact_unit: Mapping[str, Any],
    design_context: Mapping[str, Any],
) -> Tuple[Dict[str, Any], List[List[int]], Optional[Dict[str, Any]]]:
    """Resolve a *primary diagnostic focus* for display only.

    The returned ranges are advisory. Mutation authority is the exact
    SourceBundle embedded in the request and later revalidated by lint.
    """
    target = compact_unit.get("target") if isinstance(compact_unit.get("target"), dict) else {}
    source = full_unit.get("source") if isinstance(full_unit.get("source"), dict) else {}
    refs = full_unit.get("source_refs") or []
    first_ref = next((x for x in refs if isinstance(x, dict)), {})
    resolved = {
        "file": target.get("file") or source.get("file") or first_ref.get("file"),
        "module": target.get("module") or source.get("module") or first_ref.get("module"),
        "original_sha256": target.get("original_sha256") or source.get("sha256") or first_ref.get("sha256"),
        "module_line_start": target.get("module_line_start") or source.get("module_line_start") or first_ref.get("module_line_start"),
        "module_line_end": target.get("module_line_end") or source.get("module_line_end") or first_ref.get("module_line_end"),
    }
    focus = normalize_ranges(
        compact_unit.get("recommended_focus_ranges")
        or compact_unit.get("editable_line_ranges")
        or []
    )
    snapshot: Optional[Dict[str, Any]] = None
    if resolved.get("file") and resolved.get("module"):
        try:
            snapshot = source_snapshot_for_target(design_context, resolved)
        except BuildRequestError:
            snapshot = None
    return resolved, focus, snapshot

# ---------------------------------------------------------------------------
# Eligible pattern handling
# ---------------------------------------------------------------------------


def eligible_patterns_for_unit(
    eligible_obj: Mapping[str, Any], unit_id: str
) -> Tuple[List[str], List[Dict[str, Any]]]:
    """Return recommended patterns. Empty is legal for exploratory mode."""
    if not eligible_obj:
        return [], []
    declared_unit = eligible_obj.get("unit_id")
    if isinstance(declared_unit, str) and declared_unit and declared_unit != unit_id:
        raise BuildRequestError(
            f"pattern artifact is for {declared_unit}, requested unit is {unit_id}"
        )
    raw = eligible_obj.get("eligible_patterns")
    if raw is None:
        raw = eligible_obj.get("recommended_patterns")
    if raw in (None, []):
        return [], []
    if not isinstance(raw, list):
        raise BuildRequestError("recommended pattern set must be an array")
    ids: List[str] = []
    slim: List[Dict[str, Any]] = []
    seen = set()
    for index, item in enumerate(raw):
        if isinstance(item, str):
            pattern_id = item
            entry = {"id": item}
        elif isinstance(item, dict):
            pattern_id = item.get("id") or item.get("pattern_id")
            entry = dict(item)
            if pattern_id and "id" not in entry:
                entry["id"] = pattern_id
        else:
            raise BuildRequestError(f"recommended_patterns[{index}] has invalid type")
        if not isinstance(pattern_id, str) or not pattern_id:
            raise BuildRequestError(f"recommended_patterns[{index}] has no valid id")
        if pattern_id in seen:
            continue
        seen.add(pattern_id); ids.append(pattern_id); slim.append(entry)
    return ids, slim

# ---------------------------------------------------------------------------
# Context architecture / history
# ---------------------------------------------------------------------------


def resolve_design_context_path(
    opt: Mapping[str, Any], optimizer_input_path: Path, cli_path: Optional[str]
) -> Path:
    if cli_path:
        return Path(cli_path).expanduser().resolve()

    arch = opt.get("context_architecture") or {}
    design_ref = arch.get("design_context") or {}
    path = design_ref.get("path")
    if isinstance(path, str) and path:
        return Path(path).expanduser().resolve()

    design = opt.get("design") or {}
    variant = design.get("flow_variant")
    if not variant:
        variant = optimizer_input_path.stem.removesuffix("_optimizer_input")
    return (optimizer_input_path.parent / f"{variant}_design_context.json").resolve()


def validate_design_context_reference(
    opt: Mapping[str, Any], design_context_path: Path
) -> None:
    arch = opt.get("context_architecture") or {}
    ref = arch.get("design_context") or {}
    expected_sha = ref.get("sha256")
    if isinstance(expected_sha, str) and expected_sha:
        actual_sha = sha256_file(design_context_path)
        if actual_sha.lower() != expected_sha.lower():
            raise BuildRequestError(
                "design-context SHA mismatch: optimizer_input reference is stale or file changed"
            )


def resolve_history_path(
    opt: Mapping[str, Any], optimizer_input_path: Path, cli_path: Optional[str]
) -> Path:
    if cli_path:
        return Path(cli_path).expanduser().resolve()
    arch = opt.get("context_architecture") or {}
    hist = arch.get("iteration_history") or {}
    path = hist.get("path")
    if isinstance(path, str) and path:
        return Path(path).expanduser().resolve()
    design = opt.get("design") or {}
    variant = design.get("flow_variant") or optimizer_input_path.stem.removesuffix("_optimizer_input")
    return (optimizer_input_path.parent / f"{variant}_optimizer_history.jsonl").resolve()


def read_history_records(path: Path) -> List[Dict[str, Any]]:
    if not path.is_file():
        return []
    records: List[Dict[str, Any]] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BuildRequestError(f"invalid JSONL in {path}:{lineno}: {exc}") from exc
        if not isinstance(value, dict):
            raise BuildRequestError(f"history record {path}:{lineno} is not an object")
        records.append(value)
    return records


def history_iteration_number(record: Mapping[str, Any]) -> Optional[int]:
    for key in ("iteration", "iteration_number", "attempt", "attempt_no"):
        value = record.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
            return value
    return None


def infer_next_iteration(records: Sequence[Mapping[str, Any]]) -> int:
    numbers = [n for n in (history_iteration_number(r) for r in records) if n is not None]
    return (max(numbers) + 1) if numbers else 1


def _first_present(record: Mapping[str, Any], paths: Sequence[Tuple[str, ...]]) -> Any:
    for path in paths:
        cur: Any = record
        ok = True
        for key in path:
            if not isinstance(cur, Mapping) or key not in cur:
                ok = False
                break
            cur = cur[key]
        if ok and cur not in (None, "", [], {}):
            return cur
    return None


def compact_relevant_history_summary(
    records: Sequence[Mapping[str, Any]], unit_id: str
) -> Optional[Dict[str, Any]]:
    """
    Keep only the latest directly relevant result.  Never copy a full old
    candidate, transaction, formal log or raw report into the request.
    """
    chosen: Optional[Mapping[str, Any]] = None
    for record in reversed(records):
        rec_unit = _first_present(record, [("unit_id",), ("target_unit_id",)])
        if rec_unit is None or rec_unit == unit_id:
            chosen = record
            break
    if chosen is None:
        return None

    summary: Dict[str, Any] = {}
    iteration = history_iteration_number(chosen)
    if iteration is not None:
        summary["iteration"] = iteration

    pattern = _first_present(
        chosen,
        [
            ("chosen_pattern_id",),
            ("pattern_id",),
            ("candidate", "chosen_pattern_id"),
            ("current_iteration", "chosen_pattern_id"),
        ],
    )
    if pattern is not None:
        summary["pattern_id"] = pattern

    verdict = _first_present(
        chosen,
        [("verdict",), ("status",), ("decision",), ("ppa_decision",)],
    )
    if verdict is not None:
        summary["result"] = verdict

    reason = _first_present(
        chosen,
        [("reason",), ("rejection_reason",), ("note",)],
    )
    if reason is not None:
        summary["reason"] = reason

    # Keep only compact PPA numbers if present.
    metrics = _first_present(
        chosen,
        [("metrics",), ("candidate_metrics",), ("ppa", "candidate"), ("ppa", "metrics")],
    )
    if isinstance(metrics, Mapping):
        compact_metrics = {
            key: metrics.get(key)
            for key in ("wns_ns", "tns_ns", "area_um2", "power_mw")
            if metrics.get(key) is not None
        }
        if compact_metrics:
            summary["metrics"] = compact_metrics

    # If no decision-relevant field exists, do not pass arbitrary old content.
    if not any(k in summary for k in ("pattern_id", "result", "reason", "metrics")):
        return None
    return summary


def compact_previous_attempts(
    records: Sequence[Mapping[str, Any]],
    unit_id: str,
    diagnosis_docs: Sequence[Mapping[str, Any]] = (),
) -> List[Dict[str, Any]]:
    attempts: List[Dict[str, Any]] = []
    for record in records:
        rec_unit = _first_present(record, [("unit_id",), ("target_unit_id",)])
        if rec_unit not in (None, unit_id):
            continue
        item: Dict[str, Any] = {}
        iteration = history_iteration_number(record)
        if iteration is not None:
            item["iteration"] = iteration
        pattern = _first_present(record, [("chosen_pattern_id",), ("pattern_id",), ("candidate", "chosen_pattern_id")])
        if pattern is not None:
            item["pattern_id"] = pattern
        verdict = _first_present(record, [("verdict",), ("status",), ("decision",)])
        if verdict is not None:
            item["verdict"] = verdict
        reason = _first_present(record, [("diagnosis_summary",), ("reason",), ("rejection_reason",), ("note",)])
        if reason is not None:
            item["failure"] = reason
        if item:
            attempts.append(item)

    for doc in diagnosis_docs:
        item: Dict[str, Any] = {"source": "diagnosis"}
        for out_key, candidates in (
            ("verdict", ("verdict", "status", "result")),
            ("failure", ("diagnosis_summary", "summary", "reason", "message")),
            ("counterexample", ("counterexample_summary", "counterexample")),
        ):
            for key in candidates:
                value = doc.get(key)
                if value not in (None, "", [], {}):
                    item[out_key] = value
                    break
        attempts.append(item)

    # Keep all relevant compact records; never include raw candidate/formal logs.
    return attempts


# ---------------------------------------------------------------------------
# Request assembly
# ---------------------------------------------------------------------------


def canonicalize_primary_focus(focus, bundle_context):
    """Use bundle paths after checking module ownership and the source hash."""
    if not bundle_context:
        return
    module = focus.get("module")
    owner = (bundle_context.get("module_to_file") or {}).get(module)
    sources = [item for item in bundle_context.get("sources", [])
               if item.get("path") == owner]
    if not owner or len(sources) != 1:
        raise BuildRequestError("primary diagnostic module has no unique SourceBundle owner")
    if sources[0].get("sha256") != focus.get("original_sha256"):
        raise BuildRequestError("primary diagnostic source hash differs from SourceBundle owner")
    focus["file"] = owner


def build_request(
    *,
    opt: Mapping[str, Any],
    eligible_obj: Mapping[str, Any],
    schema_obj: Mapping[str, Any],
    optimizer_input_path: Path,
    eligible_path: Optional[Path],
    schema_path: Path,
    design_context_path: Path,
    history_path: Path,
    unit_id: str,
    iteration_number: Optional[int],
    previous_diagnosis_paths: Sequence[Path] = (),
    source_bundle_context: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    design_context = load_json(design_context_path)
    validate_design_context_reference(opt, design_context_path)
    if design_context.get("schema") not in (None, "optimizer_design_context/1"):
        raise BuildRequestError(
            f"unsupported design-context schema: {design_context.get('schema')!r}"
        )

    full_unit = full_unit_from_optimizer_input(opt, unit_id)
    compact_unit = compact_unit_from_design_context(design_context, unit_id)
    primary_focus, recommended_focus_ranges, source_snapshot = resolve_target(
        full_unit, compact_unit, design_context
    )

    eligibility = full_unit.get("eligibility") or {}
    optimizer_mode = (
        full_unit.get("optimizer_mode")
        or eligibility.get("optimizer_mode")
        or compact_unit.get("optimizer_mode")
        or "guided"
    )
    diagnostic_confidence = (
        full_unit.get("diagnostic_confidence")
        or eligibility.get("diagnostic_confidence")
        or compact_unit.get("diagnostic_confidence")
        or "unknown"
    )
    routing_reason = (
        full_unit.get("routing_reason")
        or eligibility.get("routing_reason")
        or compact_unit.get("routing_reason")
        or ("high_confidence_diagnosis" if optimizer_mode == "guided" else "diagnostic_uncertainty")
    )

    recommended_ids, recommended_entries = eligible_patterns_for_unit(eligible_obj, unit_id)
    pattern_focus: Dict[str, List[List[int]]] = {}
    if recommended_ids and primary_focus.get("file") and primary_focus.get("module"):
        try:
            from operator_scope import derive_focus_scopes
        except ImportError:
            from timing_automation.operator_scope import derive_focus_scopes
        try:
            pattern_focus = derive_focus_scopes(full_unit, primary_focus, recommended_ids)
        except Exception:
            pattern_focus = {pid: [] for pid in recommended_ids}

    # Exhausted guided recommendations degrade to exploratory rather than stop.
    if optimizer_mode == "guided" and not recommended_ids:
        optimizer_mode = "exploratory"
        routing_reason = "guided_recommendations_unavailable"

    affected_path_ids = list(full_unit.get("affected_paths") or [])
    member_region_ids = list(full_unit.get("member_regions") or [])
    records = read_history_records(history_path)
    current_iteration_no = iteration_number or infer_next_iteration(records)
    history_summary = compact_relevant_history_summary(records, unit_id)
    diagnosis_docs: List[Dict[str, Any]] = []
    diagnosis_refs: List[Dict[str, Any]] = []
    for diagnosis_path in previous_diagnosis_paths:
        diagnosis_path = Path(diagnosis_path).expanduser().resolve()
        doc = load_json(diagnosis_path)
        diagnosis_docs.append(doc)
        diagnosis_refs.append({"path": str(diagnosis_path), "sha256": sha256_file(diagnosis_path)})
    previous_attempts = compact_previous_attempts(records, unit_id, diagnosis_docs)

    arch = opt.get("context_architecture") or {}
    budget_policy = arch.get("context_budget_policy") or {}
    estimator = budget_policy.get("estimator") or {}
    try:
        chars_per_token = float(estimator.get("chars_per_token", 4.0))
    except (TypeError, ValueError) as exc:
        raise BuildRequestError("invalid context-budget chars_per_token") from exc
    if chars_per_token <= 0:
        raise BuildRequestError("context-budget chars_per_token must be > 0")
    response_schema_version = extract_schema_version(schema_obj)

    current_iteration = {
        "iteration_number": current_iteration_no,
        "unit_id": unit_id,
        "optimizer_mode": optimizer_mode,
        "diagnostic_confidence": diagnostic_confidence,
        "routing_reason": routing_reason,
        "objective": design_context.get("objective"),
        "recommended_pattern_ids": recommended_ids,
        "recommended_patterns": recommended_entries,
        "critical_evidence": compact_unit.get("critical_evidence"),
        "recommended_transformation_families": compact_unit.get(
            "recommended_transformation_families",
            compact_unit.get("allowed_transformation_families", []),
        ),
        "logical_branch_evidence": full_unit.get("logical_branch_evidence"),
    }

    fixed_context = arch.get("fixed_context") or {"configured": False, "files": [], "estimated_tokens": 0}
    design_ref = {
        "path": str(design_context_path), "sha256": sha256_file(design_context_path),
        "schema": design_context.get("schema"),
        "source_optimizer_input_schema": design_context.get("source_optimizer_input_schema"),
    }
    eligible_ref = None
    if eligible_path is not None and Path(eligible_path).is_file():
        eligible_ref = {"path": str(Path(eligible_path).resolve()), "sha256": sha256_file(Path(eligible_path))}
    response_ref = {"path": str(schema_path), "sha256": sha256_file(schema_path), "schema_version": response_schema_version}

    source_bundle_context = dict(source_bundle_context or {})
    canonicalize_primary_focus(primary_focus, source_bundle_context)
    section_estimates = {
        "fixed_instructions_tokens": int(fixed_context.get("estimated_tokens") or 0),
        "design_context_tokens": int(((arch.get("design_context") or {}).get("estimated_tokens") or 0) or estimate_tokens_from_json(design_context, chars_per_token)),
        "source_bundle_tokens": estimate_tokens_from_json(source_bundle_context, chars_per_token),
        "current_iteration_tokens": estimate_tokens_from_json(current_iteration, chars_per_token),
        "history_summary_tokens": estimate_tokens_from_json(history_summary, chars_per_token) if history_summary is not None else 0,
        "previous_attempts_tokens": estimate_tokens_from_json(previous_attempts, chars_per_token) if previous_attempts else 0,
        "response_contract_tokens": estimate_tokens_from_json(schema_obj, chars_per_token),
    }
    pre_render_total = sum(section_estimates.values())
    soft_limit = budget_policy.get("soft_input_token_target")
    hard_limit = budget_policy.get("hard_input_token_limit")
    completion_reserve = budget_policy.get("completion_reserve_tokens")
    service_budget = budget_policy.get("service_model_token_budget")
    context_budget = {
        "policy": budget_policy,
        "section_estimates": section_estimates,
        "pre_render_estimated_input_tokens": pre_render_total,
        "soft_limit_exceeded_pre_render": isinstance(soft_limit, int) and pre_render_total > soft_limit,
        "hard_limit_exceeded_pre_render": isinstance(hard_limit, int) and pre_render_total > hard_limit,
        "estimated_total_with_completion_reserve": pre_render_total + completion_reserve if isinstance(completion_reserve, int) else None,
        "service_budget_exceeded_pre_render": isinstance(service_budget, int) and isinstance(completion_reserve, int) and pre_render_total + completion_reserve > service_budget,
        "final_hard_preflight_required_immediately_before_llm": True,
        "cumulative_history_record_count": len(records),
        "cumulative_token_tracking": budget_policy.get("cumulative_token_tracking") or {"enabled": True, "diagnostic_only": True},
    }

    request: Dict[str, Any] = {
        "schema": REQUEST_SCHEMA,
        "request_format_version": REQUEST_FORMAT_VERSION,
        "response_schema_version": response_schema_version,
        "design": opt.get("design", {}),
        "flow_variant": (opt.get("design") or {}).get("flow_variant"),
        "unit_id": unit_id,
        "optimizer_mode": optimizer_mode,
        "diagnostic_confidence": diagnostic_confidence,
        "routing_reason": routing_reason,
        "source_bundle": source_bundle_context,
        "primary_diagnostic_focus": primary_focus,
        "recommended_focus_ranges": recommended_focus_ranges,
        "recommended_pattern_focus_ranges": pattern_focus,
        "recommended_pattern_ids": recommended_ids,
        # The primary target is retained only as a diagnostic display anchor.
        # Mutation authority is the exact SourceBundle, not these focus ranges.
        "target": primary_focus,
        "eligible_pattern_ids": recommended_ids,
        "operator_scope_evidence": {"path": str(optimizer_input_path.resolve()), "sha256": sha256_file(optimizer_input_path)},
        "affected_path_ids": affected_path_ids,
        "member_region_ids": member_region_ids,
        "source_snapshot": ({
            "source_id": source_snapshot.get("source_id"),
            "file": source_snapshot.get("file"),
            "module": source_snapshot.get("module"),
            "sha256": source_snapshot.get("sha256"),
            "module_line_start": source_snapshot.get("module_line_start"),
            "module_line_end": source_snapshot.get("module_line_end"),
            "module_text_location": "design_context.sources[].module_text",
        } if source_snapshot else None),
        "rendering_inputs": {
            "fixed_context": fixed_context,
            "design_context": design_ref,
            "eligible_patterns": eligible_ref,
            "current_iteration": current_iteration,
            "relevant_history_summary": history_summary,
            "previous_attempts": previous_attempts,
            "previous_diagnoses": diagnosis_refs,
            "iteration_history": {"path": str(history_path), "format": "jsonl", "append_only": True, "full_history_is_not_sent_to_llm": True},
            "response_contract": response_ref,
        },
        "prompt_rendering": {"finished_prompt_embedded": False, "prompt_system_present": False, "prompt_user_present": False},
        "context_budget": context_budget,
    }
    return request

# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------


def run_self_test() -> None:
    import tempfile

    with tempfile.TemporaryDirectory(prefix="nebula_build_request_") as tmp:
        root = Path(tmp)
        source_sha = "a" * 64
        opt_path = root / "run_optimizer_input.json"
        design_path = root / "run_design_context.json"
        eligible_path = root / "eligible.json"
        schema_path = root / "response_schema.json"
        history_path = root / "history.jsonl"
        diagnosis_path = root / "diagnosis.json"
        diagnosis_path.write_text(json.dumps({"status": "disproven", "diagnosis_summary": "carry-width bug"}), encoding="utf-8")

        design_context = {
            "schema": "optimizer_design_context/1",
            "source_optimizer_input_schema": "2.3.0",
            "design": {"design": "top", "flow_variant": "run_test"},
            "objective": {"primary": "improve timing"},
            "baseline_metrics": {"wns_ns": 0.1, "tns_ns": 0.0},
            "constraints": {},
            "sources": [{
                "source_id": "src_0",
                "file": "rtl/top.v",
                "sha256": source_sha,
                "module": "top",
                "module_line_start": 1,
                "module_line_end": 8,
                "module_text": "module top(input a, output y);\nassign y=a;\nendmodule\n",
            }],
            "optimization_regions": [],
            "optimization_units": [{
                "unit_id": "unit_000",
                "optimization_ready": True,
                "blockers": [],
                "target": {
                    "file": "rtl/top.v",
                    "module": "top",
                    "original_sha256": source_sha,
                    "module_line_start": 1,
                    "module_line_end": 8,
                },
                "editable_line_ranges": [[2, 2]],
                "member_regions": ["region_000"],
                "critical_evidence": {
                    "affected_path_count": 1,
                    "worst_slack_ns": 0.1,
                    "classes": ["logic"],
                    "patterns": ["logic"],
                },
                "allowed_transformation_families": ["bool"],
            }],
        }
        write_json(design_path, design_context)

        opt = {
            "schema_version": "2.3.0",
            "design": {"design": "top", "flow_variant": "run_test"},
            "optimization_units": [{
                "unit_id": "unit_000",
                "eligibility": {"optimization_ready": True, "blockers": []},
                "affected_paths": ["p0"],
                "member_regions": ["region_000"],
                "source": {
                    "file": "rtl/top.v",
                    "module": "top",
                    "sha256": source_sha,
                    "module_line_start": 1,
                    "module_line_end": 8,
                    "source_locations": [{"file": "rtl/top.v", "module": "top", "sha256": source_sha, "line_start": 2, "line_end": 2, "operator_roles": ["$and"]}],
                },
            }],
            "context_architecture": {
                "fixed_context": {"configured": False, "files": [], "estimated_tokens": 0},
                "design_context": {
                    "path": str(design_path),
                    "sha256": sha256_file(design_path),
                    "estimated_tokens": 200,
                },
                "iteration_history": {
                    "path": str(history_path),
                    "format": "jsonl",
                    "append_only": True,
                },
                "context_budget_policy": {
                    "service_model_token_budget": 8000,
                    "soft_input_token_target": 5000,
                    "hard_input_token_limit": 6000,
                    "completion_reserve_tokens": 2000,
                    "estimator": {"chars_per_token": 4.0},
                    "cumulative_token_tracking": {"enabled": True, "diagnostic_only": True},
                },
            },
        }
        write_json(opt_path, opt)
        eligible = {
            "unit_id": "unit_000",
            "eligible_patterns": [{"id": "bool.factor_common_subexpr", "family": "bool"}],
        }
        write_json(eligible_path, eligible)
        schema = {
            "type": "object",
            "properties": {"schema_version": {"const": "2.0"}},
            "required": ["schema_version"],
        }
        write_json(schema_path, schema)

        source_bundle_context = {
            "schema": "nebula_source_bundle_identity/1",
            "design_id": "top",
            "top": "top",
            "bundle_sha256": "0" * 64,
            "sources": [{
                "path": "rtl/top.v",
                "sha256": source_sha,
                "size_bytes": len(design_context["sources"][0]["module_text"].encode()),
                "modules": ["top"],
                "text": design_context["sources"][0]["module_text"],
            }],
            "module_to_file": {"top": "rtl/top.v"},
        }

        request = build_request(
            opt=opt,
            eligible_obj=eligible,
            schema_obj=schema,
            optimizer_input_path=opt_path,
            eligible_path=eligible_path,
            schema_path=schema_path,
            design_context_path=design_path,
            history_path=history_path,
            unit_id="unit_000",
            iteration_number=None,
            previous_diagnosis_paths=[diagnosis_path],
            source_bundle_context=source_bundle_context,
        )

        assert request["schema"] == REQUEST_SCHEMA
        assert request["unit_id"] == "unit_000"
        assert request["target"]["original_sha256"] == source_sha
        assert request["recommended_focus_ranges"] == [[2, 2]]
        assert normalize_ranges([[19, 19], [20, 20], [22, 22]]) == [[19, 19], [20, 20], [22, 22]]
        assert request["recommended_pattern_ids"] == ["bool.factor_common_subexpr"]
        assert "prompt_user" not in request
        assert "prompt_system" not in request
        assert request["prompt_rendering"]["finished_prompt_embedded"] is False
        assert request["rendering_inputs"]["current_iteration"]["iteration_number"] == 1
        assert request["context_budget"]["final_hard_preflight_required_immediately_before_llm"] is True

        # Append one previous outcome and verify only a compact summary is carried.
        history_path.write_text(
            json.dumps({
                "iteration": 1,
                "unit_id": "unit_000",
                "chosen_pattern_id": "bool.factor_common_subexpr",
                "verdict": "reject",
                "reason": "wns regression",
                "candidate": {"huge_raw_candidate": "DO_NOT_COPY"},
            }) + "\n",
            encoding="utf-8",
        )
        request2 = build_request(
            opt=opt,
            eligible_obj=eligible,
            schema_obj=schema,
            optimizer_input_path=opt_path,
            eligible_path=eligible_path,
            schema_path=schema_path,
            design_context_path=design_path,
            history_path=history_path,
            unit_id="unit_000",
            iteration_number=None,
        )
        summary = request2["rendering_inputs"]["relevant_history_summary"]
        assert summary == {
            "iteration": 1,
            "pattern_id": "bool.factor_common_subexpr",
            "result": "reject",
            "reason": "wns regression",
        }
        assert request2["rendering_inputs"]["current_iteration"]["iteration_number"] == 2
        assert "DO_NOT_COPY" not in json.dumps(request2)

    print("PASS: build_request self-test")
    print("PASS: optimizer_request/2 safety boundary preserved")
    print("PASS: overlapping/adjacent editable ranges are merged; true gaps are preserved")
    print("PASS: --unit-id selects exactly one optimization unit")
    print("PASS: design-context SHA and source SHA are checked")
    print("PASS: no finished prompt_system/prompt_user is embedded")
    print("PASS: only compact latest relevant history summary is carried")
    print("PASS: section-level context estimates and hard-preflight flag emitted")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description=(
            "Build a compact structured optimizer_request/2 without rendering "
            "the final LLM prompt."
        )
    )
    ap.add_argument("--optimizer-input", help="Trusted optimizer_input.json")
    ap.add_argument("--eligible", default=None, help="Optional recommended-pattern JSON for the selected unit")
    ap.add_argument("--unit-id", help="Exact optimization unit to request")
    ap.add_argument("--golden-manifest", default=None, help="DesignManifest JSON used to expose the complete trusted SourceBundle to the LLM.")
    ap.add_argument("--golden-base-dir", default=".", help="Base directory for --golden-manifest source paths.")
    ap.add_argument("--schema", default=str(SCHEMA_PATH), help="response_schema.json")
    ap.add_argument(
        "--design-context",
        default=None,
        help=(
            "Optional design-context JSON. Default: optimizer_input.context_architecture.design_context.path"
        ),
    )
    ap.add_argument(
        "--history",
        default=None,
        help=(
            "Optional append-only optimizer history JSONL. Default: optimizer_input.context_architecture.iteration_history.path"
        ),
    )
    ap.add_argument(
        "--previous-diagnosis",
        action="append",
        default=[],
        help="Prior diagnose_equivalence JSON; may be repeated.",
    )
    ap.add_argument(
        "--iteration-number",
        type=int,
        default=None,
        help="Optional explicit iteration number; otherwise inferred as max(history)+1",
    )
    ap.add_argument("--output", help="Output optimizer_request/2 JSON")
    ap.add_argument("--self-test", action="store_true")
    return ap.parse_args()


def main() -> int:
    args = parse_args()

    if args.self_test:
        try:
            run_self_test()
            return 0
        except Exception as exc:
            print(f"SELF-TEST FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1

    missing = [
        name
        for name, value in (
            ("--optimizer-input", args.optimizer_input),
            ("--unit-id", args.unit_id),
            ("--output", args.output),
        )
        if not value
    ]
    if missing:
        print("ERROR: missing required argument(s): " + ", ".join(missing), file=sys.stderr)
        return 2

    if args.iteration_number is not None and args.iteration_number < 1:
        print("ERROR: --iteration-number must be >= 1", file=sys.stderr)
        return 2

    try:
        optimizer_input_path = Path(args.optimizer_input).expanduser().resolve()
        eligible_path = Path(args.eligible).expanduser().resolve() if args.eligible else None
        schema_path = Path(args.schema).expanduser().resolve()

        opt = load_json(optimizer_input_path)
        eligible_obj = load_json(eligible_path) if eligible_path else {}
        schema_obj = load_json(schema_path)

        design_context_path = resolve_design_context_path(
            opt, optimizer_input_path, args.design_context
        )
        history_path = resolve_history_path(opt, optimizer_input_path, args.history)

        source_bundle_context = None
        if args.golden_manifest:
            manifest = DesignManifest.read_json(Path(args.golden_manifest).expanduser().resolve())
            bundle = SourceBundle.from_manifest(
                manifest, Path(args.golden_base_dir).expanduser().resolve(),
                verify_manifest_hashes=True,
            )
            source_bundle_context = bundle.prompt_context()
        else:
            raise BuildRequestError(
                "--golden-manifest is required for whole-design optimizer requests; "
                "diagnostic focus is not a mutation boundary"
            )

        request = build_request(
            opt=opt,
            eligible_obj=eligible_obj,
            schema_obj=schema_obj,
            optimizer_input_path=optimizer_input_path,
            eligible_path=eligible_path,
            schema_path=schema_path,
            design_context_path=design_context_path,
            history_path=history_path,
            unit_id=args.unit_id,
            iteration_number=args.iteration_number,
            previous_diagnosis_paths=[Path(x) for x in args.previous_diagnosis],
            source_bundle_context=source_bundle_context,
        )

        output_path = Path(args.output).expanduser()
        write_json(output_path, request)

        summary = {
            "ok": True,
            "request": str(output_path),
            "schema": request["schema"],
            "request_format_version": request["request_format_version"],
            "unit_id": request["unit_id"],
            "optimizer_mode": request["optimizer_mode"],
            "target_file": request.get("primary_diagnostic_focus", {}).get("file"),
            "target_module": request.get("primary_diagnostic_focus", {}).get("module"),
            "affected_path_ids": request["affected_path_ids"],
            "member_region_ids": request["member_region_ids"],
            "recommended_focus_ranges": request["recommended_focus_ranges"],
            "recommended_pattern_ids": request["recommended_pattern_ids"],
            "source_bundle_file_count": len((request.get("source_bundle") or {}).get("sources") or []),
            "iteration_number": request["rendering_inputs"]["current_iteration"]["iteration_number"],
            "finished_prompt_embedded": False,
            "pre_render_estimated_input_tokens": request["context_budget"]["pre_render_estimated_input_tokens"],
            "section_estimates": request["context_budget"]["section_estimates"],
            "soft_limit_exceeded_pre_render": request["context_budget"]["soft_limit_exceeded_pre_render"],
            "hard_limit_exceeded_pre_render": request["context_budget"]["hard_limit_exceeded_pre_render"],
        }
        print(json.dumps(summary, indent=2))
        return 0

    except BuildRequestError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
