#!/usr/bin/env python3
"""nEBULA prompt rendering helpers.

The latency-aware runner exposes three explicit model contexts:
  * GOLDEN RTL: immutable correctness reference
  * CURRENT BEST RTL: best promoted PPA reference and preferred editing base
  * PREVIOUS ATTEMPTS: compact backend-generated outcome history only

The model may choose one editing base for a candidate, but backend checks remain
authoritative for identity, duplicates, formal correctness, synthesis equivalence,
convergence and PPA promotion.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence


def render_groq_prompt(request, schema, feedback=None, max_guidance_chars=6000):
    from guidance_packet import compact_packet

    packet, budget = compact_packet(request.get("guidance") or {
        "schema": "guidance_packet/1",
        "mode_used": "exploratory",
        "groups": [],
        "recommendations": [],
        "prior_attempt_outcomes": [],
    }, max_guidance_chars)

    mode = packet["mode_used"]
    if mode == "exploratory":
        packet["groups"], packet["recommendations"] = [], []
    elif mode == "guided_evidence_only":
        packet["recommendations"] = []

    # Prior attempts are already compact backend summaries. Trim oldest entries
    # if advisory context exceeds the configured guidance/history budget.
    history = list(request.get("previous_attempts") or [])
    latest_feedback = None
    if feedback:
        latest_feedback = {
            key: feedback.get(key)
            for key in (
                "outcome_type", "instruction", "rejection_reason",
                "synthesis_implementation", "duplicate_of_attempt",
                "verified_latency_change_cycles", "predicted_latency_change_cycles",
            )
            if feedback.get(key) is not None
        }
        ppa = feedback.get("ppa_deltas") if isinstance(feedback.get("ppa_deltas"), Mapping) else {}
        compact_ppa = {
            key: ppa.get(key)
            for key in ("setup_slack_delta_ns", "hold_slack_delta_ns", "area_delta_pct", "power_delta_pct")
            if ppa.get(key) is not None
        }
        if compact_ppa:
            latest_feedback["ppa_deltas"] = compact_ppa
        latency = feedback.get("latency_result") if isinstance(feedback.get("latency_result"), Mapping) else {}
        compact_latency = {
            key: latency.get(key)
            for key in (
                "status", "verified_shift_cycles", "iteration_latency_delta_cycles",
                "verified_total_latency_regression_cycles", "max_latency_regression_cycles",
            )
            if latency.get(key) is not None
        }
        if compact_latency:
            latest_feedback["latency_result"] = compact_latency

    def advisory_chars() -> int:
        return len(json.dumps({
            "guidance": packet,
            "previous_attempts": history,
            "search_summary": request.get("search_summary") or {},
            "latest_feedback": latest_feedback,
        }, sort_keys=True))

    while history and advisory_chars() > max_guidance_chars:
        history.pop(0)
    if latest_feedback and advisory_chars() > max_guidance_chars:
        latest_feedback = None

    system = (
        "You are the nEBULA RTL optimization proposal generator. Return exactly one JSON object "
        "matching the supplied response schema and no other text. Propose one real executable RTL "
        "optimization, or return the advisory no_further_optimization decision when you cannot identify "
        "another promising unexplored implementation. Comments, whitespace, formatting-only edits, or "
        "returning an existing design unchanged are not optimizations.\n\n"
        "GOLDEN RTL is the immutable original correctness reference. Formal verification always compares "
        "the materialized candidate against GOLDEN regardless of which editing base you choose.\n\n"
        "CURRENT BEST RTL is the best promoted implementation so far and is the physical PPA comparison "
        "reference. Prefer CURRENT BEST for incremental improvement; choose GOLDEN when attempting a "
        "materially different architecture that should not inherit prior accepted restructuring.\n\n"
        "For a candidate, choose exactly one editing_base: golden or current_best. Use only that complete "
        "source bundle to construct every returned source file. Do not mix bases. The backend owns editing-base "
        "identities and SHA-256 values; do not copy bundle hashes, design identities, or per-file hashes into "
        "the response. In this complete-source format, return exactly the source-file set present in the "
        "selected editing base; do not introduce or omit source files. If a helper module is useful, define it in an "
        "existing returned .v file. Preserve the top-level interface and parameters. Latency is a limited optimization "
        "resource: prefer latency-neutral transformations when they are expected to provide comparable benefit, but you "
        "may spend available latency whenever you judge the expected timing/PPA benefit justifies it. Pipeline insertion "
        "is available from the first attempt when headroom exists; there is no backend transform-order gate. When moving "
        "or inserting a pipeline boundary, preserve transaction-cycle alignment for every downstream dependency: sibling "
        "data/bypass paths, mux/select controls, enables, and other control or data signals crossing that boundary must be "
        "delayed consistently. Do not pipeline only one branch while related downstream inputs remain from a different "
        "cycle. Added or retimed state must use an existing permitted clock/reset domain; do not introduce clocks, clock domains, latches, "
        "or change the external clock/reset contract. predicted_latency_change_cycles is advisory; backend proof is "
        "authoritative. Describe intent with primary_transformation plus concrete proposed_transformations; do not use a "
        "vague mixed label. Bottleneck locations "
        "and target_unit_id are diagnostic evidence only and are never edit boundaries.\n\n"
        "Do not return GOLDEN unchanged. Do not return CURRENT BEST unchanged. Do not reproduce any prior "
        "candidate unchanged. Avoid intentionally reproducing implementation classes that backend synthesis "
        "has already marked explored/rejected. Seek a materially different structural implementation. Keep the "
        "returned RTL compact: do not expand arithmetic or simple logic into exhaustive lookup/case tables or "
        "other source-code blowups merely to force structural novelty.\n\n"
        "A no_further_optimization response is advisory only. nEBULA independently decides whether backend "
        "convergence evidence is sufficient to stop. Model self-checks and PPA predictions are advisory; "
        "backend identity, structural checks, formal equivalence, normalized synthesis hashing, convergence, "
        "and PPA promotion are authoritative."
    )

    notes = {
        "guided": "Use the current-best mapped timing evidence and structural classification as advisory guidance.",
        "guided_evidence_only": "Use the current-best mapped timing evidence; no reliable transformation recommendation is available.",
        "exploratory": "Choose a materially different equivalent rewrite from the complete editing-base source; no reliable source-location guidance is supplied.",
    }
    system += "\n\n" + notes[mode]

    golden = request["golden"]
    current_best = request["current_best"]
    payload = {
        "task": "Improve the RTL implementation for area, power, or timing under the active backend acceptance policy.",
        "optimizer_mode": mode,
        "target_unit_id": request["unit_id"],
        "top": golden["source_bundle"]["top"],
        "golden": golden,
        "current_best": current_best,
        "editing_base_policy": request.get("editing_base_policy", {}),
        "candidate_contract": request["candidate_contract"],
        "user_ppa_budgets": request.get("user_ppa_budgets", {}),
        "latency_policy": request.get("latency_policy", {
            "reference": "frozen_golden",
            "max_latency_regression_cycles": 0,
            "current_best_total_latency_regression_cycles": 0,
            "remaining_latency_headroom_cycles": 0,
            "model_prediction_authority": "advisory_only",
        }),
        "design_capabilities": request.get("design_capabilities", {}),
        "current_best_evidence": {
            key: value
            for key, value in request.get("current_best_evidence", {}).items()
            if key != "worst_setup_path_excerpt"
        },
        "guidance": packet,
        "previous_attempts": history,
        "search_summary": request.get("search_summary") or {},
        "response_schema": schema,
    }
    if latest_feedback:
        payload["latest_feedback"] = latest_feedback

    budget["rendered_chars"] = advisory_chars()
    user = json.dumps(payload, sort_keys=True)
    budget["total_prompt_chars"] = len(system) + len(user)
    # Section-level character accounting makes future token optimization
    # evidence-driven without changing provider tokenization assumptions.
    budget["section_chars"] = {
        "system": len(system),
        "golden": len(json.dumps(golden, sort_keys=True)),
        "current_best": len(json.dumps(current_best, sort_keys=True)),
        "guidance": len(json.dumps(packet, sort_keys=True)),
        "history": len(json.dumps(history, sort_keys=True)),
        "search_summary": len(json.dumps(request.get("search_summary") or {}, sort_keys=True)),
        "response_schema": len(json.dumps(schema, sort_keys=True)),
        "latency_policy": len(json.dumps(payload.get("latency_policy") or {}, sort_keys=True)),
        "design_capabilities": len(json.dumps(payload.get("design_capabilities") or {}, sort_keys=True)),
    }
    budget["cap_scope"] = "guidance + compact previous-attempt history; Golden/Current-Best source contracts are protected"
    budget["history_entries_sent"] = len(history)
    budget["search_summary_chars"] = len(json.dumps(request.get("search_summary") or {}, sort_keys=True))
    return system, user, packet, budget


class PromptRenderError(RuntimeError):
    pass


class ContextBudgetError(PromptRenderError):
    pass


def load_json(path: Path) -> Dict[str, Any]:
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise PromptRenderError(f"JSON file not found: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PromptRenderError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PromptRenderError(f"JSON root must be an object: {path}")
    return value


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def compact_json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, sort_keys=True)


def estimate_tokens(text: str, chars_per_token: float) -> int:
    if chars_per_token <= 0:
        raise PromptRenderError("chars_per_token must be > 0")
    return int(math.ceil(len(text) / chars_per_token)) if text else 0


def verify_ref(path: Path, ref: Mapping[str, Any], label: str) -> None:
    expected = ref.get("sha256")
    if isinstance(expected, str) and expected:
        actual = sha256_file(path)
        if actual.lower() != expected.lower():
            raise PromptRenderError(
                f"{label} SHA mismatch: expected={expected}, actual={actual}, path={path}"
            )


def load_fixed_context(request: Mapping[str, Any]) -> List[Dict[str, Any]]:
    rendering = request.get("rendering_inputs") or {}
    fixed = rendering.get("fixed_context") or {}
    files = fixed.get("files") or []
    if not isinstance(files, list):
        raise PromptRenderError("rendering_inputs.fixed_context.files must be an array")

    values: List[Dict[str, Any]] = []
    for index, ref in enumerate(files):
        if not isinstance(ref, Mapping):
            raise PromptRenderError(f"fixed-context reference {index} is invalid")
        raw_path = ref.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            raise PromptRenderError(f"fixed-context reference {index} has no path")
        path = Path(raw_path).expanduser().resolve()
        value = load_json(path)
        verify_ref(path, ref, f"fixed-context[{index}]")
        values.append(value)
    return values


def select_one(items: Any, *, key: str, value: Any, label: str) -> Dict[str, Any]:
    matches = [x for x in (items or []) if isinstance(x, dict) and x.get(key) == value]
    if len(matches) != 1:
        raise PromptRenderError(
            f"expected exactly one {label} with {key}={value!r}; found {len(matches)}"
        )
    return matches[0]


def build_design_view(request: Mapping[str, Any], design_context: Mapping[str, Any],
                      guidance=None, user_budgets=None) -> Dict[str, Any]:
    unit_id = request.get("unit_id")
    unit = select_one(
        design_context.get("optimization_units"), key="unit_id", value=unit_id,
        label="optimization unit",
    )
    regions = [
        r for r in (design_context.get("optimization_regions") or [])
        if isinstance(r, dict) and r.get("optimization_unit_id") == unit_id
    ]
    bundle = request.get("source_bundle") or {}
    sources = bundle.get("sources") or []
    if not isinstance(sources, list) or not sources:
        # Compatibility fallback for older requests. New v2.2 requests should
        # always carry the complete SourceBundle prompt snapshot.
        sources = design_context.get("sources") or []
    active_guidance = guidance if guidance is not None else request.get("guidance")
    optimizer_mode = (active_guidance or {}).get("mode_used") or request.get("optimizer_mode") or "guided"
    if active_guidance and optimizer_mode == "exploratory":
        active_guidance = {**active_guidance, "groups": [], "recommendations": []}
    focus = request.get("primary_diagnostic_focus") or request.get("target") or {}
    use_legacy_diagnosis = optimizer_mode != "exploratory" and active_guidance is None
    return {
        "design": design_context.get("design"),
        "baseline_metrics": design_context.get("baseline_metrics"),
        "constraints": design_context.get("constraints"),
        "optimizer_mode": optimizer_mode,
        "guidance": active_guidance,
        "user_ppa_budgets": user_budgets if user_budgets is not None else request.get("user_ppa_budgets", {}),
        "diagnostic_confidence": request.get("diagnostic_confidence"),
        "routing_reason": request.get("routing_reason"),
        "source_bundle": {
            "schema": bundle.get("schema"),
            "design_id": bundle.get("design_id"),
            "top": bundle.get("top"),
            "bundle_sha256": bundle.get("bundle_sha256"),
            "module_to_file": bundle.get("module_to_file"),
            "sources": sources,
            "mutation_rule": (
                "Any file/module in this exact SourceBundle may be changed if source "
                "identity, top-level interface, design validity, and formal-equivalence "
                "gates pass. Helper modules, hierarchy, and sequential state may change. Diagnostic focus "
                "locations and patterns are recommendations, not edit restrictions."
            ),
        },
        "diagnostic_focus": {
            "primary": focus if use_legacy_diagnosis else {},
            "recommended_focus_ranges": request.get("recommended_focus_ranges", []) if use_legacy_diagnosis else [],
            "recommended_pattern_focus_ranges": request.get("recommended_pattern_focus_ranges", {}) if use_legacy_diagnosis else {},
            "recommended_pattern_ids": request.get("recommended_pattern_ids", []) if use_legacy_diagnosis else [],
            "advisory_only": True,
        },
        "critical_evidence": unit.get("critical_evidence") if use_legacy_diagnosis else None,
        "diagnostic_regions": regions if use_legacy_diagnosis else [],
    }

def compact_design_view_for_soft_limit(view: Mapping[str, Any]) -> Dict[str, Any]:
    """Compact optional diagnostics while preserving the whole SourceBundle."""
    compact_regions: List[Dict[str, Any]] = []
    for raw in view.get("diagnostic_regions") or []:
        if not isinstance(raw, Mapping):
            continue
        compact_regions.append({
            "region_id": raw.get("region_id"),
            "affected_path_count": raw.get("affected_path_count"),
            "timing": raw.get("timing"),
            "structure": raw.get("structure"),
        })
    return {
        "design": view.get("design"),
        "baseline_metrics": view.get("baseline_metrics"),
        "constraints": view.get("constraints"),
        "optimizer_mode": view.get("optimizer_mode"),
        "diagnostic_confidence": view.get("diagnostic_confidence"),
        "routing_reason": view.get("routing_reason"),
        "source_bundle": view.get("source_bundle"),
        "diagnostic_focus": view.get("diagnostic_focus"),
        "critical_evidence": view.get("critical_evidence"),
        "diagnostic_regions": compact_regions,
    }

def read_history_records(path: Path) -> List[Dict[str, Any]]:
    if not path.is_file():
        return []
    records: List[Dict[str, Any]] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise PromptRenderError(f"invalid JSONL in {path}:{lineno}: {exc}") from exc
        if isinstance(value, dict):
            records.append(value)
    return records


def prior_cumulative_input_tokens(request: Mapping[str, Any]) -> int:
    rendering = request.get("rendering_inputs") or {}
    hist = rendering.get("iteration_history") or {}
    raw_path = hist.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        return 0
    total = 0
    for record in read_history_records(Path(raw_path).expanduser().resolve()):
        usage = record.get("context_usage") or {}
        value = usage.get("estimated_input_tokens")
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            total += value
    return total


def compact_previous_attempts_for_budget(values: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Last-resort compaction: retain the newest failures and their actionable diagnosis."""
    out: List[Dict[str, Any]] = []
    for raw in list(values)[-3:]:
        item: Dict[str, Any] = {}
        for key in ("iteration", "chosen_pattern_id", "verdict", "failure", "reason", "diagnosis", "counterexample"):
            value = raw.get(key)
            if value is None:
                continue
            if isinstance(value, str) and len(value) > 600:
                value = value[:597] + "..."
            item[key] = value
        out.append(item)
    return out


def _load_external_diagnoses(paths: Sequence[Path]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for raw in paths:
        path = Path(raw).expanduser().resolve()
        payload = load_json(path)
        summary = None
        for key in ("summary", "diagnosis", "reason", "counterexample_summary"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                summary = value.strip()
                break
        out.append({"source": str(path), "summary": summary, "payload": payload if summary is None else None})
    return out


def section_text(title: str, value: Any) -> str:
    return f"{title}\n{compact_json(value)}"


@dataclass(frozen=True)
class RenderedPrompt:
    system_prompt: str
    user_prompt: str
    context_budget: Dict[str, Any]
    compaction_applied: bool

    def to_metadata(self) -> Dict[str, Any]:
        return {
            "schema": "rendered_prompt_metadata/1",
            "context_budget": self.context_budget,
            "compaction_applied": self.compaction_applied,
            "finished_prompt_persisted": False,
        }


def _render_sections(
    *,
    fixed_values: Sequence[Mapping[str, Any]],
    design_view: Mapping[str, Any],
    current_iteration: Mapping[str, Any],
    history_summary: Optional[Mapping[str, Any]],
    previous_attempts: Sequence[Mapping[str, Any]],
    response_schema: Mapping[str, Any],
) -> Dict[str, str]:
    fixed_payload = {"fixed_policies": list(fixed_values)}
    mode = design_view.get("optimizer_mode") or "guided"
    mode_note = (
        "GUIDED mode: prioritize the supplied diagnosis and recommendations, but they are not edit boundaries."
        if mode == "guided"
        else "GUIDED_EVIDENCE_ONLY mode: use mapped evidence without assuming a transformation recommendation."
        if mode == "guided_evidence_only"
        else "EXPLORATORY mode: diagnosis is uncertain; reason over the complete SourceBundle and choose any safe equivalent rewrite."
    )
    system_text = (
        "You are the nEBULA RTL optimization proposal generator. "
        "Follow the fixed policies exactly. " + mode_note + "\n"
        + compact_json(fixed_payload)
    )
    if previous_attempts:
        system_text += (
            "\nPrior attempts below are authoritative feedback. Do not recreate a pattern or "
            "implementation that was rejected on a valid comparable result; use the concrete "
            "failure diagnosis to choose a materially different rewrite. Infrastructure/tool "
            "failures are not implementation rejections."
        )

    design_text = section_text("DESIGN_CONTEXT", design_view)
    iteration_text = section_text("CURRENT_ITERATION", current_iteration)
    history_text = (
        section_text("RELEVANT_PREVIOUS_RESULT", history_summary)
        if history_summary is not None else ""
    )
    previous_text = (
        section_text("PREVIOUS_ATTEMPTS", list(previous_attempts)) if previous_attempts else ""
    )
    contract_text = section_text("RESPONSE_CONTRACT", response_schema)

    return {
        "fixed_instructions": system_text,
        "design_context": design_text,
        "current_iteration": iteration_text,
        "history_summary": history_text,
        "previous_attempts": previous_text,
        "response_contract": contract_text,
    }


def _budget_from_sections(
    *,
    sections: Mapping[str, str],
    chars_per_token: float,
    policy: Mapping[str, Any],
    completion_reserve_tokens: int,
    cumulative_before: int,
    compaction_applied: bool,
) -> Dict[str, Any]:
    estimates = {k: estimate_tokens(v, chars_per_token) for k, v in sections.items()}
    input_tokens = sum(estimates.values())

    soft = policy.get("soft_input_token_target")
    hard = policy.get("hard_input_token_limit")
    total_budget = policy.get("service_model_token_budget")

    if not isinstance(soft, int) or not isinstance(hard, int) or not isinstance(total_budget, int):
        raise PromptRenderError("context-budget policy is missing integer soft/hard/total limits")
    if completion_reserve_tokens < 1:
        raise PromptRenderError("completion reserve must be >= 1")

    return {
        "estimator": {
            "method": "ceil(characters/chars_per_token)",
            "chars_per_token": chars_per_token,
        },
        "section_estimates": estimates,
        "estimated_input_tokens": input_tokens,
        "soft_input_token_target": soft,
        "hard_input_token_limit": hard,
        "completion_reserve_tokens": completion_reserve_tokens,
        "service_model_token_budget": total_budget,
        "estimated_total_with_completion_reserve": input_tokens + completion_reserve_tokens,
        "soft_limit_exceeded": input_tokens > soft,
        "hard_limit_exceeded": input_tokens > hard,
        "service_budget_exceeded": input_tokens + completion_reserve_tokens > total_budget,
        "compaction_applied": compaction_applied,
        "cumulative_prior_input_tokens": cumulative_before,
        "cumulative_input_tokens_including_current_estimate": cumulative_before + input_tokens,
        "cumulative_is_diagnostic_only": True,
    }


def render_request_for_llm(
    request_path: Path,
    *,
    response_schema_override: Optional[Path] = None,
    completion_reserve_tokens: Optional[int] = None,
    previous_diagnosis_paths: Sequence[Path] = (),
) -> RenderedPrompt:
    request_path = Path(request_path).expanduser().resolve()
    request = load_json(request_path)

    if request.get("schema") != "optimizer_request/2":
        raise PromptRenderError("request schema must be optimizer_request/2")
    if str(request.get("request_format_version", "")) < "2.1":
        raise PromptRenderError("request_format_version >= 2.1 is required")
    if request.get("prompt_rendering", {}).get("finished_prompt_embedded") is not False:
        raise PromptRenderError("request does not declare compact deferred prompt rendering")

    rendering = request.get("rendering_inputs") or {}

    design_ref = rendering.get("design_context") or {}
    design_path_raw = design_ref.get("path")
    if not isinstance(design_path_raw, str) or not design_path_raw:
        raise PromptRenderError("request has no design-context path")
    design_path = Path(design_path_raw).expanduser().resolve()
    design_context = load_json(design_path)
    verify_ref(design_path, design_ref, "design-context")

    response_ref = rendering.get("response_contract") or {}
    if response_schema_override is not None:
        schema_path = Path(response_schema_override).expanduser().resolve()
        verify_ref(schema_path, response_ref, "response-contract override")
    else:
        schema_raw = response_ref.get("path")
        if not isinstance(schema_raw, str) or not schema_raw:
            raise PromptRenderError("request has no response-contract path")
        schema_path = Path(schema_raw).expanduser().resolve()
        verify_ref(schema_path, response_ref, "response-contract")
    response_schema = load_json(schema_path)

    fixed_values = load_fixed_context(request)
    if not fixed_values:
        raise PromptRenderError(
            "no fixed optimizer context is configured; rerun build_optimizer_input.py "
            "with --fixed-context-dir before generating this request"
        )
    current_iteration = rendering.get("current_iteration") or {}
    if not isinstance(current_iteration, Mapping):
        raise PromptRenderError("current_iteration must be an object")
    history_summary = rendering.get("relevant_history_summary")
    if history_summary is not None and not isinstance(history_summary, Mapping):
        raise PromptRenderError("relevant_history_summary must be an object or null")
    raw_attempts = rendering.get("previous_attempts") or request.get("previous_attempts") or []
    if not isinstance(raw_attempts, list) or any(not isinstance(x, Mapping) for x in raw_attempts):
        raise PromptRenderError("previous_attempts must be an array of objects")
    previous_attempts: List[Mapping[str, Any]] = list(raw_attempts)
    previous_attempts.extend(_load_external_diagnoses(previous_diagnosis_paths))

    policy = request.get("context_budget", {}).get("policy") or {}
    estimator = policy.get("estimator") or {}
    try:
        chars_per_token = float(estimator.get("chars_per_token", 4.0))
    except (TypeError, ValueError) as exc:
        raise PromptRenderError("invalid chars_per_token") from exc

    configured_reserve = policy.get("completion_reserve_tokens")
    if completion_reserve_tokens is None:
        if not isinstance(configured_reserve, int):
            raise PromptRenderError("context-budget policy has no completion reserve")
        completion_reserve_tokens = configured_reserve
    elif isinstance(configured_reserve, int):
        # Never reserve less than the configured safety floor.
        completion_reserve_tokens = max(completion_reserve_tokens, configured_reserve)

    design_view = build_design_view(request, design_context)
    cumulative_before = prior_cumulative_input_tokens(request)

    sections = _render_sections(
        fixed_values=fixed_values,
        design_view=design_view,
        current_iteration=current_iteration,
        history_summary=history_summary,
        previous_attempts=previous_attempts,
        response_schema=response_schema,
    )
    budget = _budget_from_sections(
        sections=sections,
        chars_per_token=chars_per_token,
        policy=policy,
        completion_reserve_tokens=completion_reserve_tokens,
        cumulative_before=cumulative_before,
        compaction_applied=False,
    )

    compaction_applied = False
    if budget["soft_limit_exceeded"]:
        design_view = compact_design_view_for_soft_limit(design_view)
        sections = _render_sections(
            fixed_values=fixed_values,
            design_view=design_view,
            current_iteration=current_iteration,
            history_summary=history_summary,
            previous_attempts=previous_attempts,
            response_schema=response_schema,
        )
        compaction_applied = True
        budget = _budget_from_sections(
            sections=sections,
            chars_per_token=chars_per_token,
            policy=policy,
            completion_reserve_tokens=completion_reserve_tokens,
            cumulative_before=cumulative_before,
            compaction_applied=True,
        )

    if (budget["hard_limit_exceeded"] or budget["service_budget_exceeded"]) and previous_attempts:
        previous_attempts = compact_previous_attempts_for_budget(previous_attempts)
        sections = _render_sections(
            fixed_values=fixed_values,
            design_view=design_view,
            current_iteration=current_iteration,
            history_summary=history_summary,
            previous_attempts=previous_attempts,
            response_schema=response_schema,
        )
        compaction_applied = True
        budget = _budget_from_sections(
            sections=sections,
            chars_per_token=chars_per_token,
            policy=policy,
            completion_reserve_tokens=completion_reserve_tokens,
            cumulative_before=cumulative_before,
            compaction_applied=True,
        )

    if budget["hard_limit_exceeded"] or budget["service_budget_exceeded"]:
        raise ContextBudgetError(
            "ContextBudgetError: rendered request does not fit after deterministic compaction; "
            f"input={budget['estimated_input_tokens']} tokens, "
            f"completion_reserve={budget['completion_reserve_tokens']}, "
            f"hard_input_limit={budget['hard_input_token_limit']}, "
            f"service_budget={budget['service_model_token_budget']}"
        )

    user_parts = [
        sections["design_context"],
        sections["current_iteration"],
    ]
    if sections["history_summary"]:
        user_parts.append(sections["history_summary"])
    if sections["previous_attempts"]:
        user_parts.append(sections["previous_attempts"])
    user_parts.append(sections["response_contract"])

    return RenderedPrompt(
        system_prompt=sections["fixed_instructions"],
        user_prompt="\n\n".join(user_parts),
        context_budget=budget,
        compaction_applied=compaction_applied,
    )


def run_self_test() -> None:
    import tempfile

    with tempfile.TemporaryDirectory(prefix="nebula_prompt_renderer_") as tmp:
        root = Path(tmp)
        fixed = root / "fixed.json"
        design = root / "design.json"
        schema = root / "schema.json"
        hist = root / "history.jsonl"
        request = root / "request.json"

        fixed.write_text(json.dumps({"schema": "fixed/1", "rule": "return JSON"}), encoding="utf-8")
        design_obj = {
            "schema": "optimizer_design_context/1",
            "design": {"design": "d", "platform": "p"},
            "objective": {"primary": "wns"},
            "baseline_metrics": {"metrics": {"wns_ns": 0.1}},
            "constraints": {},
            "sources": [{
                "file": "rtl/top.v",
                "module": "top",
                "sha256": "a" * 64,
                "module_text": "module top(input a, output y); assign y=a; endmodule\n",
            }],
            "optimization_units": [{
                "unit_id": "unit_000",
                "critical_evidence": {"affected_path_count": 1, "patterns": ["mux"]},
            }],
            "optimization_regions": [{
                "region_id": "region_000",
                "optimization_unit_id": "unit_000",
                "affected_path_count": 1,
                "timing": {"worst_slack_ns": 0.1},
                "structure": {"patterns": ["mux"]},
                "source_locations": [{"file": "rtl/top.v", "line_start": 1, "line_end": 1}],
            }],
        }
        design.write_text(json.dumps(design_obj), encoding="utf-8")
        schema_obj = {"type": "object", "properties": {"schema_version": {"const": "2.0"}}}
        schema.write_text(json.dumps(schema_obj), encoding="utf-8")
        hist.write_text(json.dumps({
            "iteration": 1,
            "unit_id": "unit_000",
            "verdict": "reject",
            "context_usage": {"estimated_input_tokens": 100},
        }) + "\n", encoding="utf-8")

        req = {
            "schema": "optimizer_request/2",
            "request_format_version": "2.1",
            "unit_id": "unit_000",
            "target": {
                "file": "rtl/top.v", "module": "top", "original_sha256": "a" * 64,
                "module_line_start": 1, "module_line_end": 1,
            },
            "editable_line_ranges": [[1, 1]],
            "rendering_inputs": {
                "fixed_context": {"files": [{"path": str(fixed), "sha256": sha256_file(fixed)}]},
                "design_context": {"path": str(design), "sha256": sha256_file(design)},
                "current_iteration": {"iteration_number": 2, "eligible_pattern_ids": ["mux.x"]},
                "relevant_history_summary": {"iteration": 1, "result": "reject"},
                "previous_attempts": [{
                    "iteration": 1, "chosen_pattern_id": "mux.old", "verdict": "reject",
                    "failure": "carry-width mismatch at line 47",
                }],
                "iteration_history": {"path": str(hist)},
                "response_contract": {"path": str(schema), "sha256": sha256_file(schema)},
            },
            "prompt_rendering": {"finished_prompt_embedded": False},
            "context_budget": {"policy": {
                "service_model_token_budget": 8000,
                "soft_input_token_target": 5000,
                "hard_input_token_limit": 6000,
                "completion_reserve_tokens": 2000,
                "estimator": {"chars_per_token": 4.0},
            }},
        }
        request.write_text(json.dumps(req), encoding="utf-8")

        rendered = render_request_for_llm(request, completion_reserve_tokens=2048)
        assert "module top" in rendered.user_prompt
        assert "mux.x" in rendered.user_prompt
        assert "PREVIOUS_ATTEMPTS" in rendered.user_prompt
        assert "carry-width mismatch" in rendered.user_prompt
        assert "Do not recreate" in rendered.system_prompt
        assert rendered.context_budget["section_estimates"]["previous_attempts"] > 0
        assert rendered.context_budget["estimated_input_tokens"] < 6000
        assert rendered.context_budget["completion_reserve_tokens"] == 2048
        assert rendered.context_budget["cumulative_prior_input_tokens"] == 100
        assert rendered.context_budget["cumulative_is_diagnostic_only"] is True
        print("PASS: prompt_renderer self-test")
        print("PASS: fixed/design/current/history/response sections rendered")
        print("PASS: source/SHA references verified")
        print("PASS: soft/hard/service budgets enforced before API use")
        print("PASS: actual completion reserve participates in budget check")
        print("PASS: cumulative iteration tokens tracked diagnostically")
        print("PASS: prior rejection diagnoses are preserved and compacted last")


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Render and preflight a compact nEBULA LLM request.")
    ap.add_argument("--request", help="optimizer_request/2 JSON")
    ap.add_argument("--response-schema", default=None, help="Optional response-schema override")
    ap.add_argument("--completion-reserve", type=int, default=None)
    ap.add_argument("--metadata-output", default=None, help="Optional metadata-only JSON; prompts are not stored")
    ap.add_argument("--previous-diagnosis", action="append", default=[], help="Prior diagnosis JSON; repeatable")
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
    if not args.request:
        print("ERROR: --request is required", file=sys.stderr)
        return 2
    try:
        rendered = render_request_for_llm(
            Path(args.request),
            response_schema_override=Path(args.response_schema) if args.response_schema else None,
            completion_reserve_tokens=args.completion_reserve,
            previous_diagnosis_paths=tuple(Path(x) for x in args.previous_diagnosis),
        )
        meta = rendered.to_metadata()
        if args.metadata_output:
            out = Path(args.metadata_output)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(meta, indent=2))
        return 0
    except ContextBudgetError as exc:
        print(str(exc), file=sys.stderr)
        return 3
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
