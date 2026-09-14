#!/usr/bin/env python3
"""
run_candidate_flow.py

Run an ACCEPTED nEBULA candidate through ORFS without modifying the golden RTL.

Why this file exists
--------------------
The golden ORFS config normally contains something like:

    export VERILOG_FILES = $(wildcard ./designs/src/$(DESIGN_NAME)/*.v)

If we simply call the normal baseline runner after linting a candidate, ORFS
would synthesize the GOLDEN RTL again. The resulting PPA numbers would be
meaningless.

This runner keeps the frozen physical experiment intact while overriding only
the logical RTL inputs from the candidate DesignManifest:

    frozen physical recipe
        |
        +-- exact design config / SDC / flow knobs / tool identity
        |
        v
    accepted candidate DesignManifest
        |
        +-- candidate source_files
        +-- include_dirs
        +-- defines
        +-- top parameters
        |
        v
    make DESIGN_CONFIG=<frozen config>
         FLOW_VARIANT=<unique candidate variant>
         VERILOG_FILES=<candidate files>
         VERILOG_INCLUDE_DIRS=<candidate include dirs>
         VERILOG_DEFINES=<candidate defines>
         VERILOG_TOP_PARAMS=<candidate params>
         <recipe stage target>

The golden source tree is never overwritten.

Stage mapping
-------------
    synth        -> ORFS target "synth"
    global_route -> ORFS target "grt"
    final        -> ORFS default target

A successful run must produce the expected ORFS stage JSON:
    synth        -> logs/.../1_synth.json
    global_route -> logs/.../5_1_grt.json
    final        -> logs/.../6_report.json

Normal usage
------------
python3 timing_automation/run_candidate_flow.py \
    --candidate-manifest ai_runs/cand_0001_sources/design_manifest.json \
    --candidate-base-dir ai_runs/cand_0001_sources \
    --physical-recipe ai_runs/mapper_global_route.physical_recipe.json \
    --candidate-id cand_0001

Then collect metrics with the SAME physical recipe:

python3 timing_automation/collect_metrics.py \
    --variant <printed FLOW_VARIANT> \
    --design mapper_reconvergent_test \
    --platform nangate45 \
    --physical-recipe ai_runs/mapper_global_route.physical_recipe.json \
    --output ai_runs/<variant>_global_route_metrics.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

try:
    from sta_netlist_compat import preserve_raw_and_sanitize, orfs_yosys_netlist_target, orfs_make_target
except ImportError:
    from timing_automation.sta_netlist_compat import preserve_raw_and_sanitize, orfs_yosys_netlist_target, orfs_make_target


# =============================================================================
# Local imports
# =============================================================================

try:
    from physical_recipe import PhysicalRecipe
except ImportError:
    from timing_automation.physical_recipe import PhysicalRecipe


# =============================================================================
# Constants
# =============================================================================

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_FLOW_DIR = SCRIPT_DIR.parent

RUN_METADATA_SCHEMA = "candidate_flow_run/1"

VALID_STAGES = (
    "synth",
    "global_route",
    "final",
)

STAGE_TARGETS: Dict[str, Tuple[str, ...]] = {
    "synth": ("synth",),
    "global_route": ("grt",),
    "final": (),
}

STAGE_JSON_FILES: Dict[str, str] = {
    "synth": "1_synth.json",
    "global_route": "5_1_grt.json",
    "final": "6_report.json",
}

SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


# =============================================================================
# Errors / data model
# =============================================================================

class CandidateFlowError(RuntimeError):
    """Candidate ORFS launch/validation failure."""


@dataclass(frozen=True)
class CandidateInputs:
    manifest_path: Path
    base_dir: Path
    design_id: str
    top: str
    source_files: Tuple[Path, ...]
    include_dirs: Tuple[Path, ...]
    defines: Tuple[str, ...]
    parameters: Dict[str, Any]
    source_hashes: Dict[str, str]
    input_fingerprint_sha256: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "manifest": str(self.manifest_path),
            "base_dir": str(self.base_dir),
            "design_id": self.design_id,
            "top": self.top,
            "source_files": [str(path) for path in self.source_files],
            "include_dirs": [str(path) for path in self.include_dirs],
            "defines": list(self.defines),
            "parameters": dict(self.parameters),
            "source_hashes": dict(self.source_hashes),
            "input_fingerprint_sha256": self.input_fingerprint_sha256,
        }


# =============================================================================
# Generic helpers
# =============================================================================

def load_json(path: Path) -> Dict[str, Any]:
    path = Path(path).expanduser().resolve()

    if not path.is_file():
        raise CandidateFlowError(f"JSON file not found: {path}")

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CandidateFlowError(
            f"invalid JSON in {path}: {exc}"
        ) from exc

    if not isinstance(value, dict):
        raise CandidateFlowError(
            f"JSON root must be an object: {path}"
        )

    return value


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def canonical_json_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")

    return hashlib.sha256(payload).hexdigest()


def sanitize_id(value: str) -> str:
    value = str(value).strip()
    if not value:
        raise CandidateFlowError(
            "candidate-id must be a non-empty string"
        )

    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", value)
    safe = safe.strip("._-")

    if not safe:
        raise CandidateFlowError(
            f"candidate-id {value!r} contains no usable characters"
        )

    return safe


def generate_flow_variant(candidate_id: str) -> str:
    safe_id = sanitize_id(candidate_id)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = uuid.uuid4().hex[:6]
    return f"{safe_id}_{timestamp}_{suffix}"


def resolve_manifest_path(
    raw: str,
    base_dir: Path,
) -> Path:
    path = Path(raw).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (base_dir / path).resolve()


def ensure_json_scalar(value: Any, *, field: str) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    raise CandidateFlowError(
        f"{field} must contain only scalar parameter values; "
        f"got {type(value).__name__}"
    )


# =============================================================================
# Candidate-manifest loading
# =============================================================================

def load_candidate_inputs(
    *,
    manifest_path: Path,
    base_dir: Optional[Path] = None,
) -> CandidateInputs:
    manifest_path = Path(manifest_path).expanduser().resolve()
    data = load_json(manifest_path)

    root = (
        Path(base_dir).expanduser().resolve()
        if base_dir is not None
        else manifest_path.parent.resolve()
    )

    design_id = data.get("design_id")
    top = data.get("top")

    if not isinstance(design_id, str) or not design_id.strip():
        raise CandidateFlowError(
            "candidate manifest design_id must be a non-empty string"
        )

    if not isinstance(top, str) or not top.strip():
        raise CandidateFlowError(
            "candidate manifest top must be a non-empty string"
        )

    raw_sources = data.get("source_files")

    if not isinstance(raw_sources, list) or not raw_sources:
        raise CandidateFlowError(
            "candidate manifest source_files must be a non-empty array"
        )

    source_files: List[Path] = []
    logical_sources: List[str] = []

    for index, item in enumerate(raw_sources):
        if not isinstance(item, str) or not item.strip():
            raise CandidateFlowError(
                f"source_files[{index}] must be a non-empty string"
            )

        logical = item
        resolved = resolve_manifest_path(logical, root)

        if not resolved.is_file():
            raise CandidateFlowError(
                f"candidate source file not found: {resolved}"
            )

        logical_sources.append(logical)
        source_files.append(resolved)

    raw_hashes = data.get("source_hashes")

    if not isinstance(raw_hashes, dict):
        raise CandidateFlowError(
            "candidate manifest source_hashes must be an object"
        )

    verified_hashes: Dict[str, str] = {}

    for logical, resolved in zip(logical_sources, source_files):
        expected = raw_hashes.get(logical)

        if (
            not isinstance(expected, str)
            or not SHA256_RE.fullmatch(expected)
        ):
            raise CandidateFlowError(
                f"candidate manifest has invalid/missing source hash "
                f"for {logical!r}"
            )

        actual = sha256_file(resolved)

        if actual != expected.lower():
            raise CandidateFlowError(
                "candidate source hash mismatch: "
                f"{logical}: expected={expected.lower()}, actual={actual}"
            )

        verified_hashes[logical] = actual

    raw_include_dirs = data.get("include_dirs", [])

    if raw_include_dirs is None:
        raw_include_dirs = []

    if not isinstance(raw_include_dirs, list):
        raise CandidateFlowError(
            "candidate manifest include_dirs must be an array"
        )

    include_dirs: List[Path] = []

    for index, item in enumerate(raw_include_dirs):
        if not isinstance(item, str) or not item.strip():
            raise CandidateFlowError(
                f"include_dirs[{index}] must be a non-empty string"
            )

        resolved = resolve_manifest_path(item, root)

        if not resolved.is_dir():
            raise CandidateFlowError(
                f"candidate include directory not found: {resolved}"
            )

        if resolved not in include_dirs:
            include_dirs.append(resolved)

    raw_defines = data.get("defines", [])

    if raw_defines is None:
        raw_defines = []

    if not isinstance(raw_defines, list):
        raise CandidateFlowError(
            "candidate manifest defines must be an array"
        )

    defines: List[str] = []

    for index, item in enumerate(raw_defines):
        if not isinstance(item, str) or not item.strip():
            raise CandidateFlowError(
                f"defines[{index}] must be a non-empty string"
            )

        define = item.strip()

        # The DesignManifest convention is logical NAME or NAME=VALUE.
        # Accept a leading -D too, but normalize it away here.
        if define.startswith("-D"):
            define = define[2:].strip()

        if not define:
            raise CandidateFlowError(
                f"defines[{index}] is empty after normalization"
            )

        defines.append(define)

    raw_parameters = data.get("parameters", {})

    if raw_parameters is None:
        raw_parameters = {}

    if not isinstance(raw_parameters, dict):
        raise CandidateFlowError(
            "candidate manifest parameters must be an object"
        )

    parameters: Dict[str, Any] = {}

    for key, value in raw_parameters.items():
        if not isinstance(key, str) or not key.strip():
            raise CandidateFlowError(
                "candidate manifest parameter names must be non-empty strings"
            )

        parameters[key] = ensure_json_scalar(
            value,
            field=f"parameters.{key}",
        )

    fingerprint_payload = {
        "design_id": design_id,
        "top": top,
        "source_files": [
            {
                "logical_path": logical,
                "resolved_path": str(resolved),
                "sha256": verified_hashes[logical],
            }
            for logical, resolved in zip(
                logical_sources,
                source_files,
            )
        ],
        "include_dirs": [
            str(path)
            for path in include_dirs
        ],
        "defines": defines,
        "parameters": parameters,
    }

    return CandidateInputs(
        manifest_path=manifest_path,
        base_dir=root,
        design_id=design_id,
        top=top,
        source_files=tuple(source_files),
        include_dirs=tuple(include_dirs),
        defines=tuple(defines),
        parameters=parameters,
        source_hashes=verified_hashes,
        input_fingerprint_sha256=canonical_json_sha256(
            fingerprint_payload
        ),
    )


# =============================================================================
# Recipe validation
# =============================================================================

def recipe_file_for_role(
    recipe: PhysicalRecipe,
    role: str,
) -> Path:
    matches = [
        item
        for item in recipe.files
        if item.role == role
    ]

    if len(matches) != 1:
        raise CandidateFlowError(
            f"physical recipe must contain exactly one {role!r} file; "
            f"found {len(matches)}"
        )

    item = matches[0]

    logical = Path(item.logical_path).expanduser()
    base = Path(recipe.source_base_dir).expanduser().resolve()

    return (
        logical.resolve()
        if logical.is_absolute()
        else (base / logical).resolve()
    )


def validate_recipe_for_candidate(
    *,
    recipe: PhysicalRecipe,
    candidate: CandidateInputs,
    flow_dir: Path,
) -> Path:
    recipe.validate()

    verification = recipe.verify_frozen_files()

    if verification.get("ok") is not True:
        bad = [
            item
            for item in verification.get("files", [])
            if item.get("matches") is not True
        ]

        summary = "; ".join(
            f"{item.get('role')}:{item.get('path')}"
            for item in bad
        ) or "unknown frozen-file mismatch"

        raise CandidateFlowError(
            "physical recipe frozen files no longer match: "
            + summary
        )

    if recipe.stage not in VALID_STAGES:
        raise CandidateFlowError(
            f"unsupported recipe stage {recipe.stage!r}"
        )

    if recipe.design_id != candidate.design_id:
        raise CandidateFlowError(
            "candidate/recipe design mismatch: "
            f"candidate={candidate.design_id!r}, "
            f"recipe={recipe.design_id!r}"
        )

    if recipe.top != candidate.top:
        raise CandidateFlowError(
            "candidate/recipe top mismatch: "
            f"candidate={candidate.top!r}, "
            f"recipe={recipe.top!r}"
        )

    source_base = Path(recipe.source_base_dir).expanduser().resolve()

    # P0 invariant: recipe was frozen from this ORFS flow tree.
    if source_base != flow_dir:
        raise CandidateFlowError(
            "physical recipe source_base_dir does not match --flow-dir: "
            f"recipe={source_base}, flow_dir={flow_dir}"
        )

    config = recipe_file_for_role(
        recipe,
        "design_config",
    )

    if not config.is_file():
        raise CandidateFlowError(
            f"frozen design config not found: {config}"
        )

    return config


# =============================================================================
# ORFS variable construction
# =============================================================================

def orfs_verilog_defines(
    defines: Sequence[str],
) -> str:
    # ORFS/Yosys accepts -DNAME and -DNAME=VALUE.
    return " ".join(
        f"-D{define}"
        for define in defines
    )


def orfs_top_params(
    parameters: Mapping[str, Any],
) -> str:
    tokens: List[str] = []

    for key in sorted(parameters):
        value = parameters[key]

        if isinstance(value, bool):
            rendered = "1" if value else "0"
        elif value is None:
            raise CandidateFlowError(
                f"top parameter {key!r} has null value"
            )
        else:
            rendered = str(value)

        tokens.extend([key, rendered])

    return " ".join(tokens)


def build_make_command(
    *,
    flow_dir: Path,
    design_config: Path,
    recipe: PhysicalRecipe,
    candidate: CandidateInputs,
    flow_variant: str,
    report_checks_tcl: Optional[Path],
) -> List[str]:
    from physical_recipe import frozen_physical_variables
    physical_variables = frozen_physical_variables(recipe)
    command = [
        "make",
        f"DESIGN_CONFIG={design_config}",
        f"FLOW_VARIANT={flow_variant}",
        "VERILOG_FILES="
        + " ".join(
            str(path)
            for path in candidate.source_files
        ),
    ]

    if candidate.include_dirs:
        command.append(
            "VERILOG_INCLUDE_DIRS="
            + " ".join(
                str(path)
                for path in candidate.include_dirs
            )
        )

    if candidate.defines:
        command.append(
            "VERILOG_DEFINES="
            + orfs_verilog_defines(candidate.defines)
        )

    if candidate.parameters:
        command.append(
            "VERILOG_TOP_PARAMS="
            + orfs_top_params(candidate.parameters)
        )

    # The baseline run already uses this. It is harmless for earlier stage
    # targets because the final-report hook is not reached.
    if (
        report_checks_tcl is not None
        and report_checks_tcl.is_file()
    ):
        command.append(
            f"POST_FINAL_REPORT_TCL={report_checks_tcl}"
        )

    command.extend(f"{key}={value}" for key, value in sorted(physical_variables.items()))
    command.extend(STAGE_TARGETS[recipe.stage])

    return command


# =============================================================================
# Run metadata
# =============================================================================

def build_yosys_checkpoint_command(
    *,
    command: Sequence[str],
    recipe: PhysicalRecipe,
    flow_dir: Path,
    flow_variant: str,
) -> tuple[list[str], Path]:
    """Build the pre-OpenSTA ORFS command for the exact Yosys netlist target."""
    targets = STAGE_TARGETS[recipe.stage]
    base_command = list(command[:-len(targets)]) if targets else list(command)
    netlist = orfs_yosys_netlist_target(
        flow_dir, recipe.platform, recipe.design_id, flow_variant
    )
    make_target = orfs_make_target(flow_dir, netlist)
    return [*base_command, make_target], netlist


def metadata_path(
    flow_dir: Path,
    flow_variant: str,
) -> Path:
    return (
        flow_dir
        / "ai_runs"
        / f"{flow_variant}.candidate_flow.json"
    )


def write_metadata(
    path: Path,
    value: Mapping[str, Any],
) -> None:
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    path.write_text(
        json.dumps(
            value,
            indent=2,
            sort_keys=False,
        )
        + "\n",
        encoding="utf-8",
    )


def expected_stage_json(
    *,
    flow_dir: Path,
    recipe: PhysicalRecipe,
    flow_variant: str,
) -> Path:
    return (
        flow_dir
        / "logs"
        / recipe.platform
        / recipe.design_id
        / flow_variant
        / STAGE_JSON_FILES[recipe.stage]
    )


# =============================================================================
# Main runner
# =============================================================================

def run_candidate_flow(
    *,
    candidate_manifest_path: Path,
    candidate_base_dir: Optional[Path],
    physical_recipe_path: Path,
    candidate_id: str,
    flow_dir: Path = DEFAULT_FLOW_DIR,
    flow_variant: Optional[str] = None,
    report_checks_tcl: Optional[Path] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    flow_dir = Path(flow_dir).expanduser().resolve()

    if not (flow_dir / "Makefile").is_file():
        raise CandidateFlowError(
            f"ORFS Makefile not found under flow directory: {flow_dir}"
        )

    candidate = load_candidate_inputs(
        manifest_path=candidate_manifest_path,
        base_dir=candidate_base_dir,
    )

    recipe = PhysicalRecipe.read_json(
        Path(physical_recipe_path)
    )

    design_config = validate_recipe_for_candidate(
        recipe=recipe,
        candidate=candidate,
        flow_dir=flow_dir,
    )

    variant = (
        str(flow_variant).strip()
        if flow_variant is not None
        else generate_flow_variant(candidate_id)
    )

    if not variant:
        raise CandidateFlowError(
            "flow variant must not be empty"
        )

    if report_checks_tcl is None:
        default_report = (
            flow_dir
            / "timing_automation"
            / "report_checks.tcl"
        )
        report_checks_tcl = (
            default_report
            if default_report.is_file()
            else None
        )
    else:
        report_checks_tcl = (
            Path(report_checks_tcl)
            .expanduser()
            .resolve()
        )

    command = build_make_command(
        flow_dir=flow_dir,
        design_config=design_config,
        recipe=recipe,
        candidate=candidate,
        flow_variant=variant,
        report_checks_tcl=report_checks_tcl,
    )

    stage_json = expected_stage_json(
        flow_dir=flow_dir,
        recipe=recipe,
        flow_variant=variant,
    )

    combined_input_sha = canonical_json_sha256(
        {
            "candidate_input_fingerprint_sha256": (
                candidate.input_fingerprint_sha256
            ),
            "physical_recipe_fingerprint_sha256": (
                recipe.fingerprint_sha256.lower()
            ),
            "stage": recipe.stage,
            "design_id": recipe.design_id,
            "top": recipe.top,
            "platform": recipe.platform,
        }
    )

    metadata: Dict[str, Any] = {
        "schema": RUN_METADATA_SCHEMA,
        "ok": False,
        "dry_run": bool(dry_run),
        "candidate_id": sanitize_id(candidate_id),
        "flow_variant": variant,
        "design": {
            "design_id": recipe.design_id,
            "top": recipe.top,
            "platform": recipe.platform,
        },
        "evaluation": {
            "stage": recipe.stage,
            "recipe_id": recipe.recipe_id,
            "recipe_fingerprint_sha256": (
                recipe.fingerprint_sha256.lower()
            ),
        },
        "candidate_inputs": candidate.to_dict(),
        "combined_input_sha256": combined_input_sha,
        "design_config": str(design_config),
        "command": command,
        "command_shell_display": shlex.join(command),
        "cwd": str(flow_dir),
        "expected_stage_json": str(stage_json),
        "returncode": None,
        "stage_output_exists": False,
    }

    out_meta = metadata_path(
        flow_dir,
        variant,
    )

    print("\n========================================")
    print("Starting candidate ORFS flow")
    print("========================================")
    print(f"Candidate     : {candidate_id}")
    print(f"Design        : {recipe.design_id}")
    print(f"Top           : {recipe.top}")
    print(f"Platform      : {recipe.platform}")
    print(f"Stage         : {recipe.stage}")
    print(f"Flow Variant  : {variant}")
    print(
        "Recipe SHA    : "
        f"{recipe.fingerprint_sha256.lower()}"
    )
    print(
        "Candidate SHA : "
        f"{candidate.input_fingerprint_sha256}"
    )
    print(f"Config        : {design_config}")
    print("Golden RTL    : NOT MODIFIED")
    print("========================================\n")

    print("Running command:")
    print(shlex.join(command))
    print()

    if dry_run:
        metadata["ok"] = True
        metadata["dry_run"] = True
        write_metadata(out_meta, metadata)

        print("DRY RUN: ORFS was not executed.")
        print(f"Run metadata: {out_meta}")

        return metadata

    # ORFS's phony ``synth`` target includes OpenROAD synth_odb, so it is
    # already too late for a Yosys->OpenSTA compatibility transform.  Build
    # the exact Yosys netlist target first for *every* requested stage, preserve
    # the raw implementation, sanitize the STA-facing copy, then resume the
    # original stage command.
    yosys_command, netlist = build_yosys_checkpoint_command(
        command=command, recipe=recipe, flow_dir=flow_dir, flow_variant=variant
    )
    raw_netlist = netlist.with_name("1_2_yosys.raw.v")
    compat_meta = netlist.with_name("1_2_yosys_sta_compat.json")
    metadata["synthesis_command"] = yosys_command
    metadata["synthesis_command_shell_display"] = shlex.join(yosys_command)
    metadata["yosys_netlist_target"] = str(netlist)
    metadata["synthesis_netlist"] = str(netlist)
    metadata["synthesis_netlist_raw"] = str(raw_netlist)
    metadata["synthesis_netlist_sta"] = str(netlist)

    synth_proc = subprocess.run(yosys_command, cwd=str(flow_dir), text=True)
    metadata["synthesis_returncode"] = synth_proc.returncode
    if synth_proc.returncode != 0 or not netlist.is_file():
        metadata["returncode"] = synth_proc.returncode or 1
        metadata["stage_output_exists"] = False
        metadata["ok"] = False
        metadata["failure_reason"] = (
            "yosys_target_failed" if synth_proc.returncode != 0 else "synthesis_netlist_not_found"
        )
        write_metadata(out_meta, metadata)
        raise CandidateFlowError(metadata["failure_reason"] + f"; metadata: {out_meta}")
    try:
        metadata["sta_netlist_compat"] = preserve_raw_and_sanitize(
            netlist, raw_path=raw_netlist, metadata_path=compat_meta
        )
    except Exception as exc:
        metadata["returncode"] = 1
        metadata["stage_output_exists"] = False
        metadata["ok"] = False
        metadata["failure_reason"] = "sta_netlist_compat_failed"
        metadata["sta_netlist_compat"] = {
            "status": "failed", "reason": f"{type(exc).__name__}: {exc}"
        }
        write_metadata(out_meta, metadata)
        raise CandidateFlowError(
            f"candidate STA-netlist compatibility failed: {exc}; metadata: {out_meta}"
        ) from exc

    proc = subprocess.run(command, cwd=str(flow_dir), text=True)

    metadata["returncode"] = proc.returncode
    metadata["stage_output_exists"] = stage_json.is_file()

    metadata["ok"] = bool(proc.returncode == 0 and stage_json.is_file())

    write_metadata(
        out_meta,
        metadata,
    )

    if proc.returncode != 0:
        metadata["failure_reason"] = (
            "synth_odb_failed" if recipe.stage == "synth" else "physical_stage_failed"
        )
        write_metadata(out_meta, metadata)
        raise CandidateFlowError(
            "candidate ORFS flow failed with "
            f"return code {proc.returncode}; "
            f"metadata: {out_meta}"
        )

    if not stage_json.is_file():
        metadata["failure_reason"] = "stage_metrics_missing"
        write_metadata(out_meta, metadata)
        raise CandidateFlowError(
            "ORFS returned success but expected stage metric JSON "
            f"was not produced: {stage_json}"
        )

    print("\n========================================")
    print("Candidate ORFS flow completed")
    print("========================================")
    print(f"Design        : {recipe.design_id}")
    print(f"Platform      : {recipe.platform}")
    print(f"Stage         : {recipe.stage}")
    print(f"Flow Variant  : {variant}")
    print(f"Stage JSON    : {stage_json}")
    print(
        "Recipe SHA    : "
        f"{recipe.fingerprint_sha256.lower()}"
    )
    print(
        "Candidate SHA : "
        f"{candidate.input_fingerprint_sha256}"
    )
    print(f"Run metadata  : {out_meta}")
    print("========================================")

    return metadata


# =============================================================================
# Self-test
# =============================================================================

def run_self_test() -> None:
    try:
        from physical_recipe import build_physical_recipe
    except ImportError:
        from timing_automation.physical_recipe import (
            build_physical_recipe,
        )

    with tempfile.TemporaryDirectory(
        prefix="nebula_candidate_flow_"
    ) as tmp:
        root = Path(tmp).resolve()
        flow_dir = root / "flow"
        candidate_root = root / "candidate"

        flow_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        candidate_root.mkdir(
            parents=True,
            exist_ok=True,
        )

        (flow_dir / "Makefile").write_text(
            "# self-test ORFS Makefile placeholder\n",
            encoding="utf-8",
        )

        design_dir = (
            flow_dir
            / "designs"
            / "nangate45"
            / "demo"
        )

        design_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        config = design_dir / "config.mk"
        sdc = design_dir / "constraint.sdc"

        config.write_text(
            "export PLATFORM = nangate45\n"
            "export DESIGN_NAME = demo\n"
            "export VERILOG_FILES = ./golden.v\n"
            "export CORE_UTILIZATION = 40\n",
            encoding="utf-8",
        )

        sdc.write_text(
            "create_clock -period 1.0 [get_ports clk]\n",
            encoding="utf-8",
        )

        rtl_dir = candidate_root / "rtl"
        inc_dir = candidate_root / "include"

        rtl_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        inc_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        top = rtl_dir / "top.v"
        helper = rtl_dir / "helper.v"

        top.write_text(
            "module demo(input clk, input a, output y);\n"
            "  helper u(.a(a), .y(y));\n"
            "endmodule\n",
            encoding="utf-8",
        )

        helper.write_text(
            "module helper(input a, output y);\n"
            "  assign y = a;\n"
            "endmodule\n",
            encoding="utf-8",
        )

        manifest = {
            "dataset": "self_test",
            "design_id": "demo",
            "top": "demo",
            "source_files": [
                "rtl/top.v",
                "rtl/helper.v",
            ],
            "include_dirs": ["include"],
            "defines": [
                "FEATURE=1",
                "-DSECOND_FLAG",
            ],
            "parameters": {
                "WIDTH": 8,
                "ENABLE": True,
            },
            "language": "verilog",
            "source_hashes": {
                "rtl/top.v": sha256_file(top),
                "rtl/helper.v": sha256_file(helper),
            },
        }

        manifest_path = (
            candidate_root
            / "design_manifest.json"
        )

        manifest_path.write_text(
            json.dumps(
                manifest,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        recipe_spec = {
            "schema": "physical_recipe_spec/1",
            "recipe_id": "demo_global_route_v1",
            "design": {
                "design_id": "demo",
                "top": "demo",
                "platform": "nangate45",
            },
            "stage": "global_route",
            "files": [
                {
                    "role": "design_config",
                    "path": (
                        "designs/nangate45/"
                        "demo/config.mk"
                    ),
                },
                {
                    "role": "constraints",
                    "path": (
                        "designs/nangate45/"
                        "demo/constraint.sdc"
                    ),
                },
            ],
            "flow_variables": {
                "CORE_UTILIZATION": "",
                "DIE_AREA": "0 0 100 100",
                "CORE_AREA": "2 2 98 98",
            },
            "stage_settings": {
                "stop_after": "global_route",
            },
            "toolchain": {
                "orfs_revision": "selftest",
                "openroad_revision": "selftest",
                "yosys_revision": "selftest",
            },
            "metadata": {},
        }

        recipe = build_physical_recipe(
            recipe_spec,
            base_dir=flow_dir,
        )

        recipe_path = root / "recipe.json"
        recipe.write_json(recipe_path)

        candidate = load_candidate_inputs(
            manifest_path=manifest_path,
            base_dir=candidate_root,
        )

        assert candidate.design_id == "demo"
        assert candidate.top == "demo"
        assert len(candidate.source_files) == 2
        assert len(candidate.include_dirs) == 1
        assert candidate.defines == (
            "FEATURE=1",
            "SECOND_FLAG",
        )

        design_config = validate_recipe_for_candidate(
            recipe=recipe,
            candidate=candidate,
            flow_dir=flow_dir,
        )

        command = build_make_command(
            flow_dir=flow_dir,
            design_config=design_config,
            recipe=recipe,
            candidate=candidate,
            flow_variant="cand_selftest",
            report_checks_tcl=None,
        )

        joined = "\n".join(command)

        assert command[0] == "make"
        assert "FLOW_VARIANT=cand_selftest" in command
        assert any(
            token.startswith("VERILOG_FILES=")
            for token in command
        )
        assert any(
            token.startswith("VERILOG_INCLUDE_DIRS=")
            for token in command
        )
        assert (
            "VERILOG_DEFINES=-DFEATURE=1 -DSECOND_FLAG"
            in command
        )
        assert (
            "VERILOG_TOP_PARAMS=ENABLE 1 WIDTH 8"
            in command
        )
        assert command[-1] == "grt"

        # Golden source path must never appear in candidate RTL override.
        verilog_arg = next(
            token
            for token in command
            if token.startswith("VERILOG_FILES=")
        )

        assert "./golden.v" not in verilog_arg
        assert str(top.resolve()) in verilog_arg
        assert str(helper.resolve()) in verilog_arg

        # Dry-run exercises complete validation/metadata without invoking make.
        result = run_candidate_flow(
            candidate_manifest_path=manifest_path,
            candidate_base_dir=candidate_root,
            physical_recipe_path=recipe_path,
            candidate_id="cand_0001",
            flow_dir=flow_dir,
            flow_variant="cand_0001_selftest",
            dry_run=True,
        )

        assert result["ok"] is True
        assert result["dry_run"] is True
        assert (
            result["evaluation"]["stage"]
            == "global_route"
        )
        assert (
            result["evaluation"][
                "recipe_fingerprint_sha256"
            ]
            == recipe.fingerprint_sha256
        )

        # Stale candidate source bytes must reject before make.
        top.write_text(
            top.read_text(encoding="utf-8")
            + "// mutation\n",
            encoding="utf-8",
        )

        try:
            load_candidate_inputs(
                manifest_path=manifest_path,
                base_dir=candidate_root,
            )
        except CandidateFlowError as exc:
            assert "source hash mismatch" in str(exc)
        else:
            raise AssertionError(
                "stale candidate source hash was accepted"
            )

        print("PASS: run_candidate_flow self-test")
        print(
            "PASS: complete candidate source manifest verified"
        )
        print(
            "PASS: candidate source SHA mismatch rejected before ORFS"
        )
        print(
            "PASS: frozen physical recipe verified before ORFS"
        )
        print(
            "PASS: recipe design/top identity enforced"
        )
        print(
            "PASS: candidate VERILOG_FILES override built from accepted bundle"
        )
        print(
            "PASS: include dirs / defines / top parameters propagated"
        )
        print(
            "PASS: golden VERILOG_FILES are not used for candidate run"
        )
        print(
            "PASS: global_route recipe maps to ORFS grt target"
        )
        print(
            "PASS: unique candidate FLOW_VARIANT metadata supported"
        )
        print(
            "PASS: dry-run performs full validation without invoking ORFS"
        )


# =============================================================================
# CLI
# =============================================================================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run an accepted nEBULA candidate through ORFS "
            "using a frozen physical recipe without modifying golden RTL."
        )
    )

    parser.add_argument(
        "--candidate-manifest",
        help=(
            "Accepted candidate DesignManifest, normally "
            "ai_runs/<candidate>_sources/design_manifest.json"
        ),
    )

    parser.add_argument(
        "--candidate-base-dir",
        default=None,
        help=(
            "Base directory for candidate manifest source paths. "
            "Defaults to the manifest's parent directory."
        ),
    )

    parser.add_argument(
        "--physical-recipe",
        help="Frozen physical_recipe/1 JSON.",
    )

    parser.add_argument(
        "--candidate-id",
        default="candidate",
        help="Logical candidate identifier used in FLOW_VARIANT.",
    )

    parser.add_argument(
        "--flow-dir",
        default=str(DEFAULT_FLOW_DIR),
        help="ORFS flow directory containing Makefile.",
    )

    parser.add_argument(
        "--flow-variant",
        default=None,
        help=(
            "Optional explicit FLOW_VARIANT. "
            "Normally omitted so a unique candidate variant is generated."
        ),
    )

    parser.add_argument(
        "--report-checks-tcl",
        default=None,
        help=(
            "Optional POST_FINAL_REPORT_TCL. "
            "Defaults to timing_automation/report_checks.tcl if present."
        ),
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and print the ORFS command without running make.",
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
                f"SELF-TEST FAILED: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return 1

        return 0

    missing = []

    if not args.candidate_manifest:
        missing.append("--candidate-manifest")

    if not args.physical_recipe:
        missing.append("--physical-recipe")

    if missing:
        print(
            "ERROR: missing required argument(s): "
            + ", ".join(missing),
            file=sys.stderr,
        )
        return 2

    try:
        result = run_candidate_flow(
            candidate_manifest_path=Path(
                args.candidate_manifest
            ),
            candidate_base_dir=(
                Path(args.candidate_base_dir)
                if args.candidate_base_dir
                else None
            ),
            physical_recipe_path=Path(
                args.physical_recipe
            ),
            candidate_id=args.candidate_id,
            flow_dir=Path(args.flow_dir),
            flow_variant=args.flow_variant,
            report_checks_tcl=(
                Path(args.report_checks_tcl)
                if args.report_checks_tcl
                else None
            ),
            dry_run=args.dry_run,
        )
    except CandidateFlowError as exc:
        print(
            f"ERROR: {exc}",
            file=sys.stderr,
        )
        return 2
    except Exception as exc:
        print(
            f"ERROR: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 2

    print(
        json.dumps(
            result,
            indent=2,
        )
    )

    return 0 if result.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
