#!/usr/bin/env python3
"""
evaluate_candidate.py

Stage-aware PPA comparison and promotion gate for nEBULA.

This file deliberately does NOT run OpenROAD-flow-scripts itself.

The physical-flow layer is responsible for producing canonical metric artifacts
for a specific evaluation stage. This evaluator consumes those artifacts and
decides whether a formally equivalent candidate is eligible for promotion.

Canonical P0 evaluation flow
----------------------------

    successful rtl_verify/2
              |
              v
    baseline metrics @ stage
              |
              +---- same design/platform
              +---- same evaluation stage
              +---- same frozen physical-recipe fingerprint
              |
    candidate metrics @ stage
              |
              v
       deterministic deltas
              |
              v
       conservative PPA gate
              |
        +-----+------+
        |            |
     promote        reject

Supported stages
----------------

    synth
    global_route
    final

Important safety rule
---------------------

A baseline and candidate are NEVER compared if their stage or physical-recipe
fingerprint differs.

Expected metric artifact contract
---------------------------------

The evaluator is intentionally tolerant about where the four canonical metric
values are stored, but strict about their meaning.

Recommended shape:

{
  "schema": "ppa_metrics/2",
  "design": {
    "design_id": "mapper_reconvergent_test",
    "top": "mapper_reconvergent_test"
  },
  "platform": "nangate45",
  "evaluation": {
    "stage": "global_route",
    "recipe_fingerprint_sha256": "<64 hex chars>"
  },
  "metrics": {
    "wns_ns": 0.196096,
    "tns_ns": 0.0,
    "area_um2": 1228.92,
    "power_mw": 0.43
  }
}

For compatibility with the current collector, the metric values may also be
at the JSON root. However, stage and physical-recipe fingerprint are mandatory.

Promotion policy
----------------

P0 uses a conservative Pareto-style default:

    - WNS must not regress
    - TNS must not regress
    - area must not regress
    - power must not regress
    - at least one metric must strictly improve

Small regressions can be explicitly permitted with CLI tolerances. More complex
weighted/objective policies belong to the later objective-policy layer.

Normal usage
------------

python3 timing_automation/evaluate_candidate.py \
    --stage global_route \
    --baseline-metrics ai_runs/baseline_global_route_metrics.json \
    --candidate-metrics ai_runs/cand_0001_global_route_metrics.json \
    --verification ai_runs/cand_0001.verify.json \
    --output ai_runs/cand_0001_global_route_evaluation.json

Self-test
---------

python3 timing_automation/evaluate_candidate.py --self-test
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence, Tuple



try:
    from equivalence_terms import EquivalenceVerdict, is_proven
except ImportError:
    from timing_automation.equivalence_terms import EquivalenceVerdict, is_proven

# =============================================================================
# Constants
# =============================================================================

SCHEMA = "candidate_evaluation/1"

STAGE_SYNTH = "synth"
STAGE_GLOBAL_ROUTE = "global_route"
STAGE_FINAL = "final"

VALID_STAGES = (
    STAGE_SYNTH,
    STAGE_GLOBAL_ROUTE,
    STAGE_FINAL,
)

NEXT_STAGE = {
    STAGE_SYNTH: STAGE_GLOBAL_ROUTE,
    STAGE_GLOBAL_ROUTE: STAGE_FINAL,
    STAGE_FINAL: None,
}

REQUIRED_METRICS = (
    "wns_ns",
    "tns_ns",
    "area_um2",
    "power_mw",
)

SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


# =============================================================================
# Exceptions
# =============================================================================


class EvaluationError(RuntimeError):
    """Base candidate-evaluation error."""


class EvaluationInputError(EvaluationError):
    """Raised when evaluation inputs are inconsistent or incomplete."""


# =============================================================================
# Generic helpers
# =============================================================================


def load_json(path: Path) -> Dict[str, Any]:
    path = Path(path)

    if not path.is_file():
        raise EvaluationInputError(f"JSON file not found: {path}")

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EvaluationInputError(
            f"invalid JSON in {path}: {exc}"
        ) from exc

    if not isinstance(value, dict):
        raise EvaluationInputError(
            f"JSON root must be an object: {path}"
        )

    return value


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )


def normalize_number(value: Any, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EvaluationInputError(
            f"{field} must be numeric, got {type(value).__name__}"
        )

    number = float(value)

    if not math.isfinite(number):
        raise EvaluationInputError(
            f"{field} must be finite, got {value!r}"
        )

    # Match the canonical six-decimal precision used by the metrics pipeline.
    return round(number, 6)


def round6(value: float) -> float:
    return round(float(value), 6)


def percent_delta(candidate: float, baseline: float, *, field: str) -> float:
    if baseline == 0:
        raise EvaluationInputError(
            f"cannot compute percent delta for {field}: baseline is zero"
        )

    return round6(((candidate - baseline) / baseline) * 100.0)


def get_nested(
    root: Mapping[str, Any],
    *paths: Sequence[str],
) -> Any:
    """Return the first existing nested path."""

    for path in paths:
        current: Any = root
        found = True

        for key in path:
            if not isinstance(current, Mapping) or key not in current:
                found = False
                break
            current = current[key]

        if found:
            return current

    return None


# =============================================================================
# Canonical metric view
# =============================================================================


@dataclass(frozen=True)
class CanonicalMetrics:
    wns_ns: float
    tns_ns: float
    area_um2: float
    power_mw: float

    def to_dict(self) -> Dict[str, float]:
        return {
            "wns_ns": self.wns_ns,
            "tns_ns": self.tns_ns,
            "area_um2": self.area_um2,
            "power_mw": self.power_mw,
        }


@dataclass(frozen=True)
class MetricArtifact:
    schema: str | None
    design_id: str
    top: str | None
    platform: str
    stage: str
    recipe_fingerprint_sha256: str
    flow_variant: str | None
    metrics: CanonicalMetrics

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": self.schema,
            "design_id": self.design_id,
            "top": self.top,
            "platform": self.platform,
            "stage": self.stage,
            "recipe_fingerprint_sha256": self.recipe_fingerprint_sha256,
            "flow_variant": self.flow_variant,
            "metrics": self.metrics.to_dict(),
        }


def _extract_design_id(payload: Mapping[str, Any]) -> str:
    design = payload.get("design")

    if isinstance(design, str) and design.strip():
        return design.strip()

    if isinstance(design, Mapping):
        for key in ("design_id", "name", "design"):
            value = design.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

    for key in ("design_id", "design_name"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    raise EvaluationInputError(
        "metric artifact is missing design identity"
    )


def _extract_top(payload: Mapping[str, Any]) -> str | None:
    design = payload.get("design")

    if isinstance(design, Mapping):
        value = design.get("top")
        if isinstance(value, str) and value.strip():
            return value.strip()

    value = payload.get("top")
    if isinstance(value, str) and value.strip():
        return value.strip()

    return None


def _extract_platform(payload: Mapping[str, Any]) -> str:
    value = get_nested(
        payload,
        ("platform",),
        ("design", "platform"),
        ("context", "platform"),
        ("metadata", "platform"),
    )

    if not isinstance(value, str) or not value.strip():
        raise EvaluationInputError(
            "metric artifact is missing platform"
        )

    return value.strip()


def _extract_stage(payload: Mapping[str, Any]) -> str:
    value = get_nested(
        payload,
        ("stage",),
        ("evaluation_stage",),
        ("evaluation", "stage"),
        ("context", "stage"),
        ("metadata", "evaluation_stage"),
    )

    if not isinstance(value, str) or not value.strip():
        raise EvaluationInputError(
            "metric artifact is missing evaluation stage"
        )

    stage = value.strip()

    if stage not in VALID_STAGES:
        raise EvaluationInputError(
            f"unsupported evaluation stage {stage!r}; "
            f"expected one of {list(VALID_STAGES)}"
        )

    return stage


def _extract_recipe_fingerprint(payload: Mapping[str, Any]) -> str:
    value = get_nested(
        payload,
        ("recipe_fingerprint_sha256",),
        ("physical_recipe_fingerprint_sha256",),
        ("evaluation", "recipe_fingerprint_sha256"),
        ("physical_recipe", "fingerprint_sha256"),
        ("context", "recipe_fingerprint_sha256"),
        ("metadata", "recipe_fingerprint_sha256"),
    )

    if not isinstance(value, str) or not SHA256_RE.fullmatch(value.strip()):
        raise EvaluationInputError(
            "metric artifact must contain a valid "
            "recipe_fingerprint_sha256 (64 hex characters)"
        )

    return value.strip().lower()


def _extract_flow_variant(payload: Mapping[str, Any]) -> str | None:
    value = get_nested(
        payload,
        ("flow_variant",),
        ("run", "flow_variant"),
        ("context", "flow_variant"),
        ("metadata", "flow_variant"),
    )

    if isinstance(value, str) and value.strip():
        return value.strip()

    return None


def _extract_metrics_object(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    candidates = (
        payload.get("metrics"),
        get_nested(payload, ("ppa", "metrics")),
        get_nested(payload, ("canonical_metrics",)),
        payload,
    )

    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue

        if all(key in candidate for key in REQUIRED_METRICS):
            return candidate

    raise EvaluationInputError(
        "metric artifact does not contain canonical metrics "
        f"{list(REQUIRED_METRICS)}"
    )


def parse_metric_artifact(
    payload: Mapping[str, Any],
) -> MetricArtifact:
    metrics_obj = _extract_metrics_object(payload)

    metrics = CanonicalMetrics(
        wns_ns=normalize_number(
            metrics_obj["wns_ns"],
            field="wns_ns",
        ),
        tns_ns=normalize_number(
            metrics_obj["tns_ns"],
            field="tns_ns",
        ),
        area_um2=normalize_number(
            metrics_obj["area_um2"],
            field="area_um2",
        ),
        power_mw=normalize_number(
            metrics_obj["power_mw"],
            field="power_mw",
        ),
    )

    if metrics.area_um2 <= 0:
        raise EvaluationInputError(
            f"area_um2 must be > 0, got {metrics.area_um2}"
        )

    if metrics.power_mw < 0:
        raise EvaluationInputError(
            f"power_mw must be >= 0, got {metrics.power_mw}"
        )

    schema = payload.get("schema")
    if schema is not None and not isinstance(schema, str):
        raise EvaluationInputError(
            "metric artifact schema must be a string when present"
        )

    return MetricArtifact(
        schema=schema,
        design_id=_extract_design_id(payload),
        top=_extract_top(payload),
        platform=_extract_platform(payload),
        stage=_extract_stage(payload),
        recipe_fingerprint_sha256=_extract_recipe_fingerprint(payload),
        flow_variant=_extract_flow_variant(payload),
        metrics=metrics,
    )


# =============================================================================
# Verification gate
# =============================================================================


@dataclass(frozen=True)
class VerificationGate:
    ok: bool
    schema: str | None
    status: str | None
    backend: str | None
    mode: str | None
    reason: str | None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "schema": self.schema,
            "status": self.status,
            "backend": self.backend,
            "mode": self.mode,
            "reason": self.reason,
        }


def parse_verification_gate(
    payload: Mapping[str, Any],
) -> VerificationGate:
    schema = payload.get("schema")

    if schema != "rtl_verify/2":
        raise EvaluationInputError(
            "verification result schema must be 'rtl_verify/2'"
        )

    ok = payload.get("ok")
    status = payload.get("status")

    verification = payload.get("verification")
    backend = None
    mode = None

    if isinstance(verification, Mapping):
        raw_backend = (
            verification.get("canonical_backend")
            or verification.get("backend")
        )
        raw_mode = verification.get("mode")

        if isinstance(raw_backend, str):
            backend = raw_backend

        if isinstance(raw_mode, str):
            mode = raw_mode

    reason = payload.get("reason")
    if reason is not None and not isinstance(reason, str):
        reason = str(reason)

    passed = (
        ok is True
        and isinstance(status, str)
        and is_proven(status)
    )

    if not passed:
        if status == EquivalenceVerdict.DISPROVEN.value:
            detail = "candidate is functionally disproven by a counterexample"
        elif status == EquivalenceVerdict.NOT_PROVEN.value:
            detail = "equivalence proof did not close; this is not a disproof"
        elif status == EquivalenceVerdict.NOT_ATTEMPTED.value:
            detail = "equivalence was not attempted"
        elif status == EquivalenceVerdict.TOOL_ERROR.value:
            detail = "equivalence tool/setup failed"
        else:
            detail = "candidate has no accepted equivalence verdict"
        raise EvaluationInputError(
            f"{detail}: ok={ok!r}, status={status!r}, reason={reason!r}"
        )

    return VerificationGate(
        ok=True,
        schema=schema,
        status=status,
        backend=backend,
        mode=mode,
        reason=reason,
    )


# =============================================================================
# Promotion policy
# =============================================================================


@dataclass(frozen=True)
class PromotionPolicy:
    """
    Conservative P0 Pareto gate.

    A positive tolerance permits that much regression.
    """

    max_wns_regression_ns: float = 0.0
    max_tns_regression_ns: float = 0.0
    max_area_regression_pct: float = 0.0
    max_power_regression_pct: float = 0.0
    improvement_epsilon: float = 1e-9

    def validate(self) -> None:
        fields = {
            "max_wns_regression_ns": self.max_wns_regression_ns,
            "max_tns_regression_ns": self.max_tns_regression_ns,
            "max_area_regression_pct": self.max_area_regression_pct,
            "max_power_regression_pct": self.max_power_regression_pct,
            "improvement_epsilon": self.improvement_epsilon,
        }

        for name, value in fields.items():
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise EvaluationInputError(
                    f"policy {name} must be numeric"
                )
            if not math.isfinite(float(value)):
                raise EvaluationInputError(
                    f"policy {name} must be finite"
                )
            if float(value) < 0:
                raise EvaluationInputError(
                    f"policy {name} must be >= 0"
                )

    def to_dict(self) -> Dict[str, float]:
        return {
            "max_wns_regression_ns": round6(self.max_wns_regression_ns),
            "max_tns_regression_ns": round6(self.max_tns_regression_ns),
            "max_area_regression_pct": round6(self.max_area_regression_pct),
            "max_power_regression_pct": round6(self.max_power_regression_pct),
            "improvement_epsilon": float(self.improvement_epsilon),
        }


# =============================================================================
# Evaluation
# =============================================================================


def _validate_identity(
    *,
    baseline: MetricArtifact,
    candidate: MetricArtifact,
    requested_stage: str,
) -> Dict[str, bool]:
    checks: Dict[str, bool] = {}

    checks["same_design"] = baseline.design_id == candidate.design_id

    checks["same_top"] = (
        baseline.top is None
        or candidate.top is None
        or baseline.top == candidate.top
    )

    checks["same_platform"] = baseline.platform == candidate.platform

    checks["baseline_stage_matches_request"] = (
        baseline.stage == requested_stage
    )

    checks["candidate_stage_matches_request"] = (
        candidate.stage == requested_stage
    )

    checks["same_stage"] = baseline.stage == candidate.stage

    checks["same_physical_recipe"] = (
        baseline.recipe_fingerprint_sha256
        == candidate.recipe_fingerprint_sha256
    )

    return checks


def _identity_failure_reasons(
    checks: Mapping[str, bool],
    *,
    baseline: MetricArtifact,
    candidate: MetricArtifact,
    requested_stage: str,
) -> list[str]:
    reasons: list[str] = []

    if not checks["same_design"]:
        reasons.append(
            "design mismatch: "
            f"baseline={baseline.design_id!r}, "
            f"candidate={candidate.design_id!r}"
        )

    if not checks["same_top"]:
        reasons.append(
            "top mismatch: "
            f"baseline={baseline.top!r}, candidate={candidate.top!r}"
        )

    if not checks["same_platform"]:
        reasons.append(
            "platform mismatch: "
            f"baseline={baseline.platform!r}, "
            f"candidate={candidate.platform!r}"
        )

    if not checks["baseline_stage_matches_request"]:
        reasons.append(
            "baseline metric stage does not match requested stage: "
            f"{baseline.stage!r} != {requested_stage!r}"
        )

    if not checks["candidate_stage_matches_request"]:
        reasons.append(
            "candidate metric stage does not match requested stage: "
            f"{candidate.stage!r} != {requested_stage!r}"
        )

    if not checks["same_stage"]:
        reasons.append(
            "baseline/candidate stage mismatch: "
            f"{baseline.stage!r} != {candidate.stage!r}"
        )

    if not checks["same_physical_recipe"]:
        reasons.append(
            "baseline/candidate physical-recipe fingerprint mismatch"
        )

    return reasons


def assess_guided_candidate(baseline, candidate, proof, budgets, comparison_reference=None, latency_result=None):
    """Measured same-recipe gate against the immutable Current-Best snapshot."""
    result = {"keep": False, "comparable": False, "budgets": dict(budgets),
              "comparison_reference": comparison_reference,
              "latency": dict(latency_result or {})}
    if latency_result is not None and not bool(latency_result.get("passed")):
        return {**result, "reason": "latency_policy_failed"}
    if proof.get("status") != "passed":
        return {**result, "reason": "not_proven"}
    if any(run.get("status") != "passed" for run in (baseline, candidate)):
        return {**result, "reason": "physical_flow_failed"}
    if not baseline.get("recipe_fingerprint_sha256") or baseline["recipe_fingerprint_sha256"] != candidate.get("recipe_fingerprint_sha256"):
        return {**result, "reason": "recipe_mismatch"}
    b, c = baseline.get("metrics", {}), candidate.get("metrics", {})
    keys = ("area_um2", "power_w", "setup_ws_ns", "hold_ws_ns", "setup_tns_ns", "hold_tns_ns")
    if any(type(m.get(k)) not in (int, float) or not math.isfinite(m[k]) for m in (b, c) for k in keys):
        return {**result, "reason": "invalid_metrics"}
    if b["area_um2"] <= 0 or b["power_w"] <= 0 or c["area_um2"] < 0 or c["power_w"] < 0:
        return {**result, "reason": "degenerate_metrics"}
    canonical = lambda m: CanonicalMetrics(m["setup_ws_ns"], m["setup_tns_ns"], m["area_um2"], m["power_w"] * 1000)
    deltas = compute_deltas(canonical(b), canonical(c))
    deltas.update(setup_slack_delta_ns=c["setup_ws_ns"] - b["setup_ws_ns"],
                  hold_slack_delta_ns=c["hold_ws_ns"] - b["hold_ws_ns"])
    # Use unrounded measured differences for boundary enforcement.
    checks = {"area_budget": 100 * (c["area_um2"] - b["area_um2"]) / b["area_um2"] <= budgets["max_area_regression_pct"],
              "power_budget": 100 * (c["power_w"] - b["power_w"]) / b["power_w"] <= budgets["max_power_regression_pct"],
              "setup_budget": deltas["setup_slack_delta_ns"] >= -budgets["max_setup_slack_regression_ns"],
              "hold_budget": deltas["hold_slack_delta_ns"] >= -budgets["max_hold_slack_regression_ns"],
              "timing_clean": all(m[k] >= 0 for m in (b, c) for k in keys[2:]),
              "latency_budget": True if latency_result is None else bool(latency_result.get("passed")),
              "measurable_improvement": (c["area_um2"] <= .99 * b["area_um2"] or
                                         c["power_w"] <= .99 * b["power_w"] or
                                         deltas["setup_slack_delta_ns"] >= .01)}
    return {**result, "keep": all(checks.values()), "comparable": True, "deltas": deltas,
            "checks": checks, "reason": "accepted" if all(checks.values()) else "policy_rejected"}


def compute_deltas(
    baseline: CanonicalMetrics,
    candidate: CanonicalMetrics,
) -> Dict[str, float]:
    return {
        # Higher is better.
        "wns_gain_ns": round6(candidate.wns_ns - baseline.wns_ns),
        "tns_gain_ns": round6(candidate.tns_ns - baseline.tns_ns),

        # Lower is better.
        "area_delta_um2": round6(candidate.area_um2 - baseline.area_um2),
        "area_delta_pct": percent_delta(
            candidate.area_um2,
            baseline.area_um2,
            field="area_um2",
        ),
        "power_delta_mw": round6(candidate.power_mw - baseline.power_mw),
        "power_delta_pct": percent_delta(
            candidate.power_mw,
            baseline.power_mw,
            field="power_mw",
        ),
    }


def apply_policy(
    *,
    baseline: CanonicalMetrics,
    candidate: CanonicalMetrics,
    deltas: Mapping[str, float],
    policy: PromotionPolicy,
) -> Tuple[bool, Dict[str, bool], list[str]]:
    policy.validate()

    checks = {
        "wns_within_regression_limit": (
            deltas["wns_gain_ns"] >= -policy.max_wns_regression_ns
        ),
        "tns_within_regression_limit": (
            deltas["tns_gain_ns"] >= -policy.max_tns_regression_ns
        ),
        "area_within_regression_limit": (
            deltas["area_delta_pct"] <= policy.max_area_regression_pct
        ),
        "power_within_regression_limit": (
            deltas["power_delta_pct"] <= policy.max_power_regression_pct
        ),
    }

    epsilon = policy.improvement_epsilon

    improvements = {
        "wns_improved": candidate.wns_ns > baseline.wns_ns + epsilon,
        "tns_improved": candidate.tns_ns > baseline.tns_ns + epsilon,
        "area_improved": candidate.area_um2 < baseline.area_um2 - epsilon,
        "power_improved": candidate.power_mw < baseline.power_mw - epsilon,
    }

    checks.update(improvements)
    checks["at_least_one_metric_improved"] = any(improvements.values())

    hard_names = (
        "wns_within_regression_limit",
        "tns_within_regression_limit",
        "area_within_regression_limit",
        "power_within_regression_limit",
        "at_least_one_metric_improved",
    )

    promote = all(checks[name] for name in hard_names)

    reasons: list[str] = []

    if not checks["wns_within_regression_limit"]:
        reasons.append(
            "WNS regression exceeds policy: "
            f"gain={deltas['wns_gain_ns']} ns, "
            f"allowed_regression={policy.max_wns_regression_ns} ns"
        )

    if not checks["tns_within_regression_limit"]:
        reasons.append(
            "TNS regression exceeds policy: "
            f"gain={deltas['tns_gain_ns']} ns, "
            f"allowed_regression={policy.max_tns_regression_ns} ns"
        )

    if not checks["area_within_regression_limit"]:
        reasons.append(
            "area regression exceeds policy: "
            f"delta={deltas['area_delta_pct']}%, "
            f"allowed={policy.max_area_regression_pct}%"
        )

    if not checks["power_within_regression_limit"]:
        reasons.append(
            "power regression exceeds policy: "
            f"delta={deltas['power_delta_pct']}%, "
            f"allowed={policy.max_power_regression_pct}%"
        )

    if not checks["at_least_one_metric_improved"]:
        reasons.append(
            "candidate does not strictly improve any canonical PPA metric"
        )

    return promote, checks, reasons


def evaluate_candidate(
    *,
    requested_stage: str,
    baseline_payload: Mapping[str, Any],
    candidate_payload: Mapping[str, Any],
    verification_payload: Mapping[str, Any],
    policy: PromotionPolicy | None = None,
) -> Dict[str, Any]:
    if requested_stage not in VALID_STAGES:
        raise EvaluationInputError(
            f"invalid requested stage {requested_stage!r}; "
            f"expected one of {list(VALID_STAGES)}"
        )

    active_policy = policy or PromotionPolicy()
    active_policy.validate()

    verification = parse_verification_gate(verification_payload)
    baseline = parse_metric_artifact(baseline_payload)
    candidate = parse_metric_artifact(candidate_payload)

    identity_checks = _validate_identity(
        baseline=baseline,
        candidate=candidate,
        requested_stage=requested_stage,
    )

    identity_reasons = _identity_failure_reasons(
        identity_checks,
        baseline=baseline,
        candidate=candidate,
        requested_stage=requested_stage,
    )

    if identity_reasons:
        raise EvaluationInputError(
            "metric artifacts are not directly comparable: "
            + "; ".join(identity_reasons)
        )

    deltas = compute_deltas(
        baseline.metrics,
        candidate.metrics,
    )

    promote, policy_checks, policy_reasons = apply_policy(
        baseline=baseline.metrics,
        candidate=candidate.metrics,
        deltas=deltas,
        policy=active_policy,
    )

    checks = {
        "formal_equivalence_passed": True,
        **identity_checks,
        **policy_checks,
    }

    next_stage = NEXT_STAGE[requested_stage]

    result: Dict[str, Any] = {
        "tool": "evaluate_candidate.py",
        "schema": SCHEMA,
        "ok": True,
        "decision": "promote" if promote else "reject",
        "promote": promote,
        "stage": requested_stage,
        "promote_to": (
            next_stage
            if promote and next_stage is not None
            else (
                "final_accept"
                if promote and requested_stage == STAGE_FINAL
                else None
            )
        ),
        "design": {
            "design_id": baseline.design_id,
            "top": baseline.top or candidate.top,
            "platform": baseline.platform,
        },
        "physical_recipe": {
            "fingerprint_sha256": baseline.recipe_fingerprint_sha256,
        },
        "verification_gate": verification.to_dict(),
        "baseline": baseline.to_dict(),
        "candidate": candidate.to_dict(),
        "delta": deltas,
        "policy": active_policy.to_dict(),
        "checks": checks,
        "reasons": policy_reasons,
    }

    return result


# =============================================================================
# Self-test
# =============================================================================


def _metric_payload(
    *,
    design_id: str = "demo",
    top: str = "top",
    platform: str = "nangate45",
    stage: str = STAGE_GLOBAL_ROUTE,
    recipe: str = "a" * 64,
    wns_ns: float = 0.1,
    tns_ns: float = 0.0,
    area_um2: float = 1000.0,
    power_mw: float = 1.0,
    flow_variant: str = "run_test",
) -> Dict[str, Any]:
    return {
        "schema": "ppa_metrics/2",
        "design": {
            "design_id": design_id,
            "top": top,
        },
        "platform": platform,
        "flow_variant": flow_variant,
        "evaluation": {
            "stage": stage,
            "recipe_fingerprint_sha256": recipe,
        },
        "metrics": {
            "wns_ns": wns_ns,
            "tns_ns": tns_ns,
            "area_um2": area_um2,
            "power_mw": power_mw,
        },
    }


def _verify_payload(
    *,
    ok: bool = True,
    status: str = EquivalenceVerdict.PROVEN_BOUNDED.value,
) -> Dict[str, Any]:
    return {
        "tool": "verify_rtl.py",
        "schema": "rtl_verify/2",
        "ok": ok,
        "status": status,
        "reason": "self-test",
        "verification": {
            "backend": "yosys",
            "canonical_backend": "yosys",
            "mode": "sequential_equivalence",
        },
    }


def _expect_input_failure(callable_obj, expected_text: str) -> None:
    try:
        callable_obj()
    except EvaluationInputError as exc:
        assert expected_text in str(exc), str(exc)
    else:
        raise AssertionError(
            f"expected EvaluationInputError containing {expected_text!r}"
        )


def run_self_test() -> None:
    baseline = _metric_payload()

    better = _metric_payload(
        wns_ns=0.12,
        tns_ns=0.0,
        area_um2=980.0,
        power_mw=0.95,
        flow_variant="cand_good",
    )

    good = evaluate_candidate(
        requested_stage=STAGE_GLOBAL_ROUTE,
        baseline_payload=baseline,
        candidate_payload=better,
        verification_payload=_verify_payload(),
    )

    assert good["ok"] is True
    assert good["promote"] is True
    assert good["decision"] == "promote"
    assert good["promote_to"] == STAGE_FINAL
    assert good["delta"]["wns_gain_ns"] == 0.02
    assert good["delta"]["area_delta_pct"] == -2.0
    assert good["delta"]["power_delta_pct"] == -5.0

    # ------------------------------------------------------------------
    # Regression must reject under the conservative default policy.
    # ------------------------------------------------------------------

    worse = _metric_payload(
        wns_ns=0.09,
        tns_ns=0.0,
        area_um2=1000.0,
        power_mw=1.0,
        flow_variant="cand_worse",
    )

    rejected = evaluate_candidate(
        requested_stage=STAGE_GLOBAL_ROUTE,
        baseline_payload=baseline,
        candidate_payload=worse,
        verification_payload=_verify_payload(),
    )

    assert rejected["ok"] is True
    assert rejected["promote"] is False
    assert rejected["decision"] == "reject"
    assert rejected["checks"]["wns_within_regression_limit"] is False

    # ------------------------------------------------------------------
    # Explicit tolerance may permit a small trade-off if another metric
    # strictly improves.
    # ------------------------------------------------------------------

    tradeoff = _metric_payload(
        wns_ns=0.095,
        tns_ns=0.0,
        area_um2=970.0,
        power_mw=1.0,
        flow_variant="cand_tradeoff",
    )

    tolerated = evaluate_candidate(
        requested_stage=STAGE_GLOBAL_ROUTE,
        baseline_payload=baseline,
        candidate_payload=tradeoff,
        verification_payload=_verify_payload(),
        policy=PromotionPolicy(
            max_wns_regression_ns=0.01,
        ),
    )

    assert tolerated["promote"] is True
    assert tolerated["checks"]["area_improved"] is True

    # ------------------------------------------------------------------
    # Stage mismatch must fail before computing a promotion decision.
    # ------------------------------------------------------------------

    wrong_stage = _metric_payload(
        stage=STAGE_FINAL,
        flow_variant="wrong_stage",
    )

    _expect_input_failure(
        lambda: evaluate_candidate(
            requested_stage=STAGE_GLOBAL_ROUTE,
            baseline_payload=baseline,
            candidate_payload=wrong_stage,
            verification_payload=_verify_payload(),
        ),
        "not directly comparable",
    )

    # ------------------------------------------------------------------
    # Physical recipe mismatch must fail closed.
    # ------------------------------------------------------------------

    wrong_recipe = _metric_payload(
        recipe="b" * 64,
        flow_variant="wrong_recipe",
    )

    _expect_input_failure(
        lambda: evaluate_candidate(
            requested_stage=STAGE_GLOBAL_ROUTE,
            baseline_payload=baseline,
            candidate_payload=wrong_recipe,
            verification_payload=_verify_payload(),
        ),
        "physical-recipe fingerprint mismatch",
    )

    # ------------------------------------------------------------------
    # Design mismatch must fail closed.
    # ------------------------------------------------------------------

    wrong_design = _metric_payload(
        design_id="other_design",
        flow_variant="wrong_design",
    )

    _expect_input_failure(
        lambda: evaluate_candidate(
            requested_stage=STAGE_GLOBAL_ROUTE,
            baseline_payload=baseline,
            candidate_payload=wrong_design,
            verification_payload=_verify_payload(),
        ),
        "design mismatch",
    )

    # ------------------------------------------------------------------
    # Failed formal verification may never reach PPA promotion.
    # ------------------------------------------------------------------

    _expect_input_failure(
        lambda: evaluate_candidate(
            requested_stage=STAGE_GLOBAL_ROUTE,
            baseline_payload=baseline,
            candidate_payload=better,
            verification_payload=_verify_payload(
                ok=False,
                status=EquivalenceVerdict.NOT_PROVEN.value,
            ),
        ),
        "proof did not close",
    )

    # ------------------------------------------------------------------
    # Final stage promotes to final_accept, not another physical stage.
    # ------------------------------------------------------------------

    final_baseline = _metric_payload(
        stage=STAGE_FINAL,
        flow_variant="final_base",
    )

    final_better = _metric_payload(
        stage=STAGE_FINAL,
        wns_ns=0.11,
        area_um2=990.0,
        power_mw=0.99,
        flow_variant="final_cand",
    )

    final_result = evaluate_candidate(
        requested_stage=STAGE_FINAL,
        baseline_payload=final_baseline,
        candidate_payload=final_better,
        verification_payload=_verify_payload(),
    )

    assert final_result["promote"] is True
    assert final_result["promote_to"] == "final_accept"

    print("PASS: evaluate_candidate self-test")
    print("PASS: canonical WNS/TNS/area/power metrics consumed")
    print("PASS: six-decimal metric normalization preserved")
    print("PASS: successful formal verification required")
    print("PASS: same-design/platform identity enforced")
    print("PASS: same-stage comparison enforced")
    print("PASS: physical-recipe fingerprint equality enforced")
    print("PASS: conservative Pareto promotion works")
    print("PASS: explicit regression tolerances work")
    print("PASS: synth/global_route/final promotion chain represented")
    print("PASS: final-stage candidate promotes to final_accept")


# =============================================================================
# CLI
# =============================================================================


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Compare stage-matched baseline/candidate PPA metrics and decide "
            "whether a formally verified candidate should be promoted."
        )
    )

    parser.add_argument(
        "--stage",
        choices=VALID_STAGES,
        default=None,
        help="Evaluation stage.",
    )

    parser.add_argument(
        "--baseline-metrics",
        default=None,
        help="Baseline canonical metric JSON for this stage.",
    )

    parser.add_argument(
        "--candidate-metrics",
        default=None,
        help="Candidate canonical metric JSON for this stage.",
    )

    parser.add_argument(
        "--verification",
        default=None,
        help="Successful rtl_verify/2 result for the candidate.",
    )

    parser.add_argument(
        "--max-wns-regression-ns",
        type=float,
        default=0.0,
        help="Allowed WNS regression in ns. Default: 0.",
    )

    parser.add_argument(
        "--max-tns-regression-ns",
        type=float,
        default=0.0,
        help="Allowed TNS regression in ns. Default: 0.",
    )

    parser.add_argument(
        "--max-area-regression-pct",
        type=float,
        default=0.0,
        help="Allowed area increase in percent. Default: 0.",
    )

    parser.add_argument(
        "--max-power-regression-pct",
        type=float,
        default=0.0,
        help="Allowed power increase in percent. Default: 0.",
    )

    parser.add_argument(
        "--output",
        default=None,
        help="Optional candidate_evaluation/1 JSON output.",
    )

    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run standalone regression tests.",
    )

    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.self_test:
        try:
            run_self_test()
        except Exception as exc:
            print(
                f"SELF-TEST FAILED: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return 1
        return 0

    missing = []

    if not args.stage:
        missing.append("--stage")
    if not args.baseline_metrics:
        missing.append("--baseline-metrics")
    if not args.candidate_metrics:
        missing.append("--candidate-metrics")
    if not args.verification:
        missing.append("--verification")

    if missing:
        print(
            "ERROR: missing required argument(s): " + ", ".join(missing),
            file=sys.stderr,
        )
        return 2

    try:
        policy = PromotionPolicy(
            max_wns_regression_ns=args.max_wns_regression_ns,
            max_tns_regression_ns=args.max_tns_regression_ns,
            max_area_regression_pct=args.max_area_regression_pct,
            max_power_regression_pct=args.max_power_regression_pct,
        )

        result = evaluate_candidate(
            requested_stage=args.stage,
            baseline_payload=load_json(Path(args.baseline_metrics)),
            candidate_payload=load_json(Path(args.candidate_metrics)),
            verification_payload=load_json(Path(args.verification)),
            policy=policy,
        )

        if args.output:
            write_json(Path(args.output), result)

        print(json.dumps(result, indent=2))

        # 0 = valid evaluation + promote
        # 1 = valid evaluation + reject
        # 2 = invalid/incomparable inputs
        return 0 if result["promote"] is True else 1

    except EvaluationInputError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(
            f"ERROR: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
