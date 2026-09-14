#!/usr/bin/env python3
"""
run_optimizer.py

Single-shot nEBULA RTL optimization transaction.

This is the P0 orchestrator.  All expensive or semantic decisions are delegated
to the already-tested deterministic/formal/physical modules:

    optimizer_request/2
            |
            +--> Groq structured candidate OR --candidate reuse
            |
            v
      canonical patches[] candidate
            |
            v
      lint_candidate.py
        - request/file/SHA/editable-range checks
        - deterministic splice
        - Yosys interface/parameter/hierarchy checks
        - Yosys state-boundary fingerprint
            |
            v
      accepted candidate source bundle
            |
            v
      sec_bundle.py
            |
            v
      verify_rtl.py
            |
       equivalent?
        /      \
      no        yes
      |          |
   REJECT        v
          run_candidate_flow.py
          (golden RTL untouched)
                 |
                 v
          collect_metrics.py
          (same frozen stage/recipe)
                 |
                 v
          evaluate_candidate.py
                 |
          PROMOTE / REJECT

Important
---------
"PROMOTE" means the candidate passed all gates and its accepted source bundle is
retained as an artifact.  This script NEVER overwrites the golden RTL.

Exit codes
----------
0  candidate promoted, or --no-orfs stopped after proven equivalence
1  candidate rejected by lint, formal equivalence, or PPA policy
2  setup/infrastructure/tool failure

Typical generated-candidate usage
---------------------------------
export GROQ_API_KEY=...

python3 timing_automation/run_optimizer.py \
    --request ai_runs/<variant>_unit_000_request.json \
    --golden-manifest path/to/golden/design_manifest.json \
    --golden-base-dir . \
    --physical-recipe ai_runs/mapper_global_route.physical_recipe.json \
    --baseline-metrics ai_runs/<baseline>_global_route_metrics.json \
    --model openai/gpt-oss-120b \
    --tag cand_0001

Reuse a hand-written/already-generated candidate
------------------------------------------------
python3 timing_automation/run_optimizer.py \
    --request ai_runs/<variant>_unit_000_request.json \
    --golden-manifest path/to/golden/design_manifest.json \
    --golden-base-dir . \
    --physical-recipe ai_runs/mapper_global_route.physical_recipe.json \
    --baseline-metrics ai_runs/<baseline>_global_route_metrics.json \
    --candidate ai_runs/cand_0001.candidate.json \
    --tag cand_0001_reuse
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple


# =============================================================================
# Local imports
# =============================================================================

try:
    from constraint_model import verify_frozen_constraints
    from qualify_dataset import qualify_dataset
    from design_manifest import DesignManifest
    from evaluate_candidate import (
        EvaluationInputError,
        PromotionPolicy,
        evaluate_candidate,
    )
    from lint_candidate import lint_candidate
    from physical_recipe import PhysicalRecipe, frozen_physical_variables
    from run_candidate_flow import (
        CandidateFlowError,
        run_candidate_flow,
    )
    from sec_bundle import (
        SECBundleError,
        build_sec_bundle,
    )
    from verify_rtl import (
        VerificationBackendError,
        VerificationSetupError,
        run_verification,
        write_json as write_verify_json,
    )
    from collect_metrics import (
        collect_metrics,
        save_metrics,
    )
    from equivalence_terms import EquivalenceVerdict, describe as describe_equivalence, is_proven
except ImportError:
    from timing_automation.constraint_model import verify_frozen_constraints
    from timing_automation.qualify_dataset import qualify_dataset
    from timing_automation.design_manifest import DesignManifest
    from timing_automation.evaluate_candidate import (
        EvaluationInputError,
        PromotionPolicy,
        evaluate_candidate,
    )
    from timing_automation.lint_candidate import lint_candidate
    from timing_automation.physical_recipe import PhysicalRecipe, frozen_physical_variables
    from timing_automation.run_candidate_flow import (
        CandidateFlowError,
        run_candidate_flow,
    )
    from timing_automation.sec_bundle import (
        SECBundleError,
        build_sec_bundle,
    )
    from timing_automation.verify_rtl import (
        VerificationBackendError,
        VerificationSetupError,
        run_verification,
        write_json as write_verify_json,
    )
    from timing_automation.collect_metrics import (
        collect_metrics,
        save_metrics,
    )
    from timing_automation.equivalence_terms import EquivalenceVerdict, describe as describe_equivalence, is_proven


# =============================================================================
# Paths / schemas
# =============================================================================

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_FLOW_DIR = SCRIPT_DIR.parent
DEFAULT_AI_RUNS = DEFAULT_FLOW_DIR / "ai_runs"
DEFAULT_RESPONSE_SCHEMA = SCRIPT_DIR / "response_schema.json"

TRANSACTION_SCHEMA = "optimizer_transaction/4"
RESULT_SCHEMA = "optimizer_result/2"

CANONICAL_METRICS = (
    "wns_ns",
    "tns_ns",
    "area_um2",
    "power_mw",
)

VALID_STAGES = (
    "synth",
    "global_route",
    "final",
)


# =============================================================================
# Errors
# =============================================================================

class OptimizerError(RuntimeError):
    """Infrastructure/setup failure in the transaction."""


class OptimizerRejected(RuntimeError):
    """Expected candidate rejection by a trusted gate."""


# nEBULA_PROMPT_V2_PATCH: deferred compact prompt rendering + append-only history
try:
    from prompt_renderer import (
        ContextBudgetError,
        PromptRenderError,
        render_request_for_llm,
    )
    from optimizer_history import append_history_record_from_transaction
except ImportError:
    from timing_automation.prompt_renderer import (
        ContextBudgetError,
        PromptRenderError,
        render_request_for_llm,
    )
    from timing_automation.optimizer_history import append_history_record_from_transaction


# =============================================================================
# Generic helpers
# =============================================================================

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _is_retryable_provider_error(exc: BaseException) -> bool:
    """Retry timeouts and provider 5xx only; never retry 4xx responses."""
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return 500 <= status <= 599
    name = type(exc).__name__.lower()
    message = str(exc).lower()
    return "timeout" in name or "timed out" in message or "timeout" in message


def _diagnosis_summary(paths: Sequence[Path]) -> Optional[str]:
    parts: list[str] = []
    for raw in paths:
        path = Path(raw).expanduser().resolve()
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, Mapping):
            continue
        for key in ("summary", "diagnosis", "reason", "counterexample_summary"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value.strip())
                break
    if not parts:
        return None
    return " | ".join(parts)[:2000]


def load_json(path: Path) -> Dict[str, Any]:
    path = Path(path).expanduser().resolve()

    if not path.is_file():
        raise OptimizerError(f"JSON file not found: {path}")

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise OptimizerError(
            f"invalid JSON in {path}: {exc}"
        ) from exc

    if not isinstance(value, dict):
        raise OptimizerError(
            f"JSON root must be an object: {path}"
        )

    return value


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            value,
            indent=2,
            sort_keys=False,
        )
        + "\n",
        encoding="utf-8",
    )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(Path(path).read_bytes())


def sanitize_tag(value: str) -> str:
    value = str(value).strip()

    if not value:
        raise OptimizerError("optimizer tag must not be empty")

    if not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
        raise OptimizerError(
            "optimizer tag may contain only letters, digits, '_', '-', and '.'"
        )

    return value


def default_tag() -> str:
    return (
        "cand_"
        + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        + "_"
        + uuid.uuid4().hex[:6]
    )


def artifact_stem(
    ai_runs: Path,
    tag: str,
) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", sanitize_tag(tag))
    return Path(ai_runs).resolve() / safe


def artifact_path(stem: Path, suffix: str) -> Path:
    return Path(str(stem) + suffix)


def _design_id(payload: Mapping[str, Any]) -> Optional[str]:
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

    return None


def _platform(payload: Mapping[str, Any]) -> Optional[str]:
    value = payload.get("platform")

    if isinstance(value, str) and value.strip():
        return value.strip()

    design = payload.get("design")
    if isinstance(design, Mapping):
        value = design.get("platform")
        if isinstance(value, str) and value.strip():
            return value.strip()

    return None


def _evaluation_stage(payload: Mapping[str, Any]) -> Optional[str]:
    evaluation = payload.get("evaluation")

    if isinstance(evaluation, Mapping):
        value = evaluation.get("stage")
        if isinstance(value, str) and value.strip():
            return value.strip()

    value = payload.get("stage")
    if isinstance(value, str) and value.strip():
        return value.strip()

    return None


def _recipe_fingerprint(payload: Mapping[str, Any]) -> Optional[str]:
    evaluation = payload.get("evaluation")

    if isinstance(evaluation, Mapping):
        value = evaluation.get("recipe_fingerprint_sha256")
        if isinstance(value, str) and value.strip():
            return value.strip().lower()

    physical = payload.get("physical_recipe")
    if isinstance(physical, Mapping):
        value = physical.get("fingerprint_sha256")
        if isinstance(value, str) and value.strip():
            return value.strip().lower()

    return None


def _metrics_object(payload: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    for key in ("metrics", "canonical_metrics", "canonical"):
        value = payload.get(key)
        if isinstance(value, Mapping):
            return value

    if all(key in payload for key in CANONICAL_METRICS):
        return payload

    return None


def validate_baseline_against_recipe(
    *,
    baseline: Mapping[str, Any],
    recipe: Any,
) -> None:
    stage = _evaluation_stage(baseline)
    fingerprint = _recipe_fingerprint(baseline)
    design_id = _design_id(baseline)
    platform = _platform(baseline)
    metrics = _metrics_object(baseline)

    problems = []

    if stage != recipe.stage:
        problems.append(
            f"baseline stage {stage!r} != recipe stage {recipe.stage!r}"
        )

    if fingerprint != str(recipe.fingerprint_sha256).lower():
        problems.append(
            "baseline physical-recipe fingerprint does not match "
            "the selected physical recipe"
        )

    if design_id != recipe.design_id:
        problems.append(
            f"baseline design {design_id!r} != recipe design {recipe.design_id!r}"
        )

    if platform != recipe.platform:
        problems.append(
            f"baseline platform {platform!r} != recipe platform {recipe.platform!r}"
        )

    if baseline.get("flow_complete") is False:
        problems.append("baseline metrics artifact is marked flow_complete=false")

    if not isinstance(metrics, Mapping):
        problems.append("baseline canonical metrics object is missing")
    else:
        missing = [
            key
            for key in CANONICAL_METRICS
            if metrics.get(key) is None
        ]
        if missing:
            problems.append(
                "baseline is missing canonical metrics: "
                + ", ".join(missing)
            )

    if problems:
        raise OptimizerError(
            "baseline/physical-recipe preflight failed: "
            + "; ".join(problems)
        )


def request_identity_problems(
    request: Mapping[str, Any],
    manifest: Any,
    recipe: Any,
) -> list[str]:
    problems: list[str] = []

    schema = request.get("schema")
    if schema != "optimizer_request/2":
        problems.append(
            f"request schema must be 'optimizer_request/2', got {schema!r}"
        )

    design = request.get("design")
    if isinstance(design, Mapping):
        req_design = (
            design.get("design_id")
            or design.get("name")
            or design.get("design")
        )
        req_top = design.get("top")
        req_platform = design.get("platform")

        if (
            isinstance(req_design, str)
            and req_design
            and req_design != manifest.design_id
        ):
            problems.append(
                f"request design {req_design!r} != manifest design "
                f"{manifest.design_id!r}"
            )

        if (
            isinstance(req_top, str)
            and req_top
            and req_top != manifest.top
        ):
            problems.append(
                f"request top {req_top!r} != manifest top {manifest.top!r}"
            )

        if (
            isinstance(req_platform, str)
            and req_platform
            and req_platform != recipe.platform
        ):
            problems.append(
                f"request platform {req_platform!r} != recipe platform "
                f"{recipe.platform!r}"
            )

    mode = request.get("optimizer_mode") or "guided"
    if mode not in ("guided", "exploratory"):
        problems.append(f"invalid optimizer_mode {mode!r}")

    bundle = request.get("source_bundle") or {}
    sources = bundle.get("sources") or []
    declared = {}
    if isinstance(sources, list):
        for item in sources:
            if isinstance(item, Mapping) and isinstance(item.get("path"), str) and isinstance(item.get("sha256"), str):
                declared[item["path"]] = item["sha256"].lower()
    expected = {str(k): str(v).lower() for k, v in manifest.source_hashes.items()}
    if not declared:
        problems.append("request has no complete SourceBundle identity")
    elif declared != expected:
        problems.append("request SourceBundle hash map does not match golden manifest")

    return problems


def classify_formal_result(
    result: Mapping[str, Any],
    exit_code: int,
) -> str:
    status = result.get("status")
    if exit_code == 0 and result.get("ok") is True and isinstance(status, str) and is_proven(status):
        return "pass"

    if status in {
        EquivalenceVerdict.NOT_PROVEN.value,
        EquivalenceVerdict.DISPROVEN.value,
    }:
        return "reject"

    return "error"


# =============================================================================
# Groq candidate generation
# =============================================================================

def schema_for_groq(path: Path) -> Dict[str, Any]:
    schema = load_json(path)
    schema = dict(schema)

    # Repository metadata, not JSON-Schema vocabulary.
    schema.pop("schema", None)

    if schema.get("type") != "object":
        raise OptimizerError(
            f"response schema must describe a JSON object: {path}"
        )

    return schema


def call_groq(
    *,
    system_prompt: str,
    user_prompt: str,
    model: str,
    max_tokens: int,
    reasoning_effort: str,
    temperature: float = 0.0,
    response_schema_path: Optional[Path] = None,
    max_attempts: int = 3,
    return_usage: bool = False,
) -> str | tuple[str, dict[str, Any]]:
    """Generate one candidate. Retry only timeout/5xx provider failures."""
    try:
        from groq import Groq
    except ImportError as exc:
        raise OptimizerError(
            "Groq SDK is missing. Install it in the active environment "
            "with `python -m pip install groq`."
        ) from exc

    if not os.environ.get("GROQ_API_KEY"):
        raise OptimizerError("GROQ_API_KEY is not set")
    if temperature < 0.0 or temperature > 2.0:
        raise OptimizerError("temperature must be between 0.0 and 2.0")
    if max_attempts < 1:
        raise OptimizerError("max_attempts must be >= 1")

    client = Groq()
    last_exc: Optional[BaseException] = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                reasoning_effort=reasoning_effort,
                include_reasoning=False,
                max_completion_tokens=max_tokens,
                temperature=temperature,
            )
            if not response.choices:
                raise OptimizerError("Groq returned no completion choices")
            content = response.choices[0].message.content
            if not content or not content.strip():
                choice = response.choices[0] if response.choices else None
                message = getattr(choice, "message", None) if choice else None
                reasoning = getattr(message, "reasoning", None) or ""
                usage = getattr(response, "usage", None)
                raise OptimizerError(
                    "Groq returned an empty candidate response; "
                    f"finish_reason={getattr(choice, 'finish_reason', None)!r}, "
                    f"prompt_tokens={getattr(usage, 'prompt_tokens', None)}, "
                    f"completion_tokens={getattr(usage, 'completion_tokens', None)}, "
                    f"total_tokens={getattr(usage, 'total_tokens', None)}, "
                    f"reasoning_chars={len(reasoning)}"
                )
            content = content.strip()
            if return_usage:
                usage = getattr(response, "usage", None)
                choice = response.choices[0] if response.choices else None
                return content, {
                    "prompt_tokens": getattr(usage, "prompt_tokens", None),
                    "completion_tokens": getattr(usage, "completion_tokens", None),
                    "total_tokens": getattr(usage, "total_tokens", None),
                    "finish_reason": getattr(choice, "finish_reason", None),
                    "provider_requests": 1,
                }
            return content
        except Exception as exc:
            last_exc = exc
            if isinstance(exc, OptimizerError) or not _is_retryable_provider_error(exc):
                raise
            if attempt >= max_attempts:
                break
            time.sleep(2 ** (attempt - 1))

    raise OptimizerError(
        f"Groq failed after {max_attempts} transient attempts: {last_exc}"
    ) from last_exc


# =============================================================================
# Artifact lifecycle
# =============================================================================

def known_artifacts(stem: Path) -> Tuple[Path, ...]:
    return (
        artifact_path(stem, ".candidate.json"),
        artifact_path(stem, ".candidate.v"),
        artifact_path(stem, ".lint.json"),
        artifact_path(stem, ".sec_bundle.json"),
        artifact_path(stem, ".verify.json"),
        artifact_path(stem, ".verify.ys"),
        artifact_path(stem, ".verify.log"),
        artifact_path(stem, ".metrics.json"),
        artifact_path(stem, ".evaluation.json"),
        artifact_path(stem, ".transaction.json"),
        artifact_path(stem, ".result.json"),
        Path(str(stem) + "_sources"),
        Path(str(stem) + "_accepted"),
    )


def prepare_artifacts(
    stem: Path,
    *,
    overwrite: bool,
) -> None:
    existing = [
        path
        for path in known_artifacts(stem)
        if path.exists()
    ]

    if existing and not overwrite:
        raise OptimizerError(
            "optimizer artifacts already exist for this tag; "
            "use a new --tag or --overwrite: "
            + ", ".join(str(path) for path in existing[:5])
        )

    if overwrite:
        for path in existing:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()


def snapshot_candidate(
    *,
    source: Path,
    destination: Path,
) -> Path:
    source = Path(source).expanduser().resolve()
    destination = Path(destination).resolve()

    if not source.is_file():
        raise OptimizerError(f"candidate file not found: {source}")

    data = source.read_bytes()
    destination.parent.mkdir(parents=True, exist_ok=True)

    if source != destination:
        destination.write_bytes(data)

    return destination


# =============================================================================
# Transaction finalization
# =============================================================================

def _candidate_patch_summary(transaction: Mapping[str, Any]) -> Dict[str, Any]:
    candidate = transaction.get("candidate") or {}
    artifact = candidate.get("artifact") if isinstance(candidate, Mapping) else None
    if not isinstance(artifact, str) or not Path(artifact).is_file():
        return {}
    try:
        payload = load_json(Path(artifact))
    except Exception:
        return {}
    patches = payload.get("patches") if isinstance(payload.get("patches"), list) else []
    return {
        "chosen_pattern_id": payload.get("chosen_pattern_id"),
        "target_unit_id": payload.get("target_unit_id"),
        "patch_count": len(patches),
        "output_format": "complete_verilog" if payload.get("schema_version") == "3.0" else "legacy_patches",
        "source_file_count": len(payload.get("source_files") or []),
        "changed_ranges": [
            p.get("changed_source_range") for p in patches
            if isinstance(p, Mapping) and isinstance(p.get("changed_source_range"), Mapping)
        ],
    }


def _stop_hint(transaction: Mapping[str, Any], verdict: str) -> str:
    if verdict == "promote":
        return "reanalyze_promoted_rtl"
    if verdict == "equivalent_no_ppa":
        return "run_physical_evaluation"
    if verdict == "error":
        return "fix_infrastructure_and_retry"
    formal = (transaction.get("stages") or {}).get("formal") or {}
    status = str(formal.get("status") or "")
    if status == EquivalenceVerdict.NOT_PROVEN.value:
        return "increase_proof_effort_or_change_verification_strategy"
    if status == EquivalenceVerdict.DISPROVEN.value:
        return "reject_implementation_and_try_alternative"
    evaluation = (transaction.get("stages") or {}).get("evaluation") or {}
    if evaluation:
        return "try_alternative_pattern_or_rewrite"
    return "retry_with_diagnosis"


def _build_verdict_block(
    transaction: Mapping[str, Any],
    *,
    verdict: str,
    note: str,
) -> Dict[str, Any]:
    metrics_stage = (transaction.get("stages") or {}).get("metrics") or {}
    return {
        "verdict": verdict,
        "reason": note,
        "stop_hint": _stop_hint(transaction, verdict),
        "metrics": dict(metrics_stage.get("metrics") or {}),
        "patch_summary": _candidate_patch_summary(transaction),
    }


def _snapshot_promoted_bundle(transaction: Dict[str, Any], stem: Path) -> None:
    bundle = transaction.get("accepted_candidate_bundle")
    if not isinstance(bundle, Mapping):
        return
    src_text = bundle.get("directory")
    if not isinstance(src_text, str):
        return
    src = Path(src_text).expanduser().resolve()
    if not src.is_dir():
        return
    dst = Path(str(stem) + "_accepted").resolve()
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    manifest = dst / "design_manifest.json"
    transaction["promoted_candidate_bundle"] = {
        "directory": str(dst),
        "manifest": str(manifest) if manifest.is_file() else None,
    }


def finish_transaction(
    *,
    transaction: Dict[str, Any],
    stem: Path,
    verdict: str,
    note: str,
    exit_code: int,
) -> Tuple[int, Dict[str, Any]]:
    transaction["finished_utc"] = utc_now()
    transaction["verdict"] = verdict
    transaction["note"] = note
    if verdict == "promote":
        _snapshot_promoted_bundle(transaction, stem)
    transaction["verdict_block"] = _build_verdict_block(
        transaction, verdict=verdict, note=note
    )

    # nEBULA prompt-v2 append-only history.  History logging is compact and
    # must not alter an already-determined optimization verdict.
    try:
        appended = append_history_record_from_transaction(transaction)
        if isinstance(transaction.get("iteration_history"), dict):
            transaction["iteration_history"]["appended"] = bool(appended)
    except Exception as exc:
        if isinstance(transaction.get("iteration_history"), dict):
            transaction["iteration_history"]["append_error"] = str(exc)

    transaction_path = artifact_path(
        stem,
        ".transaction.json",
    )

    result_path = artifact_path(
        stem,
        ".result.json",
    )

    write_json(
        transaction_path,
        transaction,
    )

    result = {
        "schema": RESULT_SCHEMA,
        "tag": transaction.get("tag"),
        "verdict": verdict,
        "note": note,
        "design": transaction.get("design"),
        "stage": transaction.get("stage"),
        "candidate": transaction.get("candidate"),
        "accepted_candidate_bundle": transaction.get(
            "accepted_candidate_bundle"
        ),
        "promoted_candidate_bundle": transaction.get(
            "promoted_candidate_bundle"
        ),
        "verdict_block": transaction.get("verdict_block"),
        "reproducibility": transaction.get("reproducibility"),
        "verification": transaction.get(
            "artifacts",
            {},
        ).get("verification"),
        "metrics": transaction.get(
            "artifacts",
            {},
        ).get("candidate_metrics"),
        "evaluation": transaction.get(
            "artifacts",
            {},
        ).get("evaluation"),
        "transaction": str(transaction_path),
    }

    write_json(
        result_path,
        result,
    )

    print("\n" + "=" * 72)
    print("nEBULA SINGLE-SHOT OPTIMIZER")
    print("=" * 72)
    print(f"Verdict      : {verdict}")
    print(f"Note         : {note}")
    print(f"Transaction  : {transaction_path}")
    print(f"Result       : {result_path}")

    evaluation = transaction.get("stages", {}).get("evaluation")
    if isinstance(evaluation, Mapping):
        decision = evaluation.get("decision")
        promote_to = evaluation.get("promote_to")
        if decision is not None:
            print(f"PPA decision : {decision}")
        if promote_to is not None:
            print(f"Promote to   : {promote_to}")

    print("=" * 72)

    return exit_code, transaction


# =============================================================================
# Core transaction
# =============================================================================

def run_single_transaction(
    *,
    request_path: Path,
    golden_manifest_path: Path,
    golden_base_dir: Path,
    physical_recipe_path: Path,
    baseline_metrics_path: Path,
    flow_dir: Path,
    ai_runs: Path,
    tag: str,
    candidate_path: Optional[Path],
    model: str,
    max_tokens: int,
    reasoning_effort: str,
    response_schema_path: Path,
    frozen_ranges: Sequence[Tuple[int, int]],
    yosys_exe: Optional[str],
    formal_mode: Optional[str],
    formal_backend: Optional[str],
    seq_depth: Optional[int],
    timeout_s: Optional[float],
    policy: PromotionPolicy,
    no_orfs: bool,
    overwrite: bool,
    temperature: float = 0.0,
    history_file: Optional[Path] = None,
    previous_diagnosis_paths: Sequence[Path] = (),
) -> Tuple[int, Dict[str, Any]]:
    flow_dir = Path(flow_dir).expanduser().resolve()
    ai_runs = Path(ai_runs).expanduser().resolve()
    golden_base_dir = Path(golden_base_dir).expanduser().resolve()

    tag = sanitize_tag(tag)
    stem = artifact_stem(ai_runs, tag)

    ai_runs.mkdir(
        parents=True,
        exist_ok=True,
    )

    prepare_artifacts(
        stem,
        overwrite=overwrite,
    )

    transaction: Dict[str, Any] = {
        "schema": TRANSACTION_SCHEMA,
        "started_utc": utc_now(),
        "tag": tag,
        "verdict": "running",
        "stages": {},
        "artifacts": {},
        "reproducibility": {
            "model": None if candidate_path is not None else model,
            "temperature": None if candidate_path is not None else float(temperature),
            "reasoning_effort": None if candidate_path is not None else reasoning_effort,
            "request_sha256": sha256_file(Path(request_path)),
            "prompt_sha256": None,
        },
    }

    try:
        # ------------------------------------------------------------------
        # 0. Preflight
        # ------------------------------------------------------------------

        print("[0] preflight ...")

        request = load_json(request_path)
        baseline = load_json(baseline_metrics_path)

        manifest = DesignManifest.read_json(
            Path(golden_manifest_path)
        )
        manifest.validate(
            require_source_hashes=True
        )

        recipe = PhysicalRecipe.read_json(
            Path(physical_recipe_path)
        )
        recipe.validate()

        recipe_verification = (
            recipe.verify_frozen_files()
        )

        if recipe_verification.get("ok") is not True:
            raise OptimizerError(
                "physical recipe frozen-file verification failed"
            )

        if manifest.design_id != recipe.design_id:
            raise OptimizerError(
                "golden manifest / physical recipe design mismatch: "
                f"{manifest.design_id!r} != {recipe.design_id!r}"
            )

        if manifest.top != recipe.top:
            raise OptimizerError(
                "golden manifest / physical recipe top mismatch: "
                f"{manifest.top!r} != {recipe.top!r}"
            )

        if recipe.stage not in VALID_STAGES:
            raise OptimizerError(
                f"unsupported physical recipe stage {recipe.stage!r}"
            )

        if (
            Path(recipe.source_base_dir)
            .expanduser()
            .resolve()
            != flow_dir
        ):
            raise OptimizerError(
                "physical recipe source_base_dir does not match --flow-dir"
            )

        validate_baseline_against_recipe(
            baseline=baseline,
            recipe=recipe,
        )

        identity_problems = (
            request_identity_problems(
                request,
                manifest,
                recipe,
            )
        )

        if identity_problems:
            raise OptimizerError(
                "request identity preflight failed: "
                + "; ".join(identity_problems)
            )

        if not no_orfs and not (flow_dir / "Makefile").is_file():
            raise OptimizerError(
                f"ORFS Makefile not found: {flow_dir / 'Makefile'}"
            )

        qualification = qualify_dataset(manifest=manifest, base_dir=golden_base_dir,
                                        yosys_exe=yosys_exe, require_timing=True)
        if not qualification.get("qualified"):
            raise OptimizerError("QUALIFICATION_REJECT: " + "; ".join(qualification.get("reasons", [])))

        frozen_physical_variables(recipe)
        verify_frozen_constraints(manifest, recipe)

        rendered_prompt = None
        if candidate_path is None:
            if not Path(response_schema_path).is_file():
                raise OptimizerError(
                    f"response schema not found: {response_schema_path}"
                )

            try:
                rendered_prompt = render_request_for_llm(
                    Path(request_path),
                    response_schema_override=Path(response_schema_path),
                    completion_reserve_tokens=max_tokens,
                    previous_diagnosis_paths=tuple(Path(x) for x in previous_diagnosis_paths),
                )
            except ContextBudgetError as exc:
                raise OptimizerError(str(exc)) from exc
            except PromptRenderError as exc:
                raise OptimizerError(
                    f"prompt rendering failed: {exc}"
                ) from exc

            transaction["context_budget"] = dict(
                rendered_prompt.context_budget
            )
            transaction["reproducibility"]["prompt_sha256"] = sha256_text(
                rendered_prompt.system_prompt + "\0" + rendered_prompt.user_prompt
            )
            cb = transaction["context_budget"]
            print(
                "    Context      : "
                f"input~{cb['estimated_input_tokens']} + "
                f"reserve={cb['completion_reserve_tokens']} / "
                f"budget={cb['service_model_token_budget']} tokens"
            )
            print(
                "    Compaction   : "
                + ("applied" if rendered_prompt.compaction_applied else "not needed")
            )

        transaction["unit_id"] = request.get("unit_id")
        transaction["optimizer_mode"] = request.get("optimizer_mode") or "guided"
        transaction["diagnostic_confidence"] = request.get("diagnostic_confidence")
        transaction["routing_reason"] = request.get("routing_reason")
        transaction["source_bundle_sha256"] = (request.get("source_bundle") or {}).get("bundle_sha256")
        rendering_inputs = request.get("rendering_inputs") or {}
        current_iteration = rendering_inputs.get("current_iteration") or {}
        history_ref = rendering_inputs.get("iteration_history") or {}
        if isinstance(history_ref.get("path"), str):
            transaction["iteration_history"] = {
                "path": history_ref.get("path"),
                "iteration_number": current_iteration.get("iteration_number"),
                "append_only": True,
            }
        if history_file is not None:
            iteration_number = current_iteration.get("iteration_number")
            if not isinstance(iteration_number, int) or iteration_number < 1:
                iteration_number = 1
            transaction["iteration_history"] = {
                "path": str(Path(history_file).expanduser().resolve()),
                "iteration_number": iteration_number,
                "append_only": True,
            }
        diagnosis = _diagnosis_summary(tuple(Path(x) for x in previous_diagnosis_paths))
        if diagnosis:
            transaction["diagnosis_summary"] = diagnosis

        transaction["design"] = {
            "dataset": manifest.dataset,
            "design_id": manifest.design_id,
            "top": manifest.top,
            "platform": recipe.platform,
        }
        transaction["stage"] = recipe.stage
        transaction["physical_recipe"] = {
            "path": str(
                Path(physical_recipe_path).resolve()
            ),
            "recipe_id": recipe.recipe_id,
            "fingerprint_sha256": (
                recipe.fingerprint_sha256.lower()
            ),
            "verified": True,
        }
        transaction["baseline_metrics"] = str(
            Path(baseline_metrics_path).resolve()
        )

        transaction["stages"]["preflight"] = {
            "ok": True,
            "request_schema": request.get("schema"),
            "recipe_verified": True,
            "baseline_matches_recipe": True,
        }

        # ------------------------------------------------------------------
        # 1. Candidate generation/reuse
        # ------------------------------------------------------------------

        candidate_artifact = artifact_path(
            stem,
            ".candidate.json",
        )

        if candidate_path is not None:
            print("[1] snapshotting supplied candidate ...")

            candidate_artifact = snapshot_candidate(
                source=candidate_path,
                destination=candidate_artifact,
            )

            transaction["stages"]["candidate"] = {
                "ok": True,
                "source": "reused",
                "input": str(
                    Path(candidate_path).resolve()
                ),
                "artifact": str(candidate_artifact),
            }
            transaction["model"] = None

        else:
            print("[1] generating one Groq candidate ...")

            if rendered_prompt is None:
                raise OptimizerError(
                    "internal error: compact prompt was not rendered during preflight"
                )

            raw = call_groq(
                system_prompt=rendered_prompt.system_prompt,
                user_prompt=rendered_prompt.user_prompt,
                model=model,
                max_tokens=max_tokens,
                reasoning_effort=reasoning_effort,
                temperature=temperature,
                response_schema_path=Path(response_schema_path),
            )

            candidate_artifact.write_text(
                raw + "\n",
                encoding="utf-8",
            )

            transaction["stages"]["candidate"] = {
                "ok": True,
                "source": "groq",
                "model": model,
                "reasoning_effort": reasoning_effort,
                "temperature": float(temperature),
                "artifact": str(candidate_artifact),
            }
            transaction["model"] = model

        candidate_sha = sha256_file(
            candidate_artifact
        )

        transaction["candidate"] = {
            "artifact": str(candidate_artifact),
            "sha256": candidate_sha,
        }

        # ------------------------------------------------------------------
        # 2. Deterministic lint / proof-setup gate
        # ------------------------------------------------------------------

        print("[2] deterministic lint + proof-setup gate ...")

        candidate_sources = Path(
            str(stem) + "_sources"
        )

        lint_result = lint_candidate(
            manifest=manifest,
            base_dir=golden_base_dir,
            request=request,
            candidate_text=candidate_artifact.read_text(
                encoding="utf-8"
            ),
            frozen_ranges=frozen_ranges,
            yosys_exe=yosys_exe,
            candidate_out_dir=candidate_sources,
        )

        lint_path = artifact_path(
            stem,
            ".lint.json",
        )

        write_json(
            lint_path,
            lint_result,
        )

        transaction["artifacts"]["lint"] = str(
            lint_path
        )
        transaction["stages"]["lint"] = {
            "ok": lint_result.get("ok") is True,
            "checks": lint_result.get("checks"),
            "reasons": lint_result.get("reasons"),
        }

        if lint_result.get("ok") is not True:
            return finish_transaction(
                transaction=transaction,
                stem=stem,
                verdict="reject",
                note="candidate rejected by deterministic lint/proof-setup gate",
                exit_code=1,
            )

        candidate_manifest_path = (
            candidate_sources
            / "design_manifest.json"
        )

        if not candidate_manifest_path.is_file():
            raise OptimizerError(
                "lint passed but accepted candidate manifest "
                f"was not materialized: {candidate_manifest_path}"
            )

        candidate_manifest = (
            DesignManifest.read_json(
                candidate_manifest_path
            )
        )

        transaction["accepted_candidate_bundle"] = {
            "directory": str(candidate_sources),
            "manifest": str(
                candidate_manifest_path
            ),
        }

        # Expose complete source artifacts for direct use, in addition to the manifest.
        if getattr(candidate_manifest, "source_files", None):
            verilog_files = [str(candidate_manifest.resolve_path(name, candidate_sources))
                             for name in candidate_manifest.source_files]
            transaction["candidate"]["verilog_files"] = verilog_files
            if len(verilog_files) == 1:
                standalone = artifact_path(stem, ".candidate.v")
                shutil.copyfile(verilog_files[0], standalone)
                transaction["candidate"]["complete_verilog"] = str(standalone)

        # Candidate input artifact itself may not mutate after lint.
        if sha256_file(candidate_artifact) != candidate_sha:
            raise OptimizerError(
                "candidate artifact changed during lint"
            )

        # ------------------------------------------------------------------
        # 3. Canonical SEC bundle
        # ------------------------------------------------------------------

        print("[3] building SEC bundle ...")

        sec_bundle = build_sec_bundle(
            golden_manifest=manifest,
            golden_base_dir=golden_base_dir,
            candidate_manifest=candidate_manifest,
            candidate_base_dir=candidate_sources,
            mode=formal_mode,
            backend=formal_backend,
        )

        sec_bundle_path = artifact_path(
            stem,
            ".sec_bundle.json",
        )

        sec_bundle.write_json(
            sec_bundle_path
        )

        transaction["artifacts"]["sec_bundle"] = str(
            sec_bundle_path
        )
        transaction["stages"]["sec_bundle"] = {
            "ok": True,
            "mode": sec_bundle.mode,
            "backend": sec_bundle.backend,
            "execution_ready": sec_bundle.execution_ready,
        }

        # ------------------------------------------------------------------
        # 4. Formal equivalence
        # ------------------------------------------------------------------

        print("[4] formal equivalence ...")

        verify_path = artifact_path(
            stem,
            ".verify.json",
        )

        verification, verify_exit = (
            run_verification(
                sec_bundle,
                output_path=verify_path,
                yosys_exe=yosys_exe,
                seq_depth_override=seq_depth,
                timeout_override_s=timeout_s,
            )
        )

        verification["bundle"] = str(
            sec_bundle_path.resolve()
        )

        write_verify_json(
            verify_path,
            verification,
        )

        transaction["artifacts"]["verification"] = str(
            verify_path
        )

        formal_class = classify_formal_result(
            verification,
            verify_exit,
        )

        transaction["stages"]["formal"] = {
            "ok": formal_class == "pass",
            "classification": formal_class,
            "returncode": verify_exit,
            "status": verification.get("status"),
            "reason": verification.get("reason"),
        }

        if formal_class == "reject":
            return finish_transaction(
                transaction=transaction,
                stem=stem,
                verdict="reject",
                note=describe_equivalence(
                    str(verification.get("status")),
                    method=(verification.get("verification") or {}).get("method"),
                    depth=(verification.get("verification") or {}).get("induction_depth"),
                ),
                exit_code=1,
            )

        if formal_class == "error":
            raise OptimizerError(
                "formal verification infrastructure/setup failed: "
                f"status={verification.get('status')!r}, "
                f"reason={verification.get('reason')!r}"
            )

        # Protect the exact accepted source bundle after proof.
        candidate_manifest.validate(
            require_source_hashes=True
        )

        if no_orfs:
            transaction["stages"]["physical"] = {
                "status": "skipped",
                "reason": "--no-orfs",
            }

            return finish_transaction(
                transaction=transaction,
                stem=stem,
                verdict="equivalent_no_ppa",
                note=(
                    "deterministic lint and formal equivalence passed; "
                    "physical evaluation intentionally skipped"
                ),
                exit_code=0,
            )

        # ------------------------------------------------------------------
        # 5. Candidate ORFS under frozen recipe
        # ------------------------------------------------------------------

        print(
            f"[5] candidate ORFS ({recipe.stage}) under frozen recipe ..."
        )

        flow_result = run_candidate_flow(
            candidate_manifest_path=(
                candidate_manifest_path
            ),
            candidate_base_dir=(
                candidate_sources
            ),
            physical_recipe_path=Path(
                physical_recipe_path
            ),
            candidate_id=tag,
            flow_dir=flow_dir,
            flow_variant=None,
            report_checks_tcl=None,
            dry_run=False,
        )

        if flow_result.get("ok") is not True:
            raise OptimizerError(
                "candidate ORFS runner returned ok=false"
            )

        flow_variant = flow_result.get(
            "flow_variant"
        )

        if (
            not isinstance(flow_variant, str)
            or not flow_variant
        ):
            raise OptimizerError(
                "candidate ORFS runner did not return a flow_variant"
            )

        transaction["stages"]["physical"] = {
            "ok": True,
            "flow_variant": flow_variant,
            "stage": recipe.stage,
            "combined_input_sha256": (
                flow_result.get(
                    "combined_input_sha256"
                )
            ),
            "expected_stage_json": (
                flow_result.get(
                    "expected_stage_json"
                )
            ),
        }
        transaction["candidate_flow_variant"] = (
            flow_variant
        )

        # ------------------------------------------------------------------
        # 6. Stage-aware canonical metrics
        # ------------------------------------------------------------------

        print("[6] collecting stage-aware candidate PPA ...")

        candidate_metrics_result = collect_metrics(
            variant=flow_variant,
            design=recipe.design_id,
            platform=recipe.platform,
            flow_dir=flow_dir,
            physical_recipe=recipe,
        )

        if candidate_metrics_result.get(
            "flow_complete"
        ) is not True:
            raise OptimizerError(
                "candidate metric collection is incomplete; "
                "missing="
                + ", ".join(
                    str(x)
                    for x in candidate_metrics_result.get(
                        "missing_required_metrics",
                        [],
                    )
                )
            )

        candidate_metrics_path = artifact_path(
            stem,
            ".metrics.json",
        )

        save_metrics(
            candidate_metrics_result,
            flow_dir,
            candidate_metrics_path,
        )

        transaction["artifacts"]["candidate_metrics"] = str(
            candidate_metrics_path
        )
        transaction["stages"]["metrics"] = {
            "ok": True,
            "stage": (
                candidate_metrics_result.get(
                    "evaluation",
                    {},
                ).get("stage")
            ),
            "recipe_fingerprint_sha256": (
                candidate_metrics_result.get(
                    "evaluation",
                    {},
                ).get(
                    "recipe_fingerprint_sha256"
                )
            ),
            "metrics": candidate_metrics_result.get(
                "metrics"
            ),
        }

        # ------------------------------------------------------------------
        # 7. Promotion/rejection policy
        # ------------------------------------------------------------------

        print("[7] evaluating candidate against frozen baseline ...")

        evaluation = evaluate_candidate(
            requested_stage=recipe.stage,
            baseline_payload=baseline,
            candidate_payload=(
                candidate_metrics_result
            ),
            verification_payload=verification,
            policy=policy,
        )

        evaluation_path = artifact_path(
            stem,
            ".evaluation.json",
        )

        write_json(
            evaluation_path,
            evaluation,
        )

        transaction["artifacts"]["evaluation"] = str(
            evaluation_path
        )
        transaction["stages"]["evaluation"] = {
            "ok": evaluation.get("ok") is True,
            "decision": evaluation.get(
                "decision"
            ),
            "promote": evaluation.get(
                "promote"
            ),
            "promote_to": evaluation.get(
                "promote_to"
            ),
            "checks": evaluation.get(
                "checks"
            ),
            "reasons": evaluation.get(
                "reasons"
            ),
            "delta": evaluation.get(
                "delta"
            ),
        }

        if evaluation.get("promote") is True:
            return finish_transaction(
                transaction=transaction,
                stem=stem,
                verdict="promote",
                note=(
                    "candidate passed deterministic lint, formal equivalence, "
                    f"and {recipe.stage} PPA promotion policy"
                ),
                exit_code=0,
            )

        return finish_transaction(
            transaction=transaction,
            stem=stem,
            verdict="reject",
            note=(
                "candidate passed lint/formal but failed "
                f"{recipe.stage} PPA promotion policy"
            ),
            exit_code=1,
        )

    except (
        OptimizerError,
        CandidateFlowError,
        SECBundleError,
        VerificationSetupError,
        VerificationBackendError,
        EvaluationInputError,
        ValueError,
        OSError,
    ) as exc:
        transaction["stages"].setdefault(
            "error",
            {
                "ok": False,
                "type": type(exc).__name__,
                "message": str(exc),
            },
        )

        return finish_transaction(
            transaction=transaction,
            stem=stem,
            verdict="error",
            note=f"{type(exc).__name__}: {exc}",
            exit_code=2,
        )

    except Exception as exc:
        transaction["stages"]["error"] = {
            "ok": False,
            "type": type(exc).__name__,
            "message": str(exc),
        }

        return finish_transaction(
            transaction=transaction,
            stem=stem,
            verdict="error",
            note=f"{type(exc).__name__}: {exc}",
            exit_code=2,
        )


# =============================================================================
# Self-test
# =============================================================================

def run_self_test() -> None:
    """
    Exercise the orchestration itself without Yosys/Groq/ORFS.

    The individual heavy modules have their own real self-tests.  Here we
    replace those service calls with deterministic fakes and prove that:
      - gate ordering is lint -> SEC -> formal -> ORFS -> metrics -> evaluator
      - a lint failure stops before formal/ORFS
      - a formal reject stops before ORFS
      - a PPA promote returns exit 0 and retains the accepted bundle
      - baseline/recipe fingerprint mismatch is rejected in preflight
    """

    import types

    global verify_frozen_constraints
    global qualify_dataset
    global DesignManifest
    global PhysicalRecipe
    global lint_candidate
    global build_sec_bundle
    global run_verification
    global run_candidate_flow
    global collect_metrics
    global save_metrics
    global evaluate_candidate
    global write_verify_json

    originals = {
        "verify_frozen_constraints": verify_frozen_constraints,
        "qualify_dataset": qualify_dataset,
        "DesignManifest": DesignManifest,
        "PhysicalRecipe": PhysicalRecipe,
        "lint_candidate": lint_candidate,
        "build_sec_bundle": build_sec_bundle,
        "run_verification": run_verification,
        "run_candidate_flow": run_candidate_flow,
        "collect_metrics": collect_metrics,
        "save_metrics": save_metrics,
        "evaluate_candidate": evaluate_candidate,
        "write_verify_json": write_verify_json,
    }

    class FakeManifest:
        dataset = "self_test"
        design_id = "demo"
        top = "top"
        source_hashes = {"rtl/top.v": "f" * 64}

        def validate(
            self,
            require_source_hashes: bool = True,
        ) -> None:
            return None

    class FakeManifestReader:
        @staticmethod
        def read_json(path: Path):
            return FakeManifest()

    class FakeRecipe:
        flow_variables = {"DIE_AREA": "0 0 100 100", "CORE_AREA": "2 2 98 98"}
        recipe_id = "demo_grt_v1"
        design_id = "demo"
        top = "top"
        platform = "nangate45"
        stage = "global_route"
        fingerprint_sha256 = "a" * 64

        def __init__(self, source_base_dir: str):
            self.source_base_dir = source_base_dir

        def validate(self) -> None:
            return None

        def verify_frozen_files(self):
            return {
                "ok": True,
                "files": [],
            }

    class FakeRecipeReader:
        current = None

        @classmethod
        def read_json(cls, path: Path):
            return cls.current

    class FakeBundle:
        mode = "sequential_equivalence"
        backend = "yosys"
        execution_ready = True

        def write_json(self, path: Path) -> None:
            write_json(
                path,
                {
                    "schema": "sec_bundle/1",
                    "mode": self.mode,
                    "backend": self.backend,
                },
            )

    with tempfile.TemporaryDirectory(
        prefix="nebula_run_optimizer_"
    ) as tmp:
        root = Path(tmp).resolve()
        flow_dir = root / "flow"
        ai_runs = flow_dir / "ai_runs"

        flow_dir.mkdir(
            parents=True,
            exist_ok=True,
        )
        ai_runs.mkdir(
            parents=True,
            exist_ok=True,
        )

        (flow_dir / "Makefile").write_text(
            "# self-test\n",
            encoding="utf-8",
        )

        request_path = root / "request.json"
        manifest_path = root / "manifest.json"
        recipe_path = root / "recipe.json"
        baseline_path = root / "baseline.json"
        candidate_input = root / "candidate.json"

        request_path.write_text(
            json.dumps(
                {
                    "schema": "optimizer_request/2",
                    "unit_id": "unit_000",
                    "optimizer_mode": "guided",
                    "source_bundle": {
                        "schema": "nebula_source_bundle_identity/1",
                        "bundle_sha256": "e" * 64,
                        "sources": [
                            {
                                "path": "rtl/top.v",
                                "sha256": "f" * 64,
                                "modules": ["top"],
                                "text": "module top; endmodule\n",
                            }
                        ],
                        "module_to_file": {"top": "rtl/top.v"},
                    },
                }
            ),
            encoding="utf-8",
        )

        manifest_path.write_text(
            "{}\n",
            encoding="utf-8",
        )
        recipe_path.write_text(
            "{}\n",
            encoding="utf-8",
        )
        candidate_input.write_text(
            '{"schema_version":"2.0"}\n',
            encoding="utf-8",
        )

        baseline_payload = {
            "schema": "ppa_metrics/2",
            "design": "demo",
            "platform": "nangate45",
            "flow_variant": "baseline",
            "evaluation": {
                "stage": "global_route",
                "recipe_fingerprint_sha256": "a" * 64,
            },
            "flow_complete": True,
            "metrics": {
                "wns_ns": 0.1,
                "tns_ns": 0.0,
                "area_um2": 1000.0,
                "power_mw": 1.0,
            },
        }

        write_json(
            baseline_path,
            baseline_payload,
        )

        events: list[str] = []
        lint_should_pass = True
        formal_should_pass = True

        def fake_lint_candidate(
            *,
            manifest,
            base_dir,
            request,
            candidate_text,
            frozen_ranges,
            yosys_exe,
            candidate_out_dir,
        ):
            events.append("lint")

            if not lint_should_pass:
                return {
                    "schema": "lint_candidate/2",
                    "ok": False,
                    "checks": {
                        "state_boundary_unchanged": False,
                    },
                    "reasons": ["self-test lint reject"],
                }

            candidate_out_dir.mkdir(
                parents=True,
                exist_ok=True,
            )
            (candidate_out_dir / "design_manifest.json").write_text(
                "{}\n",
                encoding="utf-8",
            )

            return {
                "schema": "lint_candidate/2",
                "ok": True,
                "checks": {
                    "state_boundary_unchanged": True,
                },
                "reasons": [],
            }

        def fake_build_sec_bundle(**kwargs):
            events.append("sec_bundle")
            return FakeBundle()

        def fake_run_verification(
            bundle,
            *,
            output_path,
            yosys_exe,
            seq_depth_override,
            timeout_override_s,
        ):
            events.append("formal")

            if formal_should_pass:
                return (
                    {
                        "tool": "verify_rtl.py",
                        "schema": "rtl_verify/2",
                        "ok": True,
                        "status": EquivalenceVerdict.PROVEN_BOUNDED.value,
                        "reason": "self-test",
                        "verification": {
                            "backend": "yosys",
                            "canonical_backend": "yosys",
                            "mode": "sequential_equivalence",
                        },
                    },
                    0,
                )

            return (
                {
                    "tool": "verify_rtl.py",
                    "schema": "rtl_verify/2",
                    "ok": False,
                    "status": EquivalenceVerdict.DISPROVEN.value,
                    "reason": "self-test",
                    "verification": {
                        "backend": "yosys",
                        "canonical_backend": "yosys",
                        "mode": "sequential_equivalence",
                    },
                },
                3,
            )

        def fake_write_verify_json(path, value):
            write_json(path, value)

        def fake_run_candidate_flow(**kwargs):
            events.append("orfs")
            return {
                "ok": True,
                "flow_variant": "cand_selftest_variant",
                "combined_input_sha256": "b" * 64,
                "expected_stage_json": "5_1_grt.json",
            }

        candidate_metrics_payload = {
            "schema": "ppa_metrics/2",
            "design": "demo",
            "platform": "nangate45",
            "flow_variant": "cand_selftest_variant",
            "evaluation": {
                "stage": "global_route",
                "recipe_fingerprint_sha256": "a" * 64,
            },
            "flow_complete": True,
            "metrics": {
                "wns_ns": 0.11,
                "tns_ns": 0.0,
                "area_um2": 1000.0,
                "power_mw": 1.0,
            },
        }

        def fake_collect_metrics(**kwargs):
            events.append("metrics")
            return dict(candidate_metrics_payload)

        def fake_save_metrics(
            result,
            flow_dir,
            output_file=None,
        ):
            output_file = Path(output_file)
            write_json(output_file, result)
            return output_file

        def fake_evaluate_candidate(**kwargs):
            events.append("evaluate")
            return {
                "tool": "evaluate_candidate.py",
                "schema": "candidate_evaluation/1",
                "ok": True,
                "decision": "promote",
                "promote": True,
                "stage": "global_route",
                "promote_to": "final",
                "checks": {
                    "pareto_improvement": True,
                },
                "reasons": [],
                "delta": {
                    "wns_ns": 0.01,
                },
            }

        try:
            verify_frozen_constraints = lambda *args: None
            qualify_dataset = lambda **kwargs: {"qualified": True}
            DesignManifest = FakeManifestReader
            PhysicalRecipe = FakeRecipeReader
            FakeRecipeReader.current = FakeRecipe(
                str(flow_dir)
            )
            lint_candidate = fake_lint_candidate
            build_sec_bundle = fake_build_sec_bundle
            run_verification = fake_run_verification
            run_candidate_flow = fake_run_candidate_flow
            collect_metrics = fake_collect_metrics
            save_metrics = fake_save_metrics
            evaluate_candidate = fake_evaluate_candidate
            write_verify_json = fake_write_verify_json

            policy = PromotionPolicy()

            # ----------------------------------------------------------
            # Full promote path.
            # ----------------------------------------------------------

            events.clear()
            lint_should_pass = True
            formal_should_pass = True

            rc, txn = run_single_transaction(
                request_path=request_path,
                golden_manifest_path=manifest_path,
                golden_base_dir=root,
                physical_recipe_path=recipe_path,
                baseline_metrics_path=baseline_path,
                flow_dir=flow_dir,
                ai_runs=ai_runs,
                tag="selftest_promote",
                candidate_path=candidate_input,
                model="unused",
                max_tokens=100,
                reasoning_effort="low",
                response_schema_path=root / "unused_schema.json",
                frozen_ranges=(),
                yosys_exe=None,
                formal_mode=None,
                formal_backend=None,
                seq_depth=None,
                timeout_s=None,
                policy=policy,
                no_orfs=False,
                overwrite=False,
            )

            assert rc == 0
            assert txn["verdict"] == "promote"
            assert txn["verdict_block"]["verdict"] == "promote"
            assert txn["verdict_block"]["stop_hint"] == "reanalyze_promoted_rtl"
            assert txn["reproducibility"]["request_sha256"] == sha256_file(request_path)
            assert txn["reproducibility"]["model"] is None
            promoted = txn.get("promoted_candidate_bundle") or {}
            assert Path(promoted.get("directory", "")).is_dir()
            assert Path(promoted.get("manifest", "")).is_file()
            assert events == [
                "lint",
                "sec_bundle",
                "formal",
                "orfs",
                "metrics",
                "evaluate",
            ]

            # ----------------------------------------------------------
            # Lint reject: no SEC/formal/ORFS.
            # ----------------------------------------------------------

            events.clear()
            lint_should_pass = False
            formal_should_pass = True

            rc, txn = run_single_transaction(
                request_path=request_path,
                golden_manifest_path=manifest_path,
                golden_base_dir=root,
                physical_recipe_path=recipe_path,
                baseline_metrics_path=baseline_path,
                flow_dir=flow_dir,
                ai_runs=ai_runs,
                tag="selftest_lint_reject",
                candidate_path=candidate_input,
                model="unused",
                max_tokens=100,
                reasoning_effort="low",
                response_schema_path=root / "unused_schema.json",
                frozen_ranges=(),
                yosys_exe=None,
                formal_mode=None,
                formal_backend=None,
                seq_depth=None,
                timeout_s=None,
                policy=policy,
                no_orfs=False,
                overwrite=False,
            )

            assert rc == 1
            assert txn["verdict"] == "reject"
            assert events == ["lint"]

            # ----------------------------------------------------------
            # Formal reject: no ORFS.
            # ----------------------------------------------------------

            events.clear()
            lint_should_pass = True
            formal_should_pass = False

            rc, txn = run_single_transaction(
                request_path=request_path,
                golden_manifest_path=manifest_path,
                golden_base_dir=root,
                physical_recipe_path=recipe_path,
                baseline_metrics_path=baseline_path,
                flow_dir=flow_dir,
                ai_runs=ai_runs,
                tag="selftest_formal_reject",
                candidate_path=candidate_input,
                model="unused",
                max_tokens=100,
                reasoning_effort="low",
                response_schema_path=root / "unused_schema.json",
                frozen_ranges=(),
                yosys_exe=None,
                formal_mode=None,
                formal_backend=None,
                seq_depth=None,
                timeout_s=None,
                policy=policy,
                no_orfs=False,
                overwrite=False,
            )

            assert rc == 1
            assert txn["verdict"] == "reject"
            assert events == [
                "lint",
                "sec_bundle",
                "formal",
            ]

            # ----------------------------------------------------------
            # Baseline fingerprint mismatch rejected in preflight.
            # ----------------------------------------------------------

            bad_baseline = dict(baseline_payload)
            bad_baseline["evaluation"] = {
                "stage": "global_route",
                "recipe_fingerprint_sha256": "c" * 64,
            }

            bad_baseline_path = (
                root / "bad_baseline.json"
            )

            write_json(
                bad_baseline_path,
                bad_baseline,
            )

            events.clear()
            lint_should_pass = True
            formal_should_pass = True

            rc, txn = run_single_transaction(
                request_path=request_path,
                golden_manifest_path=manifest_path,
                golden_base_dir=root,
                physical_recipe_path=recipe_path,
                baseline_metrics_path=bad_baseline_path,
                flow_dir=flow_dir,
                ai_runs=ai_runs,
                tag="selftest_bad_baseline",
                candidate_path=candidate_input,
                model="unused",
                max_tokens=100,
                reasoning_effort="low",
                response_schema_path=root / "unused_schema.json",
                frozen_ranges=(),
                yosys_exe=None,
                formal_mode=None,
                formal_backend=None,
                seq_depth=None,
                timeout_s=None,
                policy=policy,
                no_orfs=False,
                overwrite=False,
            )

            assert rc == 2
            assert txn["verdict"] == "error"
            assert events == []

            # Qualification failure must prevent prompt rendering and LLM/EDA calls.
            qualify_dataset = lambda **kwargs: {"qualified": False, "reasons": ["CONSTRAINT_COVERAGE_INCOMPLETE"]}
            events.clear()
            rc, txn = run_single_transaction(
                request_path=request_path,
                golden_manifest_path=manifest_path,
                golden_base_dir=root,
                physical_recipe_path=recipe_path,
                baseline_metrics_path=baseline_path,
                flow_dir=flow_dir,
                ai_runs=ai_runs,
                tag="selftest_bad_qualification",
                candidate_path=None,
                model="unused",
                max_tokens=100,
                reasoning_effort="low",
                response_schema_path=root / "unused_schema.json",
                frozen_ranges=(),
                yosys_exe=None,
                formal_mode=None,
                formal_backend=None,
                seq_depth=None,
                timeout_s=None,
                policy=policy,
                no_orfs=False,
                overwrite=False,
            )

            assert rc == 2
            assert txn["verdict"] == "error"
            assert events == []

            assert "QUALIFICATION_REJECT" in str(txn)

        finally:
            verify_frozen_constraints = originals["verify_frozen_constraints"]
            qualify_dataset = originals["qualify_dataset"]
            DesignManifest = originals[
                "DesignManifest"
            ]
            PhysicalRecipe = originals[
                "PhysicalRecipe"
            ]
            lint_candidate = originals[
                "lint_candidate"
            ]
            build_sec_bundle = originals[
                "build_sec_bundle"
            ]
            run_verification = originals[
                "run_verification"
            ]
            run_candidate_flow = originals[
                "run_candidate_flow"
            ]
            collect_metrics = originals[
                "collect_metrics"
            ]
            save_metrics = originals[
                "save_metrics"
            ]
            evaluate_candidate = originals[
                "evaluate_candidate"
            ]
            write_verify_json = originals[
                "write_verify_json"
            ]

    class _E500(Exception):
        status_code = 503
    class _E400(Exception):
        status_code = 400
    class _Timeout(Exception):
        pass
    assert _is_retryable_provider_error(_E500("server")) is True
    assert _is_retryable_provider_error(_E400("bad request")) is False
    assert _is_retryable_provider_error(_Timeout("request timed out")) is True

    print("PASS: run_optimizer self-test")
    print("PASS: single-shot gate order is lint -> SEC -> formal -> ORFS -> metrics -> evaluator")
    print("PASS: lint rejection stops before formal and ORFS")
    print("PASS: formal rejection stops before ORFS")
    print("PASS: successful candidate reaches PPA evaluator and promotes")
    print("PASS: baseline physical-recipe fingerprint mismatch fails in preflight")
    print("PASS: failed constraint qualification stops before LLM and all candidate stages")
    print("PASS: accepted candidate source bundle is retained; golden RTL is never overwritten")
    print("PASS: promoted candidate is snapshotted into an immutable <tag>_accepted bundle")
    print("PASS: transaction records request hash and a stable verdict block")
    print("PASS: provider retry classification allows timeout/5xx and rejects 4xx")


# =============================================================================
# CLI
# =============================================================================

def parse_frozen_ranges(
    tokens: Sequence[str],
) -> list[Tuple[int, int]]:
    result: list[Tuple[int, int]] = []

    for token in tokens:
        text = str(token).strip()

        if not text:
            continue

        if "-" in text:
            lo_text, hi_text = text.split(
                "-",
                1,
            )
        else:
            lo_text = text
            hi_text = text

        try:
            lo = int(lo_text)
            hi = int(hi_text)
        except ValueError as exc:
            raise OptimizerError(
                f"invalid frozen line range {text!r}"
            ) from exc

        if lo < 1 or hi < lo:
            raise OptimizerError(
                f"invalid frozen line range {text!r}"
            )

        result.append((lo, hi))

    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run one fail-closed nEBULA single-shot RTL optimization transaction."
        )
    )

    parser.add_argument(
        "--request",
        help="Unit-specific optimizer_request/2 JSON.",
    )

    parser.add_argument(
        "--golden-manifest",
        help="Golden DesignManifest JSON.",
    )

    parser.add_argument(
        "--golden-base-dir",
        default=".",
        help="Base directory for golden manifest source paths.",
    )

    parser.add_argument(
        "--physical-recipe",
        help="Frozen physical_recipe/1 JSON.",
    )

    parser.add_argument(
        "--baseline-metrics",
        help="Stage-matched ppa_metrics/2 baseline JSON.",
    )

    parser.add_argument(
        "--candidate",
        default=None,
        help=(
            "Optional existing raw/canonical candidate response. "
            "When supplied, Groq is skipped."
        ),
    )

    parser.add_argument(
        "--model",
        default=os.environ.get(
            "OPTIMIZER_MODEL",
            "openai/gpt-oss-120b",
        ),
        help="Groq model id used when --candidate is omitted.",
    )

    parser.add_argument(
        "--max-tokens",
        type=int,
        default=2048,
    )

    parser.add_argument(
        "--reasoning-effort",
        choices=(
            "low",
            "medium",
            "high",
        ),
        default="medium",
    )

    parser.add_argument(
        "--temperature",
        type=float,
        default=0.0,
        help="Sampling temperature; defaults to 0 for reproducible comparisons.",
    )

    parser.add_argument(
        "--history-file",
        default=None,
        help="Optional append-only optimizer history JSONL override.",
    )

    parser.add_argument(
        "--previous-diagnosis",
        action="append",
        default=[],
        help="Prior diagnosis JSON to preserve in retry context; repeatable.",
    )

    parser.add_argument(
        "--response-schema",
        default=str(
            DEFAULT_RESPONSE_SCHEMA
        ),
    )

    parser.add_argument(
        "--flow-dir",
        default=str(
            DEFAULT_FLOW_DIR
        ),
    )

    parser.add_argument(
        "--ai-runs",
        default=None,
        help=(
            "Artifact directory. Defaults to <flow-dir>/ai_runs."
        ),
    )

    parser.add_argument(
        "--tag",
        default=None,
        help="Candidate/transaction identifier.",
    )

    parser.add_argument(
        "--frozen-lines",
        nargs="*",
        default=[],
        help="Additional hard-frozen target-file line ranges, e.g. 1-8 24-32.",
    )

    parser.add_argument(
        "--yosys",
        default=None,
        help="Optional explicit Yosys executable.",
    )

    parser.add_argument(
        "--formal-mode",
        default=None,
        choices=(
            "combinational_cec",
            "sequential_equivalence",
            "backend_unsupported",
        ),
        help="Optional SEC-mode override.",
    )

    parser.add_argument(
        "--formal-backend",
        default=None,
        help="Optional formal-backend override.",
    )

    parser.add_argument(
        "--seq-depth",
        type=int,
        default=None,
    )

    parser.add_argument(
        "--timeout-s",
        type=float,
        default=None,
    )

    parser.add_argument(
        "--max-wns-regression-ns",
        type=float,
        default=0.0,
    )

    parser.add_argument(
        "--max-tns-regression-ns",
        type=float,
        default=0.0,
    )

    parser.add_argument(
        "--max-area-regression-pct",
        type=float,
        default=0.0,
    )

    parser.add_argument(
        "--max-power-regression-pct",
        type=float,
        default=0.0,
    )

    parser.add_argument(
        "--no-orfs",
        action="store_true",
        help=(
            "Stop after successful formal equivalence; "
            "do not run physical evaluation."
        ),
    )

    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace known artifacts for an explicitly reused --tag.",
    )

    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run orchestration regression tests without Groq/Yosys/ORFS.",
    )

    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.self_test:
        try:
            run_self_test()
        except Exception as exc:
            print(
                f"SELF-TEST FAILED: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return 1

        return 0

    missing = []

    for option, value in (
        ("--request", args.request),
        (
            "--golden-manifest",
            args.golden_manifest,
        ),
        (
            "--physical-recipe",
            args.physical_recipe,
        ),
        (
            "--baseline-metrics",
            args.baseline_metrics,
        ),
    ):
        if not value:
            missing.append(option)

    if missing:
        print(
            "ERROR: missing required argument(s): "
            + ", ".join(missing),
            file=sys.stderr,
        )
        return 2

    try:
        frozen_ranges = parse_frozen_ranges(
            args.frozen_lines
        )

        policy = PromotionPolicy(
            max_wns_regression_ns=(
                args.max_wns_regression_ns
            ),
            max_tns_regression_ns=(
                args.max_tns_regression_ns
            ),
            max_area_regression_pct=(
                args.max_area_regression_pct
            ),
            max_power_regression_pct=(
                args.max_power_regression_pct
            ),
        )
        policy.validate()

        flow_dir = Path(
            args.flow_dir
        ).expanduser().resolve()

        ai_runs = (
            Path(args.ai_runs)
            .expanduser()
            .resolve()
            if args.ai_runs
            else flow_dir / "ai_runs"
        )

        tag = args.tag or default_tag()

        exit_code, _transaction = (
            run_single_transaction(
                request_path=Path(
                    args.request
                ),
                golden_manifest_path=Path(
                    args.golden_manifest
                ),
                golden_base_dir=Path(
                    args.golden_base_dir
                ),
                physical_recipe_path=Path(
                    args.physical_recipe
                ),
                baseline_metrics_path=Path(
                    args.baseline_metrics
                ),
                flow_dir=flow_dir,
                ai_runs=ai_runs,
                tag=tag,
                candidate_path=(
                    Path(args.candidate)
                    if args.candidate
                    else None
                ),
                model=args.model,
                max_tokens=args.max_tokens,
                reasoning_effort=(
                    args.reasoning_effort
                ),
                response_schema_path=Path(
                    args.response_schema
                ),
                frozen_ranges=(
                    frozen_ranges
                ),
                yosys_exe=args.yosys,
                formal_mode=(
                    args.formal_mode
                ),
                formal_backend=(
                    args.formal_backend
                ),
                seq_depth=args.seq_depth,
                timeout_s=args.timeout_s,
                policy=policy,
                no_orfs=args.no_orfs,
                overwrite=args.overwrite,
                temperature=args.temperature,
                history_file=(Path(args.history_file) if args.history_file else None),
                previous_diagnosis_paths=tuple(Path(x) for x in args.previous_diagnosis),
            )
        )

        return exit_code

    except Exception as exc:
        print(
            f"ERROR: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
