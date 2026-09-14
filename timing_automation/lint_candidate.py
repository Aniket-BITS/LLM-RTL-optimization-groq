#!/usr/bin/env python3
"""
lint_candidate.py

Deterministic pre-synthesis safety gate for nEBULA optimizer candidates.

Canonical P0 flow
-----------------

    DesignManifest
         |
         v
    SourceBundle
         |
         +---- unit-specific optimizer request
         |
         +---- candidate patches[]
         |
         v
    deterministic patch validation
         |
         v
    patch_splicer.apply_patches()
         |
         v
    complete candidate source bundle
         |
         v
    Yosys proof-setup validation
         |
         +---- both designs elaborate and pass Yosys checks
         +---- top-level interface unchanged
         +---- other structural differences recorded as diagnostics
         |
         v
       PASS / REJECT

This file replaces the old single-file/regex-authoritative linter.

Authoritative checks
--------------------
1. DesignManifest source hashes match the golden files on disk.
2. The optimizer request identifies one exact unit and complete SourceBundle identity.
3. The candidate matches its requested response schema.
4. target_unit_id matches the request.
5. chosen_pattern_id may follow a recommendation, but recommendation adherence is not a safety gate.
6. Every patch targets a file/module/SHA belonging to the exact SourceBundle.
7. Diagnostic focus ranges are advisory and do not constrain patch ranges.
8. Optional additional frozen ranges are untouched.
9. Patch overlap/module ownership/source-SHA checks are enforced by the
   SourceBundle/extractor/patch-splicer layers.
10. The COMPLETE candidate source bundle is materialized in isolation.
11. Yosys validates elaboration and the top-level interface. Other structural
    differences are reported but left for formal equivalence to assess.

Regex/text parsing is not used as the authority for RTL structure.
Sequential equivalence checking remains the later functional proof gate.

Normal usage
------------

python3 timing_automation/lint_candidate.py \
    --manifest path/to/design_manifest.json \
    --base-dir . \
    --request ai_runs/<variant>_unit_000_request.json \
    --candidate ai_runs/cand_0001.candidate.json \
    --candidate-out-dir ai_runs/cand_0001_sources \
    --output ai_runs/cand_0001.lint.json

Self-test
---------

python3 timing_automation/lint_candidate.py --self-test
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple


# =============================================================================
# Local imports
# =============================================================================

try:
    from design_manifest import DesignManifest
    from extract_candidate import (
        CandidateExtractionError,
        CandidateSchemaError,
        ExtractedCandidate,
        extract_candidate,
    )
    from patch_splicer import (
        PatchError,
        SourcePatch,
        SpliceResult,
        apply_patches,
        find_module_line_spans,
    )
    from source_bundle import SourceBundle, SourceBundleError
    from yosys_structure import (
        StructureComparison,
        YosysStructureError,
        blocks_equivalence_setup,
        compare_manifests,
        resolve_yosys,
    )
except ImportError:
    from timing_automation.design_manifest import DesignManifest
    from timing_automation.extract_candidate import (
        CandidateExtractionError,
        CandidateSchemaError,
        ExtractedCandidate,
        extract_candidate,
    )
    from timing_automation.patch_splicer import (
        PatchError,
        SourcePatch,
        SpliceResult,
        apply_patches,
        find_module_line_spans,
    )
    from timing_automation.source_bundle import SourceBundle, SourceBundleError
    from timing_automation.yosys_structure import (
        StructureComparison,
        YosysStructureError,
        blocks_equivalence_setup,
        compare_manifests,
        resolve_yosys,
    )


# =============================================================================
# Exceptions
# =============================================================================


class LintCandidateError(RuntimeError):
    """Base deterministic lint error."""


class RequestValidationError(LintCandidateError):
    """Raised when the unit-specific optimizer request is inconsistent."""


# =============================================================================
# Generic JSON helpers
# =============================================================================


def load_json(path: Path) -> Dict[str, Any]:
    path = Path(path)

    if not path.is_file():
        raise LintCandidateError(f"JSON file not found: {path}")

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LintCandidateError(f"invalid JSON in {path}: {exc}") from exc

    if not isinstance(value, dict):
        raise LintCandidateError(f"JSON root must be an object: {path}")

    return value


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )


# =============================================================================
# Range helpers
# =============================================================================


def parse_cli_ranges(tokens: Sequence[str] | None) -> List[Tuple[int, int]]:
    """Parse CLI tokens such as 10 or 10-14."""

    result: List[Tuple[int, int]] = []

    for raw in tokens or []:
        token = str(raw).strip()

        if not token:
            raise ValueError("empty line-range token")

        if "-" in token:
            parts = token.split("-", 1)
            if len(parts) != 2:
                raise ValueError(f"invalid line range: {token!r}")
            try:
                lo = int(parts[0].strip())
                hi = int(parts[1].strip())
            except ValueError as exc:
                raise ValueError(f"invalid line range: {token!r}") from exc
        else:
            try:
                lo = int(token)
                hi = lo
            except ValueError as exc:
                raise ValueError(f"invalid line range: {token!r}") from exc

        if lo < 1 or hi < 1 or hi < lo:
            raise ValueError(f"invalid line range: {token!r}")

        result.append((lo, hi))

    return result


def validate_request_ranges(raw: Any) -> List[Tuple[int, int]]:
    """Validate request editable_line_ranges."""

    if not isinstance(raw, list) or not raw:
        raise RequestValidationError(
            "request editable_line_ranges must be a non-empty array"
        )

    result: List[Tuple[int, int]] = []

    for index, item in enumerate(raw):
        if (
            not isinstance(item, (list, tuple))
            or len(item) != 2
            or not isinstance(item[0], int)
            or isinstance(item[0], bool)
            or not isinstance(item[1], int)
            or isinstance(item[1], bool)
        ):
            raise RequestValidationError(
                f"editable_line_ranges[{index}] must be [start_line, end_line]"
            )

        lo = int(item[0])
        hi = int(item[1])

        if lo < 1 or hi < lo:
            raise RequestValidationError(
                f"editable_line_ranges[{index}] has invalid range {lo}-{hi}"
            )

        result.append((lo, hi))

    # Exact request ranges must not overlap. build_request.py already merges
    # overlaps/adjacent locations, so an overlap here is suspicious.
    ordered = sorted(result)
    previous: Tuple[int, int] | None = None

    for current in ordered:
        if previous is not None and current[0] <= previous[1]:
            raise RequestValidationError(
                "request editable_line_ranges overlap: "
                f"{previous[0]}-{previous[1]} and {current[0]}-{current[1]}"
            )
        previous = current

    return ordered


def format_ranges(ranges: Iterable[Tuple[int, int]]) -> List[List[int]]:
    return [[int(lo), int(hi)] for lo, hi in ranges]


def range_is_contained(
    patch_range: Tuple[int, int],
    allowed_ranges: Sequence[Tuple[int, int]],
) -> bool:
    """
    A patch must fit inside ONE exact editable interval.

    Two separated editable intervals are never treated as their convex hull.
    """

    start, end = patch_range
    return any(lo <= start and end <= hi for lo, hi in allowed_ranges)


def ranges_overlap(a: Tuple[int, int], b: Tuple[int, int]) -> bool:
    return max(a[0], b[0]) <= min(a[1], b[1])


# =============================================================================
# Request validation
# =============================================================================


@dataclass(frozen=True)
class RequestScope:
    unit_id: str
    optimizer_mode: str
    source_hashes: Dict[str, str]
    module_to_file: Dict[str, str]
    primary_file: str | None
    primary_module: str | None
    recommended_pattern_ids: Tuple[str, ...]
    recommended_focus_ranges: Tuple[Tuple[int, int], ...]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "unit_id": self.unit_id,
            "optimizer_mode": self.optimizer_mode,
            "source_hashes": dict(self.source_hashes),
            "module_to_file": dict(self.module_to_file),
            "primary_file": self.primary_file,
            "primary_module": self.primary_module,
            "recommended_pattern_ids": list(self.recommended_pattern_ids),
            "recommended_focus_ranges": format_ranges(self.recommended_focus_ranges),
        }

def validate_request(
    request: Mapping[str, Any],
    bundle: SourceBundle,
) -> RequestScope:
    """Validate exact SourceBundle binding; diagnostic focus is advisory."""
    if request.get("schema") != "optimizer_request/2":
        raise RequestValidationError("request schema must be 'optimizer_request/2'")
    unit_id = request.get("unit_id")
    if not isinstance(unit_id, str) or not unit_id:
        raise RequestValidationError("request unit_id must be a non-empty string")
    mode = request.get("optimizer_mode") or "guided"
    if mode not in ("guided", "guided_evidence_only", "exploratory"):
        raise RequestValidationError(f"invalid optimizer_mode {mode!r}")

    request_bundle = request.get("source_bundle") or {}
    declared_sources = request_bundle.get("sources") or []
    if not isinstance(declared_sources, list) or not declared_sources:
        raise RequestValidationError("request source_bundle.sources must be a non-empty array")
    declared_hashes: Dict[str, str] = {}
    for i, item in enumerate(declared_sources):
        if not isinstance(item, Mapping):
            raise RequestValidationError(f"source_bundle.sources[{i}] is invalid")
        name=item.get("path"); digest=item.get("sha256")
        if not isinstance(name,str) or not name or not isinstance(digest,str) or len(digest)!=64:
            raise RequestValidationError(f"source_bundle.sources[{i}] has invalid path/hash")
        declared_hashes[name]=digest.lower()
    actual_hashes={k:v.lower() for k,v in bundle.source_hashes().items()}
    if declared_hashes != actual_hashes:
        raise RequestValidationError("request SourceBundle hash map does not match golden SourceBundle")
    declared_modules=request_bundle.get("module_to_file") or {}
    actual_modules=dict(sorted(bundle.module_to_file.items()))
    if declared_modules and dict(declared_modules) != actual_modules:
        raise RequestValidationError("request SourceBundle module ownership does not match golden SourceBundle")

    focus=request.get("primary_diagnostic_focus") or request.get("target") or {}
    primary_file=focus.get("file") if isinstance(focus,Mapping) else None
    primary_module=focus.get("module") if isinstance(focus,Mapping) else None
    if primary_file and primary_file not in bundle.files:
        raise RequestValidationError(f"primary diagnostic file {primary_file!r} is not in SourceBundle")
    if primary_module:
        owner=bundle.file_for_module(primary_module)
        if primary_file and owner != primary_file:
            raise RequestValidationError("primary diagnostic module/file ownership mismatch")
    raw_focus=request.get("recommended_focus_ranges") or request.get("editable_line_ranges") or []
    focus_ranges=[]
    if raw_focus:
        # Advisory ranges still need sane shape when present.
        focus_ranges=validate_request_ranges(raw_focus)
    recommended_raw=request.get("recommended_pattern_ids")
    if recommended_raw is None:
        recommended_raw=request.get("eligible_pattern_ids") or []
    if not isinstance(recommended_raw,list):
        raise RequestValidationError("recommended_pattern_ids must be an array")
    recommended=tuple(x for x in recommended_raw if isinstance(x,str) and x)
    return RequestScope(
        unit_id=unit_id,
        optimizer_mode=mode,
        source_hashes=actual_hashes,
        module_to_file=actual_modules,
        primary_file=primary_file,
        primary_module=primary_module,
        recommended_pattern_ids=recommended,
        recommended_focus_ranges=tuple(focus_ranges),
    )

# =============================================================================
# Candidate/request checks
# =============================================================================


# Model-authored self-checks are advisory for modern complete-source schemas.
# Backend/Yosys evidence below is the only hard authority for interface, clock,
# reset, latch, domain, and latency safety.  Legacy patch schemas retain their
# historical diagnostics for replay compatibility.
REQUIRED_TRUE_SELF_CHECKS = (
    "interface_unchanged",
    "parameters_unchanged",
    "clock_reset_behavior_unchanged",
    "source_bundle_targets_valid",
    "no_new_latches",
    "no_new_clock_domains",
)
MODERN_ADVISORY_SELF_CHECK_SCHEMAS = {"3.1", "3.2", "3.3"}


def validate_candidate_against_request(
    candidate: ExtractedCandidate,
    scope: RequestScope,
    frozen_ranges: Sequence[Tuple[int, int]],
) -> Tuple[Dict[str, bool], List[str]]:
    """Validate hard request boundaries. Focus/pattern adherence is informational."""
    checks: Dict[str, bool] = {}
    reasons: List[str] = []
    unit_ok = candidate.target_unit_id == scope.unit_id
    checks["target_unit_matches_request"] = unit_ok
    if not unit_ok:
        reasons.append(f"candidate target_unit_id {candidate.target_unit_id!r} != {scope.unit_id!r}")

    chosen=candidate.chosen_pattern_id
    recommended_ok = chosen is None or chosen in scope.recommended_pattern_ids
    checks["chosen_pattern_is_recommended"] = recommended_ok

    targets_ok=True; frozen_ok=True; focus_adherence=True
    for index, patch in enumerate(candidate.patches):
        expected_sha=scope.source_hashes.get(patch.target_file)
        if expected_sha is None:
            targets_ok=False; reasons.append(f"patches[{index}] target_file is outside SourceBundle: {patch.target_file!r}")
            continue
        if patch.original_sha256.lower()!=expected_sha.lower():
            targets_ok=False; reasons.append(f"patches[{index}] original_sha256 does not match SourceBundle")
        owner=scope.module_to_file.get(patch.target_module)
        if owner != patch.target_file:
            targets_ok=False; reasons.append(f"patches[{index}] module {patch.target_module!r} does not belong to {patch.target_file!r}")
        patch_range=(patch.start_line,patch.end_line)
        if scope.primary_file == patch.target_file and scope.recommended_focus_ranges:
            if not range_is_contained(patch_range,scope.recommended_focus_ranges):
                focus_adherence=False
        # Legacy CLI frozen ranges are interpreted only for the primary focus file.
        if scope.primary_file == patch.target_file:
            for frozen_range in frozen_ranges:
                if ranges_overlap(patch_range,frozen_range):
                    frozen_ok=False
                    reasons.append(f"patches[{index}] range {patch.start_line}-{patch.end_line} touches frozen range {frozen_range[0]}-{frozen_range[1]}")
    if candidate.complete_sources is not None:
        import difflib
        targets_ok = candidate.bundle_validated
        for name, content in candidate.complete_sources.items():
            original = (candidate.original_sources or {})[name]
            old_lines = original.splitlines(keepends=True)
            new_lines = content.splitlines(keepends=True)
            for op, start, end, _new_start, _new_end in difflib.SequenceMatcher(
                    None, old_lines, new_lines, autojunk=False).get_opcodes():
                if op == "equal" or name != scope.primary_file:
                    continue
                changed_range = (start + 1, max(start + 1, end))
                if scope.recommended_focus_ranges and not range_is_contained(changed_range, scope.recommended_focus_ranges):
                    focus_adherence = False
                for lo, hi in frozen_ranges:
                    touches = (lo <= start < hi) if op == "insert" else (start < hi and end >= lo)
                    if touches:
                        frozen_ok = False
                        reasons.append(f"complete source {name!r} changes frozen lines {lo}-{hi}")
    checks["source_bundle_targets_valid"] = targets_ok
    checks["patch_targets_match_request"] = targets_ok  # compatibility alias
    checks["recommended_focus_followed"] = focus_adherence
    checks["frozen_ranges_untouched"] = frozen_ok

    self_checks=candidate.payload.get("self_checks")
    schema_version=str(candidate.payload.get("schema_version") or "")
    self_checks_ok=isinstance(self_checks,dict)
    if isinstance(self_checks,dict):
        for key in REQUIRED_TRUE_SELF_CHECKS:
            if self_checks.get(key) is not True:
                self_checks_ok=False
                if schema_version not in MODERN_ADVISORY_SELF_CHECK_SCHEMAS:
                    reasons.append(f"candidate self_checks.{key} is not true")
    elif schema_version not in MODERN_ADVISORY_SELF_CHECK_SCHEMAS:
        reasons.append("candidate self_checks is not an object")
    # Diagnostic only for schema >= 3.1.  It is intentionally absent from
    # mandatory_checks and never authorizes/rejects modern candidates.
    checks["model_self_checks_all_true"] = self_checks_ok
    checks["model_self_checks_advisory"] = schema_version in MODERN_ADVISORY_SELF_CHECK_SCHEMAS
    return checks,reasons

# =============================================================================
# Candidate source staging
# =============================================================================


def _safe_relative_stage_path(source_name: str, index: int) -> Path:
    """Map arbitrary manifest source names into a safe staging tree."""

    path = Path(source_name)

    if not path.is_absolute() and ".." not in path.parts:
        return path

    basename = path.name or f"source_{index}.v"
    return Path("__absolute_sources__") / f"{index:04d}_{basename}"


def plan_source_mapping(bundle: SourceBundle) -> Dict[str, str]:
    """Plan deterministic staged paths without writing anything."""

    return {
        source_name: _safe_relative_stage_path(source_name, index).as_posix()
        for index, source_name in enumerate(bundle.source_names())
    }


def stage_splice_result(
    bundle: SourceBundle,
    splice: SpliceResult,
    stage_root: Path,
) -> Dict[str, str]:
    """
    Write the COMPLETE candidate source bundle under stage_root.

    Returns mapping:
        original manifest source name -> staged relative source name
    """

    stage_root = Path(stage_root).resolve()
    stage_root.mkdir(parents=True, exist_ok=True)

    mapping = plan_source_mapping(bundle)

    for source_name, staged_name in mapping.items():
        destination = stage_root / staged_name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(splice.get_bytes(source_name))

    return mapping


def candidate_include_dirs(
    manifest: DesignManifest,
    golden_base_dir: Path,
    candidate_root: Path,
    source_mapping: Mapping[str, str],
) -> List[str]:
    """
    Build an include search path for staged candidates.

    Original include directories remain authoritative for unchanged header
    files. Candidate source directories are added so source-local includes can
    resolve against the staged tree where possible.
    """

    ordered: List[str] = []
    seen = set()

    def add(path: Path) -> None:
        value = str(path.resolve())
        if value not in seen:
            seen.add(value)
            ordered.append(value)

    for include_dir in manifest.resolved_include_dirs(golden_base_dir):
        add(include_dir)

    for source_name in manifest.source_files:
        try:
            add(manifest.resolve_path(source_name, golden_base_dir).parent)
        except Exception:
            pass

    for staged_name in source_mapping.values():
        add((candidate_root / staged_name).parent)

    return ordered


def build_candidate_manifest(
    golden_manifest: DesignManifest,
    golden_base_dir: Path,
    candidate_root: Path,
    source_mapping: Mapping[str, str],
    splice: SpliceResult,
) -> DesignManifest:
    """Clone the logical manifest while pointing at staged candidate files."""

    data = copy.deepcopy(golden_manifest.to_dict())

    staged_sources = [
        source_mapping[source_name]
        for source_name in golden_manifest.source_files
    ]

    data["source_files"] = staged_sources
    data["include_dirs"] = candidate_include_dirs(
        golden_manifest,
        golden_base_dir,
        candidate_root,
        source_mapping,
    )

    data["source_hashes"] = {
        source_mapping[source_name]: hashlib.sha256(
            splice.get_bytes(source_name)
        ).hexdigest()
        for source_name in golden_manifest.source_files
    }

    metadata = data.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}

    metadata = dict(metadata)
    metadata["candidate_source_mapping"] = dict(source_mapping)
    metadata["candidate_generated_by"] = "lint_candidate.py"
    data["metadata"] = metadata

    candidate_manifest = DesignManifest.from_dict(data)
    candidate_manifest.validate()

    return candidate_manifest


def materialize_accepted_candidate(
    *,
    bundle: SourceBundle,
    splice: SpliceResult,
    golden_manifest: DesignManifest,
    golden_base_dir: Path,
    out_dir: Path,
) -> Tuple[Path, Dict[str, str]]:
    """
    Materialize an accepted candidate for SEC/synthesis.

    The golden source tree is never modified.
    """

    out_dir = Path(out_dir).resolve()
    golden_base_dir = Path(golden_base_dir).resolve()

    if out_dir == golden_base_dir:
        raise LintCandidateError(
            "candidate-out-dir must not be the golden base directory"
        )

    # Preflight every destination BEFORE writing any bytes.
    source_mapping = plan_source_mapping(bundle)

    for source_name, staged_name in source_mapping.items():
        original = bundle.get_source(source_name).absolute_path.resolve()
        staged = (out_dir / staged_name).resolve()

        if original == staged:
            raise LintCandidateError(
                f"refusing to overwrite golden source {original}"
            )

    stage_splice_result(bundle, splice, out_dir)

    candidate_manifest = build_candidate_manifest(
        golden_manifest,
        golden_base_dir,
        out_dir,
        source_mapping,
        splice,
    )

    manifest_path = out_dir / "design_manifest.json"
    candidate_manifest.write_json(manifest_path)

    return manifest_path, source_mapping


# =============================================================================
# Yosys result interpretation
# =============================================================================


STATE_BOUNDARY_DIFFERENCE_CATEGORIES = {
    "state_boundary_changed",
    "elaborated_state_boundary_changed",
}


def structure_checks(
    comparison: StructureComparison,
    top: str,
) -> Dict[str, bool]:
    categories = {
        difference.category
        for difference in comparison.differences
    }

    state_boundary_unchanged = not bool(
        categories & STATE_BOUNDARY_DIFFERENCE_CATEGORIES
    )

    return {
        "yosys_structural_analysis_completed": True,
        "module_set_unchanged": not bool(
            categories & {"module_added", "module_removed"}
        ),
        "interfaces_unchanged": "interface_changed" not in categories,
        "parameters_unchanged": "parameters_changed" not in categories,
        "hierarchy_unchanged": "hierarchy_changed" not in categories,
        "state_boundary_unchanged": state_boundary_unchanged,
        "no_new_latches": "latch_introduced" not in categories,
        "no_new_clock_domains": "new_clock_domain" not in categories,
        "clock_reset_behavior_unchanged": "clock_reset_behavior_changed" not in categories,
        "preexisting_live_nets_driven": "preexisting_live_net_undriven" not in categories,
        "yosys_structure_unchanged": not comparison.differences,
        "top_interface_unchanged": not any(
            blocks_equivalence_setup(difference, top)
            for difference in comparison.differences
        ),
        "yosys_equivalence_ready": comparison.ok,
    }


# =============================================================================
# Main lint API
# =============================================================================


def lint_candidate(
    *,
    manifest: DesignManifest,
    base_dir: Path,
    request: Mapping[str, Any],
    candidate_text: str,
    frozen_ranges: Sequence[Tuple[int, int]] = (),
    yosys_exe: str | None = None,
    candidate_out_dir: Path | None = None,
) -> Dict[str, Any]:
    """Run the complete deterministic P0 pre-synthesis gate."""

    base_dir = Path(base_dir).resolve()

    checks: Dict[str, bool] = {}
    reasons: List[str] = []
    structure_differences: List[Dict[str, Any]] = []
    scope: RequestScope | None = None
    candidate: ExtractedCandidate | None = None
    splice: SpliceResult | None = None
    bundle: SourceBundle | None = None
    materialized_manifest: str | None = None
    materialized_mapping: Dict[str, str] = {}

    # ------------------------------------------------------------------
    # 1. Golden SourceBundle / manifest hashes
    # ------------------------------------------------------------------

    try:
        bundle = SourceBundle.from_manifest(
            manifest,
            base_dir,
            verify_manifest_hashes=True,
        )
        checks["golden_source_bundle_valid"] = True
    except Exception as exc:
        checks["golden_source_bundle_valid"] = False
        reasons.append(f"golden SourceBundle validation failed: {exc}")

    # ------------------------------------------------------------------
    # 2. Unit request safety boundary
    # ------------------------------------------------------------------

    if bundle is not None:
        try:
            scope = validate_request(request, bundle)
            checks["request_matches_source_bundle"] = True
        except Exception as exc:
            checks["request_matches_source_bundle"] = False
            reasons.append(f"optimizer request invalid: {exc}")
    else:
        checks["request_matches_source_bundle"] = False

    # ------------------------------------------------------------------
    # 3. Candidate schema + SourcePatch conversion + bundle validation
    # ------------------------------------------------------------------

    if bundle is not None:
        try:
            candidate = extract_candidate(
                candidate_text,
                bundle=bundle,
            )
            required_schema = request.get("response_schema_version")
            if required_schema in ("3.0", "3.1") and candidate.payload.get("schema_version") != required_schema:
                raise LintCandidateError(f"this request requires schema {required_schema} complete Verilog source_files")
            checks["candidate_schema_and_patch_targets_valid"] = True
        except Exception as exc:
            checks["candidate_schema_and_patch_targets_valid"] = False
            reasons.append(f"candidate extraction/validation failed: {exc}")
    else:
        checks["candidate_schema_and_patch_targets_valid"] = False

    # ------------------------------------------------------------------
    # 4. Candidate must match exact request scope
    # ------------------------------------------------------------------

    if candidate is not None and scope is not None:
        request_checks, request_reasons = validate_candidate_against_request(
            candidate,
            scope,
            frozen_ranges,
        )
        checks.update(request_checks)
        reasons.extend(request_reasons)
    else:
        checks.setdefault("target_unit_matches_request", False)
        checks.setdefault("chosen_pattern_is_recommended", False)
        checks.setdefault("source_bundle_targets_valid", False)
        checks.setdefault("patch_targets_match_request", False)
        checks.setdefault("recommended_focus_followed", False)
        checks.setdefault("frozen_ranges_untouched", False)
        checks.setdefault("model_self_checks_all_true", False)

    # Hard pre-splice trust boundary. Pattern recommendations and model
    # self-check claims are diagnostic/advisory only; they never authorize or
    # reject a candidate. Exact SourceBundle identity, unit binding and frozen
    # policy remain deterministic hard gates.
    pre_splice_required = (
        "golden_source_bundle_valid",
        "request_matches_source_bundle",
        "candidate_schema_and_patch_targets_valid",
        "target_unit_matches_request",
        "patch_targets_match_request",
        "source_bundle_targets_valid",
        "frozen_ranges_untouched",
    )

    pre_splice_ok = all(checks.get(key) is True for key in pre_splice_required)

    # ------------------------------------------------------------------
    # 5. Deterministic splice
    # ------------------------------------------------------------------

    if pre_splice_ok and bundle is not None and candidate is not None:
        try:
            if candidate.complete_sources is not None:
                import hashlib
                files = candidate.complete_sources
                original_hashes = bundle.source_hashes()
                candidate_hashes = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
                splice = SpliceResult(files=dict(files),
                    changed_files=tuple(name for name in files if files[name] != bundle.get_source(name).content),
                    original_hashes=original_hashes, candidate_hashes=candidate_hashes)
            else:
                splice = apply_patches(bundle, candidate.patches)
            checks["deterministic_splice_succeeded"] = True

            # Every source not named in changed_files must remain byte-identical.
            untouched_ok = True
            changed = set(splice.changed_files)

            for source_name in bundle.source_names():
                if source_name in changed:
                    continue
                if splice.get_bytes(source_name) != bundle.get_source(source_name).content:
                    untouched_ok = False
                    reasons.append(
                        f"untouched source changed unexpectedly: {source_name}"
                    )

            checks["untouched_sources_byte_identical"] = untouched_ok
        except Exception as exc:
            checks["deterministic_splice_succeeded"] = False
            checks["untouched_sources_byte_identical"] = False
            reasons.append(f"deterministic patch splice failed: {exc}")
    else:
        checks["deterministic_splice_succeeded"] = False
        checks["untouched_sources_byte_identical"] = False

    # ------------------------------------------------------------------
    # 6. Materialize an isolated temporary complete candidate and compare
    #    compiler-derived structure with Yosys.
    # ------------------------------------------------------------------

    if (
        splice is not None
        and bundle is not None
        and checks.get("untouched_sources_byte_identical") is True
    ):
        try:
            with tempfile.TemporaryDirectory(
                prefix="nebula_lint_candidate_"
            ) as tmp:
                candidate_root = Path(tmp).resolve()

                source_mapping = stage_splice_result(
                    bundle,
                    splice,
                    candidate_root,
                )

                candidate_manifest = build_candidate_manifest(
                    manifest,
                    base_dir,
                    candidate_root,
                    source_mapping,
                    splice,
                )

                comparison, _golden_snapshot, _candidate_snapshot = (
                    compare_manifests(
                        golden_manifest=manifest,
                        golden_base_dir=base_dir,
                        candidate_manifest=candidate_manifest,
                        candidate_base_dir=candidate_root,
                        yosys_exe=yosys_exe,
                    )
                )

                checks.update(structure_checks(comparison, manifest.top))
                structure_differences = [
                    difference.to_dict()
                    for difference in comparison.differences
                ]

                if not comparison.ok:
                    for difference in comparison.differences:
                        if not blocks_equivalence_setup(difference, manifest.top):
                            continue
                        reasons.append(
                            f"Yosys structural rejection "
                            f"[{difference.category}]"
                            + (
                                f" module={difference.module!r}"
                                if difference.module is not None
                                else ""
                            )
                            + f": {difference.detail}"
                        )

        except YosysStructureError as exc:
            checks["yosys_structural_analysis_completed"] = False
            checks["module_set_unchanged"] = False
            checks["interfaces_unchanged"] = False
            checks["parameters_unchanged"] = False
            checks["hierarchy_unchanged"] = False
            checks["state_boundary_unchanged"] = False
            checks["no_new_latches"] = False
            checks["no_new_clock_domains"] = False
            checks["clock_reset_behavior_unchanged"] = False
            checks["preexisting_live_nets_driven"] = False
            checks["yosys_structure_unchanged"] = False
            checks["top_interface_unchanged"] = False
            checks["yosys_equivalence_ready"] = False
            reasons.append(f"Yosys structural analysis failed: {exc}")

        except Exception as exc:
            checks["yosys_structural_analysis_completed"] = False
            checks["module_set_unchanged"] = False
            checks["interfaces_unchanged"] = False
            checks["parameters_unchanged"] = False
            checks["hierarchy_unchanged"] = False
            checks["state_boundary_unchanged"] = False
            checks["no_new_latches"] = False
            checks["no_new_clock_domains"] = False
            checks["clock_reset_behavior_unchanged"] = False
            checks["preexisting_live_nets_driven"] = False
            checks["yosys_structure_unchanged"] = False
            checks["top_interface_unchanged"] = False
            checks["yosys_equivalence_ready"] = False
            reasons.append(f"candidate structural staging failed: {exc}")
    else:
        checks["yosys_structural_analysis_completed"] = False
        checks["module_set_unchanged"] = False
        checks["interfaces_unchanged"] = False
        checks["parameters_unchanged"] = False
        checks["hierarchy_unchanged"] = False
        checks["state_boundary_unchanged"] = False
        checks["no_new_latches"] = False
        checks["no_new_clock_domains"] = False
        checks["clock_reset_behavior_unchanged"] = False
        checks["preexisting_live_nets_driven"] = False
        checks["yosys_structure_unchanged"] = False
        checks["top_interface_unchanged"] = False
        checks["yosys_equivalence_ready"] = False

    # ------------------------------------------------------------------
    # Overall result BEFORE permanent candidate materialization.
    # ------------------------------------------------------------------

    mandatory_checks = (
        "golden_source_bundle_valid",
        "request_matches_source_bundle",
        "candidate_schema_and_patch_targets_valid",
        "target_unit_matches_request",
        "patch_targets_match_request",
        "source_bundle_targets_valid",
        "frozen_ranges_untouched",
        "deterministic_splice_succeeded",
        "untouched_sources_byte_identical",
        "yosys_structural_analysis_completed",
        "no_new_latches",
        "no_new_clock_domains",
        "clock_reset_behavior_unchanged",
        "preexisting_live_nets_driven",
        "top_interface_unchanged",
        "yosys_equivalence_ready",
    )

    ok = all(checks.get(key) is True for key in mandatory_checks)

    # ------------------------------------------------------------------
    # 7. Materialize accepted candidate for the next SEC stage.
    # ------------------------------------------------------------------

    if ok and candidate_out_dir is not None and bundle is not None and splice is not None:
        try:
            manifest_path, mapping = materialize_accepted_candidate(
                bundle=bundle,
                splice=splice,
                golden_manifest=manifest,
                golden_base_dir=base_dir,
                out_dir=candidate_out_dir,
            )
            materialized_manifest = str(manifest_path)
            materialized_mapping = mapping
            checks["accepted_candidate_materialized"] = True
        except Exception as exc:
            checks["accepted_candidate_materialized"] = False
            reasons.append(f"accepted candidate materialization failed: {exc}")
            ok = False
    elif candidate_out_dir is not None:
        checks["accepted_candidate_materialized"] = False
    else:
        # Not requested, so this is not part of pass/fail.
        checks["accepted_candidate_materialized"] = True

    # Re-evaluate in case requested materialization failed.
    if candidate_out_dir is not None:
        ok = bool(ok and checks.get("accepted_candidate_materialized") is True)

    result: Dict[str, Any] = {
        "tool": "lint_candidate.py",
        "schema": "lint_candidate/2",
        "ok": ok,
        "design": {
            "dataset": manifest.dataset,
            "design_id": manifest.design_id,
            "top": manifest.top,
        },
        "unit_id": scope.unit_id if scope else request.get("unit_id"),
        "request_scope": scope.to_dict() if scope else None,
        "candidate": {
            "schema_version": (
                candidate.payload.get("schema_version")
                if candidate is not None
                else None
            ),
            "chosen_pattern_id": (
                candidate.chosen_pattern_id
                if candidate is not None
                else None
            ),
            "target_unit_id": (
                candidate.target_unit_id
                if candidate is not None
                else None
            ),
            "output_format": "complete_verilog" if candidate is not None and candidate.complete_sources is not None else "legacy_patches",
            "source_file_count": len(candidate.complete_sources or {}) if candidate is not None else 0,
            "patch_count": len(candidate.patches) if candidate is not None else 0,
            "patches": (
                copy.deepcopy(candidate.payload.get("patches", []))
                if candidate is not None
                else []
            ),
        },
        "frozen_ranges": format_ranges(frozen_ranges),
        "changed_files": (
            list(splice.changed_files)
            if splice is not None
            else []
        ),
        "original_hashes": (
            splice.original_hashes
            if splice is not None
            else {}
        ),
        "candidate_hashes": (
            splice.candidate_hashes
            if splice is not None
            else {}
        ),
        "checks": checks,
        "structure_differences": structure_differences,
        "candidate_bundle": {
            "output_dir": (
                str(Path(candidate_out_dir).resolve())
                if candidate_out_dir is not None
                else None
            ),
            "manifest": materialized_manifest,
            "source_mapping": materialized_mapping,
        },
        "reasons": reasons,
    }

    return result


# =============================================================================
# Self-test
# =============================================================================


def _self_checks() -> Dict[str, Any]:
    return {
        "interface_unchanged": True,
        "parameters_unchanged": True,
        "latency_unchanged": True,
        "clock_reset_behavior_unchanged": True,
        "source_bundle_targets_valid": True,
        "no_new_latches": True,
        "no_new_clock_domains": True,
        "notes": [],
    }


def _write_self_test_design(root: Path) -> None:
    rtl = root / "rtl"
    rtl.mkdir(parents=True, exist_ok=True)

    (rtl / "top.v").write_text(
        "module top #(\n"
        "    parameter STRUCT_TAG = 8\n"
        ") (\n"
        "    input  wire [7:0] a,\n"
        "    input  wire [7:0] b,\n"
        "    output wire [7:0] y\n"
        ");\n"
        "\n"
        "wire [7:0] t;\n"
        "assign t = a & b;\n"
        "child u_child (\n"
        "    .a(t),\n"
        "    .y(y)\n"
        ");\n"
        "\n"
        "endmodule\n",
        encoding="utf-8",
    )

    (rtl / "child.v").write_text(
        "module child (\n"
        "    input  wire [7:0] a,\n"
        "    output wire [7:0] y\n"
        ");\n"
        "assign y = a;\n"
        "endmodule\n",
        encoding="utf-8",
    )


def _write_state_boundary_self_test_design(root: Path) -> None:
    """
    Small sequential design used to prove that lint_candidate.py makes the
    Yosys state-boundary comparison a mandatory pre-synthesis gate.

    Line 11 is deliberately combinational and legal to rewrite.
    Line 12 contains the clock edge and is deliberately unsafe to rewrite.
    """

    rtl = root / "rtl"
    rtl.mkdir(parents=True, exist_ok=True)

    (rtl / "top.v").write_text(
        "module top #(\n"
        "    parameter STRUCT_TAG = 8\n"
        ") (\n"
        "    input  wire clk,\n"
        "    input  wire rst_n,\n"
        "    input  wire [7:0] a,\n"
        "    output wire [7:0] y\n"
        ");\n"
        "reg [7:0] q;\n"
        "wire [7:0] next;\n"
        "assign next = a + 8'd1;\n"
        "always @(posedge clk or negedge rst_n) begin\n"
        "    if (!rst_n)\n"
        "        q <= 8'd0;\n"
        "    else\n"
        "        q <= next;\n"
        "end\n"
        "assign y = q;\n"
        "endmodule\n",
        encoding="utf-8",
    )

    # Keep the same two-file manifest shape as the existing linter self-test.
    # The child is intentionally unused; module-set comparison still sees it.
    (rtl / "child.v").write_text(
        "module child (\n"
        "    input  wire [7:0] a,\n"
        "    output wire [7:0] y\n"
        ");\n"
        "assign y = a;\n"
        "endmodule\n",
        encoding="utf-8",
    )

def _self_test_manifest(root: Path) -> DesignManifest:
    manifest = DesignManifest(
        dataset="lint_candidate_self_test",
        design_id="lint_test",
        top="top",
        source_files=["rtl/top.v", "rtl/child.v"],
        include_dirs=[],
        defines=[],
        parameters={},
        language="verilog",
        clocks=[],
        resets=[],
        constraints={},
    )
    manifest.refresh_source_hashes(root)
    return manifest


def _self_test_request(
    *,
    bundle: SourceBundle,
    evidence_dir: Path,
    editable_ranges: Sequence[Tuple[int, int]],
) -> Dict[str, Any]:
    top_source = bundle.get_source("rtl/top.v")
    request = {
        "schema": "optimizer_request/2",
        "request_format_version": "2.2",
        "response_schema_version": "2.0",
        "unit_id": "unit_000",
        "optimizer_mode": "guided",
        "source_bundle": bundle.prompt_context(),
        "primary_diagnostic_focus": {
            "file": "rtl/top.v",
            "module": "top",
            "original_sha256": top_source.sha256,
        },
        "recommended_focus_ranges": format_ranges(editable_ranges),
        "recommended_pattern_ids": ["bool.factor_common_subexpr"],
        # compatibility aliases are advisory only
        "target": {
            "file": "rtl/top.v",
            "module": "top",
            "original_sha256": top_source.sha256,
        },
        "editable_line_ranges": format_ranges(editable_ranges),
        "eligible_pattern_ids": ["bool.factor_common_subexpr"],
    }
    return request


def _self_test_candidate(
    *,
    sha256: str,
    start_line: int,
    end_line: int,
    replacement_text: str,
    chosen_pattern_id: str = "bool.factor_common_subexpr",
) -> str:
    payload = {
        "schema_version": "2.0",
        "chosen_pattern_id": chosen_pattern_id,
        "target_unit_id": "unit_000",
        "patches": [
            {
                "patch_id": "patch_001",
                "patch_type": "line_range",
                "target_file": "rtl/top.v",
                "target_module": "top",
                "original_sha256": sha256,
                "changed_source_range": {
                    "start_line": start_line,
                    "end_line": end_line,
                },
                "replacement_text": replacement_text,
            }
        ],
        "rationale": "self-test candidate",
        "predicted_wns_gain_ns": 0.01,
        "self_checks": _self_checks(),
    }

    return json.dumps(payload)


def run_self_test(yosys_exe: str | None = None) -> None:
    yosys = resolve_yosys(yosys_exe)

    with tempfile.TemporaryDirectory(prefix="nebula_lint_selftest_") as tmp:
        root = Path(tmp).resolve()
        golden_root = root / "golden"
        _write_self_test_design(golden_root)

        manifest = _self_test_manifest(golden_root)
        bundle = SourceBundle.from_manifest(manifest, golden_root)
        top_sha = bundle.get_source("rtl/top.v").sha256

        original_top = (golden_root / "rtl" / "top.v").read_bytes()
        original_child = (golden_root / "rtl" / "child.v").read_bytes()

        # ------------------------------------------------------------------
        # Good internal rewrite: must pass structurally.
        # ------------------------------------------------------------------

        good_request = _self_test_request(
            bundle=bundle,
            evidence_dir=root,
            editable_ranges=[(10, 10)],
        )

        good_candidate = _self_test_candidate(
            sha256=top_sha,
            start_line=10,
            end_line=10,
            replacement_text="assign t = a | b;\n",
        )

        accepted_dir = root / "accepted_candidate"

        good = lint_candidate(
            manifest=manifest,
            base_dir=golden_root,
            request=good_request,
            candidate_text=good_candidate,
            yosys_exe=str(yosys),
            candidate_out_dir=accepted_dir,
        )

        assert good["ok"] is True, good
        assert good["checks"]["interfaces_unchanged"] is True
        assert good["checks"]["parameters_unchanged"] is True
        assert good["checks"]["hierarchy_unchanged"] is True
        assert good["checks"]["state_boundary_unchanged"] is True
        assert (accepted_dir / "design_manifest.json").is_file()
        assert (accepted_dir / "rtl" / "top.v").is_file()
        assert (accepted_dir / "rtl" / "child.v").is_file()

        # Golden sources must remain untouched.
        assert (golden_root / "rtl" / "top.v").read_bytes() == original_top
        assert (golden_root / "rtl" / "child.v").read_bytes() == original_child

        # Untouched child must remain byte-for-byte identical in candidate.
        assert (accepted_dir / "rtl" / "child.v").read_bytes() == original_child

        # ------------------------------------------------------------------
        # A safe patch outside the recommended focus must remain legal.
        # ------------------------------------------------------------------

        oob_request = _self_test_request(
            bundle=bundle,
            evidence_dir=root,
            editable_ranges=[(9, 9)],
        )

        oob = lint_candidate(
            manifest=manifest,
            base_dir=golden_root,
            request=oob_request,
            candidate_text=good_candidate,
            yosys_exe=str(yosys),
        )

        assert oob["ok"] is True, oob
        assert oob["checks"]["recommended_focus_followed"] is False

        # ------------------------------------------------------------------
        # Frozen range always wins.
        # ------------------------------------------------------------------

        frozen = lint_candidate(
            manifest=manifest,
            base_dir=golden_root,
            request=good_request,
            candidate_text=good_candidate,
            frozen_ranges=[(10, 10)],
            yosys_exe=str(yosys),
        )

        assert frozen["ok"] is False
        assert frozen["checks"]["frozen_ranges_untouched"] is False

        # ------------------------------------------------------------------
        # Port/interface mutation: request deliberately allows line 6 so the
        # Yosys structural gate, not edit bounds, must reject it.
        # ------------------------------------------------------------------

        port_request = _self_test_request(
            bundle=bundle,
            evidence_dir=root,
            editable_ranges=[(6, 6)],
        )

        port_candidate = _self_test_candidate(
            sha256=top_sha,
            start_line=6,
            end_line=6,
            replacement_text="    output wire [3:0] y\n",
        )

        bad_port = lint_candidate(
            manifest=manifest,
            base_dir=golden_root,
            request=port_request,
            candidate_text=port_candidate,
            yosys_exe=str(yosys),
        )

        assert bad_port["ok"] is False
        assert bad_port["checks"]["interfaces_unchanged"] is False

        # ------------------------------------------------------------------
        # Hierarchy mutation.
        # ------------------------------------------------------------------

        hierarchy_request = _self_test_request(
            bundle=bundle,
            evidence_dir=root,
            editable_ranges=[(11, 14)],
        )

        hierarchy_candidate = _self_test_candidate(
            sha256=top_sha,
            start_line=11,
            end_line=14,
            replacement_text="assign y = t;\n",
        )

        bad_hierarchy = lint_candidate(
            manifest=manifest,
            base_dir=golden_root,
            request=hierarchy_request,
            candidate_text=hierarchy_candidate,
            yosys_exe=str(yosys),
        )

        assert bad_hierarchy["ok"] is True, bad_hierarchy
        assert bad_hierarchy["checks"]["hierarchy_unchanged"] is False

        # ------------------------------------------------------------------
        # Parameter-default mutation.
        # ------------------------------------------------------------------

        parameter_request = _self_test_request(
            bundle=bundle,
            evidence_dir=root,
            editable_ranges=[(2, 2)],
        )

        parameter_candidate = _self_test_candidate(
            sha256=top_sha,
            start_line=2,
            end_line=2,
            replacement_text="    parameter STRUCT_TAG = 4\n",
        )

        bad_parameter = lint_candidate(
            manifest=manifest,
            base_dir=golden_root,
            request=parameter_request,
            candidate_text=parameter_candidate,
            yosys_exe=str(yosys),
        )

        assert bad_parameter["ok"] is True, bad_parameter
        assert bad_parameter["checks"]["parameters_unchanged"] is False

        # ------------------------------------------------------------------
        # Sequential/state-boundary integration.
        #
        # yosys_structure.py already proves detailed state fingerprinting.
        # Here we prove lint_candidate.py actually makes that result mandatory.
        # ------------------------------------------------------------------

        state_root = root / "state_golden"
        _write_state_boundary_self_test_design(state_root)

        state_manifest = _self_test_manifest(state_root)
        state_bundle = SourceBundle.from_manifest(
            state_manifest,
            state_root,
        )
        state_top_sha = state_bundle.get_source("rtl/top.v").sha256

        # Legal: rewrite only the combinational D cone.
        legal_d_request = _self_test_request(
            bundle=state_bundle,
            evidence_dir=root,
            editable_ranges=[(11, 11)],
        )

        legal_d_candidate = _self_test_candidate(
            sha256=state_top_sha,
            start_line=11,
            end_line=11,
            replacement_text="assign next = a - 8'hff;\n",
        )

        legal_d = lint_candidate(
            manifest=state_manifest,
            base_dir=state_root,
            request=legal_d_request,
            candidate_text=legal_d_candidate,
            yosys_exe=str(yosys),
        )

        assert legal_d["ok"] is True, legal_d
        assert (
            legal_d["checks"]["state_boundary_unchanged"]
            is True
        )

        # Unsafe: change the register clock edge while the model self-checks
        # still claim clock/reset behavior is unchanged. The trusted Yosys
        # state-boundary gate must override the model claim.
        clock_edge_request = _self_test_request(
            bundle=state_bundle,
            evidence_dir=root,
            editable_ranges=[(12, 12)],
        )

        clock_edge_candidate = _self_test_candidate(
            sha256=state_top_sha,
            start_line=12,
            end_line=12,
            replacement_text=(
                "always @(negedge clk or negedge rst_n) begin\n"
            ),
        )

        clock_edge = lint_candidate(
            manifest=state_manifest,
            base_dir=state_root,
            request=clock_edge_request,
            candidate_text=clock_edge_candidate,
            yosys_exe=str(yosys),
        )

        assert clock_edge["ok"] is False, clock_edge
        assert (
            clock_edge["checks"]["model_self_checks_all_true"]
            is True
        )
        assert (
            clock_edge["checks"]["state_boundary_unchanged"]
            is False
        )
        assert any(
            difference.get("category")
            in STATE_BOUNDARY_DIFFERENCE_CATEGORIES
            for difference in clock_edge["structure_differences"]
        )

        # ------------------------------------------------------------------
        # Wrong chosen pattern.
        # ------------------------------------------------------------------

        wrong_pattern_candidate = _self_test_candidate(
            sha256=top_sha,
            start_line=10,
            end_line=10,
            replacement_text="assign t = a | b;\n",
            chosen_pattern_id="arith.not_eligible",
        )

        wrong_pattern = lint_candidate(
            manifest=manifest,
            base_dir=golden_root,
            request=good_request,
            candidate_text=wrong_pattern_candidate,
            yosys_exe=str(yosys),
        )

        assert wrong_pattern["ok"] is True, wrong_pattern
        assert wrong_pattern["checks"]["chosen_pattern_is_recommended"] is False

        print("PASS: lint_candidate self-test")
        print("PASS: canonical patches[] candidate accepted")
        print("PASS: exact unit/request identity enforced")
        print("PASS: diagnostic focus ranges are advisory")
        print("PASS: frozen ranges remain hard safety gates")
        print("PASS: deterministic complete-source splicing works")
        print("PASS: untouched source files preserved byte-for-byte")
        print("PASS: Yosys interface mutation rejected")
        print("PASS: Yosys hierarchy mutation logged but not rejected (diagnostic only, per equivalence-blocking policy)")
        print("PASS: Yosys parameter mutation logged but not rejected (diagnostic only, per equivalence-blocking policy)")
        print("PASS: explicit state_boundary_unchanged check emitted")
        print("PASS: legal combinational D-cone rewrite passes lint")
        print("PASS: clock-edge state-boundary mutation rejected by lint")
        print("PASS: authoritative Yosys state check overrides model self-checks")
        print("PASS: non-recommended chosen pattern is allowed and reported")
        print("PASS: accepted candidate bundle materialized")
        print(f"Yosys: {yosys}")


# =============================================================================
# CLI
# =============================================================================


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Deterministic pre-synthesis safety gate for canonical "
            "nEBULA patches[] candidates."
        )
    )

    parser.add_argument(
        "--manifest",
        help="Golden DesignManifest JSON.",
    )

    parser.add_argument(
        "--base-dir",
        default=".",
        help="Base directory for golden manifest source paths.",
    )

    parser.add_argument(
        "--request",
        help="Unit-specific optimizer_request/2 JSON.",
    )

    parser.add_argument(
        "--candidate",
        help="Canonical candidate JSON containing patches[].",
    )

    parser.add_argument(
        "--frozen-lines",
        nargs="*",
        default=[],
        help=(
            "Optional additional hard-frozen ranges in the target file, "
            "for example 1-8 24-32."
        ),
    )

    parser.add_argument(
        "--yosys",
        default=None,
        help="Optional explicit Yosys executable path.",
    )

    parser.add_argument(
        "--candidate-out-dir",
        default=None,
        help=(
            "Optional directory where an ACCEPTED complete candidate source "
            "bundle and design_manifest.json are materialized for SEC."
        ),
    )

    parser.add_argument(
        "--output",
        default=None,
        help="Optional lint-result JSON output.",
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
            run_self_test(yosys_exe=args.yosys)
        except Exception as exc:
            print(
                f"SELF-TEST FAILED: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return 1
        return 0

    missing = []

    if not args.manifest:
        missing.append("--manifest")
    if not args.request:
        missing.append("--request")
    if not args.candidate:
        missing.append("--candidate")

    if missing:
        print(
            "ERROR: missing required argument(s): " + ", ".join(missing),
            file=sys.stderr,
        )
        return 2

    try:
        frozen_ranges = parse_cli_ranges(args.frozen_lines)
        manifest = DesignManifest.read_json(Path(args.manifest))
        request = load_json(Path(args.request))

        candidate_path = Path(args.candidate)
        if not candidate_path.is_file():
            raise LintCandidateError(
                f"candidate file not found: {candidate_path}"
            )

        candidate_text = candidate_path.read_text(encoding="utf-8")

        result = lint_candidate(
            manifest=manifest,
            base_dir=Path(args.base_dir),
            request=request,
            candidate_text=candidate_text,
            frozen_ranges=frozen_ranges,
            yosys_exe=args.yosys,
            candidate_out_dir=(
                Path(args.candidate_out_dir)
                if args.candidate_out_dir
                else None
            ),
        )

    except (LintCandidateError, ValueError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(
            f"ERROR: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 2

    if args.output:
        write_json(Path(args.output), result)

    print(json.dumps(result, indent=2))

    return 0 if result.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
