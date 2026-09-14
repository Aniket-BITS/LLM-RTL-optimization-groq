#!/usr/bin/env python3
"""
run_stage_reports.py

Generate nEBULA timing/path reports from an EXISTING ORFS physical stage.

Why this exists
---------------
The normal report_checks.tcl is currently sourced by ORFS during the final
reporting stage.  If nEBULA evaluates PPA at global_route, then parsing those
final-stage reports mixes two different physical timing states:

    global_route metrics  +  final timing reports   <-- invalid

This runner reopens the exact global-route database/SDC and sources the same
report_checks.tcl with REPORTS_DIR redirected to a stage-specific directory:

    results/.../<variant>/5_1_grt.odb
    results/.../<variant>/5_1_grt.sdc
                    |
                    v
              OpenROAD
                    |
        read Liberty / read_db / read_sdc
        source setRC.tcl
        estimate_parasitics -global_routing
                    |
                    v
           report_checks.tcl
                    |
                    v
    reports/.../<variant>/stages/global_route/

The ordinary final-stage reports are never overwritten.

P0 scope
--------
This first version intentionally supports only:

    global_route

because that is the physical stage frozen by the current nEBULA recipe and
because its authoritative ORFS handoff files are known exactly:

    5_1_grt.odb
    5_1_grt.sdc

Do not silently guess handoff files for other stages.

Example
-------
python3 timing_automation/run_stage_reports.py \
    --variant run_20260907_114531_c62c53 \
    --design mapper_reconvergent_test \
    --platform nangate45 \
    --stage global_route

Outputs
-------
reports/<platform>/<design>/<variant>/stages/global_route/
    critical_paths.rpt
    reg2reg_critical_paths.rpt
    critical_paths.json          (when supported)
    a_to_y.rpt                   (design-specific optional report)
    b_to_y.rpt
    c_to_y.rpt
    sel_to_y.rpt
    ...

and:

ai_runs/<variant>_global_route_stage_reports.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

try:
    from nebula_env import NebulaEnvError, openroad as resolve_nebula_openroad
except ImportError:
    from timing_automation.nebula_env import NebulaEnvError, openroad as resolve_nebula_openroad


# =============================================================================
# Constants
# =============================================================================

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_FLOW_DIR = SCRIPT_DIR.parent

SCHEMA = "stage_reports/1"

SUPPORTED_STAGES = ("floorplan", "place", "cts", "global_route", "detailed_route", "final")

# Ordered candidate handoffs. ORFS naming has changed across releases; the
# runner accepts only an existing ODB/SDC pair and never guesses across stages.
STAGE_HANDOFFS: Dict[str, Tuple[Tuple[str, str], ...]] = {
    "floorplan": (("2_1_floorplan.odb", "2_1_floorplan.sdc"),),
    "place": (("3_5_place_dp.odb", "3_5_place_dp.sdc"), ("3_4_place_resized.odb", "3_4_place_resized.sdc")),
    "cts": (("4_1_cts.odb", "4_1_cts.sdc"),),
    "global_route": (("5_1_grt.odb", "5_1_grt.sdc"),),
    "detailed_route": (("6_final.odb", "6_final.sdc"), ("6_1_route.odb", "6_1_route.sdc")),
    "final": (("6_final.odb", "6_final.sdc"),),
}

REQUIRED_REPORTS = ()

OPTIONAL_REPORTS = (
    "reg2reg_critical_paths.rpt",
    "critical_paths.json",
    "a_to_y.rpt",
    "b_to_y.rpt",
    "c_to_y.rpt",
    "sel_to_y.rpt",
    "a27_to_y27.rpt",
    "b27_to_y27.rpt",
    "c27_to_y27.rpt",
)


# =============================================================================
# Errors / data model
# =============================================================================

class StageReportError(RuntimeError):
    """Stage-report setup or execution failure."""


@dataclass(frozen=True)
class StageReportPlan:
    flow_dir: Path
    design: str
    platform: str
    variant: str
    stage: str
    odb: Path
    sdc: Path
    liberty_files: Tuple[Path, ...]
    setrc_tcl: Path
    report_checks_tcl: Path
    report_dir: Path
    tcl_script: Path
    metadata_path: Path
    env_overrides: Tuple[Tuple[str, str], ...] = ()
    report_subdir: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "flow_dir": str(self.flow_dir),
            "design": self.design,
            "platform": self.platform,
            "variant": self.variant,
            "stage": self.stage,
            "odb": str(self.odb),
            "sdc": str(self.sdc),
            "liberty_files": [
                str(path)
                for path in self.liberty_files
            ],
            "setrc_tcl": str(self.setrc_tcl),
            "report_checks_tcl": str(
                self.report_checks_tcl
            ),
            "report_dir": str(self.report_dir),
            "tcl_script": str(self.tcl_script),
            "metadata_path": str(
                self.metadata_path
            ),
            "env_overrides": dict(self.env_overrides),
            "report_subdir": self.report_subdir,
        }


# =============================================================================
# Generic helpers
# =============================================================================

def sha256_file(path: Path) -> str:
    return hashlib.sha256(
        Path(path).read_bytes()
    ).hexdigest()


def write_json(
    path: Path,
    payload: Dict[str, Any],
) -> None:
    path = Path(path)
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )
    path.write_text(
        json.dumps(
            payload,
            indent=2,
            sort_keys=False,
        )
        + "\n",
        encoding="utf-8",
    )


def tcl_quote(value: Path | str) -> str:
    """
    Quote a path/string safely for Tcl using braces.

    Reject literal braces because silently escaping arbitrary Tcl here is not
    useful for nEBULA's controlled ORFS paths.
    """

    text = str(value)

    if "{" in text or "}" in text:
        raise StageReportError(
            "Tcl path/string contains unsupported brace character: "
            f"{text!r}"
        )

    return "{" + text + "}"


def require_file(
    path: Path,
    *,
    label: str,
) -> Path:
    path = Path(path).expanduser().resolve()

    if not path.is_file():
        raise StageReportError(
            f"{label} not found: {path}"
        )

    return path


def require_nonempty(
    value: str,
    *,
    label: str,
) -> str:
    value = str(value).strip()

    if not value:
        raise StageReportError(
            f"{label} must be a non-empty string"
        )

    return value


# =============================================================================
# ORFS/OpenROAD discovery
# =============================================================================

def resolve_openroad(
    *,
    flow_dir: Path,
    explicit: Optional[str] = None,
) -> Path:
    try:
        return resolve_nebula_openroad(explicit, flow=flow_dir)
    except NebulaEnvError as exc:
        raise StageReportError(str(exc)) from exc


def discover_liberty_files(
    *,
    flow_dir: Path,
    platform: str,
    explicit: Sequence[str],
) -> Tuple[Path, ...]:
    """
    Resolve Liberty input conservatively.

    If --liberty is supplied, use exactly those files.

    Otherwise auto-discover only when the platform's lib directory contains
    exactly one .lib file.  Multiple .lib files may represent multiple corners,
    so guessing among them would make stage timing non-reproducible.
    """

    if explicit:
        paths = tuple(
            require_file(
                Path(value),
                label="Liberty file",
            )
            for value in explicit
        )

        if len(set(paths)) != len(paths):
            raise StageReportError(
                "duplicate --liberty paths supplied"
            )

        return paths

    lib_dir = (
        flow_dir
        / "platforms"
        / platform
        / "lib"
    ).resolve()

    if not lib_dir.is_dir():
        raise StageReportError(
            f"platform Liberty directory not found: {lib_dir}"
        )

    matches = tuple(
        sorted(
            path.resolve()
            for path in lib_dir.glob("*.lib")
            if path.is_file()
        )
    )

    if not matches:
        raise StageReportError(
            f"no .lib files found under {lib_dir}"
        )

    if len(matches) != 1:
        raise StageReportError(
            "multiple Liberty files found; refusing to guess timing corner. "
            "Pass the exact file(s) with --liberty:\n"
            + "\n".join(
                f"  - {path}"
                for path in matches
            )
        )

    return matches


# =============================================================================
# Plan construction
# =============================================================================

def build_plan(
    *,
    variant: str,
    design: str,
    platform: str,
    stage: str,
    flow_dir: Path,
    liberty: Sequence[str] = (),
    report_checks_tcl: Optional[Path] = None,
    report_subdir: Optional[str] = None,
    env_overrides: Sequence[Tuple[str, str]] = (),
) -> StageReportPlan:
    flow_dir = Path(
        flow_dir
    ).expanduser().resolve()

    if not (flow_dir / "Makefile").is_file():
        raise StageReportError(
            f"ORFS Makefile not found: {flow_dir / 'Makefile'}"
        )

    variant = require_nonempty(
        variant,
        label="variant",
    )
    design = require_nonempty(
        design,
        label="design",
    )
    platform = require_nonempty(
        platform,
        label="platform",
    )

    if stage not in SUPPORTED_STAGES:
        raise StageReportError(
            f"unsupported stage {stage!r}; "
            f"supported: {', '.join(SUPPORTED_STAGES)}"
        )

    result_dir = (
        flow_dir
        / "results"
        / platform
        / design
        / variant
    ).resolve()

    handoff_pair = None
    for odb_name, sdc_name in STAGE_HANDOFFS[stage]:
        if (result_dir / odb_name).is_file() and (result_dir / sdc_name).is_file():
            handoff_pair = (odb_name, sdc_name)
            break
    if handoff_pair is None:
        expected = ", ".join(f"{o} + {s}" for o, s in STAGE_HANDOFFS[stage])
        raise StageReportError(
            f"no complete {stage} ODB/SDC handoff found under {result_dir}; expected one of: {expected}"
        )
    odb_name, sdc_name = handoff_pair

    result_dir = (
        flow_dir
        / "results"
        / platform
        / design
        / variant
    ).resolve()

    odb = require_file(
        result_dir / odb_name,
        label=f"{stage} ODB",
    )
    sdc = require_file(
        result_dir / sdc_name,
        label=f"{stage} SDC",
    )

    setrc_tcl = require_file(
        flow_dir
        / "platforms"
        / platform
        / "setRC.tcl",
        label="platform setRC.tcl",
    )

    if report_checks_tcl is None:
        report_checks_tcl = (
            flow_dir
            / "timing_automation"
            / "report_checks.tcl"
        )

    report_checks_tcl = require_file(
        report_checks_tcl,
        label="report_checks.tcl",
    )

    liberty_files = discover_liberty_files(
        flow_dir=flow_dir,
        platform=platform,
        explicit=liberty,
    )

    report_dir = (
        flow_dir
        / "reports"
        / platform
        / design
        / variant
        / "stages"
        / stage
    ).resolve()
    if report_subdir:
        safe_subdir = str(report_subdir).strip().replace("/", "_").replace("\\", "_")
        if not safe_subdir or safe_subdir in {".", ".."}:
            raise StageReportError("invalid report_subdir")
        report_dir = report_dir / safe_subdir
    else:
        safe_subdir = None

    suffix = f"_{safe_subdir}" if safe_subdir else ""
    tcl_script = (
        flow_dir
        / "ai_runs"
        / f"{variant}_{stage}{suffix}_stage_reports.tcl"
    ).resolve()

    metadata_path = (
        flow_dir
        / "ai_runs"
        / f"{variant}_{stage}{suffix}_stage_reports.json"
    ).resolve()

    return StageReportPlan(
        flow_dir=flow_dir,
        design=design,
        platform=platform,
        variant=variant,
        stage=stage,
        odb=odb,
        sdc=sdc,
        liberty_files=liberty_files,
        setrc_tcl=setrc_tcl,
        report_checks_tcl=(
            report_checks_tcl
        ),
        report_dir=report_dir,
        tcl_script=tcl_script,
        metadata_path=metadata_path,
        env_overrides=tuple((str(k), str(v)) for k, v in env_overrides),
        report_subdir=safe_subdir,
    )


# =============================================================================
# Tcl construction
# =============================================================================

def build_stage_tcl(
    plan: StageReportPlan,
) -> str:
    """
    Reconstruct the timing context for the frozen global-route handoff.

    The order follows the normal ORFS stage-loader pattern:
      read_liberty
      read_db
      read_sdc
      source setRC.tcl

    Then global-route parasitics are explicitly re-estimated before running the
    reporting script.
    """

    lines: List[str] = []

    lines.append(
        "# Auto-generated by nEBULA run_stage_reports.py"
    )
    lines.append(
        f"# design={plan.design}"
    )
    lines.append(
        f"# platform={plan.platform}"
    )
    lines.append(
        f"# variant={plan.variant}"
    )
    lines.append(
        f"# stage={plan.stage}"
    )
    lines.append("")

    lines.append(
        "set ::env(REPORTS_DIR) "
        + tcl_quote(
            plan.report_dir
        )
    )

    # report_checks.tcl itself mkdirs REPORTS_DIR, but doing this before source
    # makes the stage runner independently explicit.
    lines.append(
        "file mkdir $::env(REPORTS_DIR)"
    )
    for key, value in plan.env_overrides:
        if not key.replace("_", "").isalnum():
            raise StageReportError(f"invalid environment key for Tcl: {key!r}")
        lines.append(f"set ::env({key}) " + tcl_quote(value))
    lines.append("")

    for liberty in plan.liberty_files:
        lines.append(
            "read_liberty "
            + tcl_quote(
                liberty
            )
        )

    lines.append(
        "read_db "
        + tcl_quote(
            plan.odb
        )
    )

    lines.append(
        "read_sdc "
        + tcl_quote(
            plan.sdc
        )
    )

    lines.append(
        "source "
        + tcl_quote(
            plan.setrc_tcl
        )
    )

    lines.append("")

    if plan.stage in {"floorplan", "place", "cts", "global_route"}:
        # Pre-route stages use placement/global-routing estimates.
        lines.append("estimate_parasitics -global_routing")
    elif plan.stage in {"detailed_route", "final"}:
        # Routed ODB contains routed parasitics in modern ORFS; if extraction
        # is not already materialized, OpenROAD will fail explicitly later.
        lines.append("# detailed_route: use routed parasitic state from ODB")
    else:
        raise StageReportError(f"no parasitic reconstruction rule for stage {plan.stage!r}")

    lines.append("")

    lines.append(
        "puts \"\""
    )
    lines.append(
        "puts \"============================================================\""
    )
    lines.append(
        "puts \"nEBULA stage-consistent timing reports\""
    )
    lines.append(
        f"puts \"Stage   : {plan.stage}\""
    )
    lines.append(
        f"puts \"Variant : {plan.variant}\""
    )
    lines.append(
        "puts \"Reports : $::env(REPORTS_DIR)\""
    )
    lines.append(
        "puts \"============================================================\""
    )
    lines.append("")

    lines.append(
        "source "
        + tcl_quote(
            plan.report_checks_tcl
        )
    )

    lines.append("")
    lines.append(
        "puts \"nEBULA stage reporting completed successfully.\""
    )

    return "\n".join(
        lines
    ) + "\n"


# =============================================================================
# Report validation / metadata
# =============================================================================

def report_inventory(
    report_dir: Path,
    *,
    report_script: Optional[Path] = None,
) -> Dict[str, Any]:
    report_dir = Path(
        report_dir
    )

    present = sorted(
        path.name
        for path in report_dir.iterdir()
        if path.is_file()
    ) if report_dir.is_dir() else []

    required = list(REQUIRED_REPORTS)
    if report_script is not None and Path(report_script).name == "report_checks.tcl":
        if not any(name.endswith("_critical_paths.rpt") and "_reg2reg_" not in name for name in present):
            required = ["<group>_critical_paths.rpt"]
    elif report_script is not None and Path(report_script).name == "report_witness.tcl":
        if not any(name.startswith("witness_") and name.endswith(".json") for name in present):
            required = ["witness_<id>.json"]
    missing_required = list(required)

    optional_present = [
        name
        for name in OPTIONAL_REPORTS
        if name in present
    ]

    return {
        "present": present,
        "required": required,
        "missing_required": (
            missing_required
        ),
        "optional_present": (
            optional_present
        ),
        "ok": not missing_required,
    }


def source_identity(
    plan: StageReportPlan,
) -> Dict[str, Any]:
    return {
        "stage": plan.stage,
        "odb": {
            "path": str(plan.odb),
            "sha256": sha256_file(
                plan.odb
            ),
        },
        "sdc": {
            "path": str(plan.sdc),
            "sha256": sha256_file(
                plan.sdc
            ),
        },
        "liberty_files": [
            {
                "path": str(path),
                "sha256": sha256_file(
                    path
                ),
            }
            for path in plan.liberty_files
        ],
        "setrc_tcl": {
            "path": str(
                plan.setrc_tcl
            ),
            "sha256": sha256_file(
                plan.setrc_tcl
            ),
        },
        "report_checks_tcl": {
            "path": str(
                plan.report_checks_tcl
            ),
            "sha256": sha256_file(
                plan.report_checks_tcl
            ),
        },
    }


# =============================================================================
# Runner
# =============================================================================

def run_stage_reports(
    *,
    variant: str,
    design: str,
    platform: str,
    stage: str,
    flow_dir: Path = DEFAULT_FLOW_DIR,
    openroad: Optional[str] = None,
    liberty: Sequence[str] = (),
    report_checks_tcl: Optional[Path] = None,
    report_subdir: Optional[str] = None,
    env_overrides: Sequence[Tuple[str, str]] = (),
    dry_run: bool = False,
    overwrite: bool = False,
) -> Dict[str, Any]:
    plan = build_plan(
        variant=variant,
        design=design,
        platform=platform,
        stage=stage,
        flow_dir=flow_dir,
        liberty=liberty,
        report_checks_tcl=(
            report_checks_tcl
        ),
        report_subdir=report_subdir,
        env_overrides=env_overrides,
    )

    plan.report_dir.parent.mkdir(
        parents=True,
        exist_ok=True,
    )
    plan.tcl_script.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    if plan.report_dir.exists():
        existing_files = [
            path
            for path in plan.report_dir.iterdir()
            if path.is_file()
        ]

        if existing_files and not overwrite:
            raise StageReportError(
                "stage report directory already contains files: "
                f"{plan.report_dir}. "
                "Use --overwrite to regenerate."
            )

        if overwrite:
            shutil.rmtree(
                plan.report_dir
            )

    plan.report_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    script = build_stage_tcl(
        plan
    )

    plan.tcl_script.write_text(
        script,
        encoding="utf-8",
    )

    openroad_path: Optional[Path]

    if dry_run:
        # Dry-run deliberately does not require a local executable.  If an
        # explicit path is supplied and valid, show it; otherwise use a
        # descriptive placeholder in metadata.
        if openroad:
            candidate = Path(
                openroad
            ).expanduser().resolve()
            openroad_path = (
                candidate
                if candidate.is_file()
                else None
            )
        else:
            openroad_path = None
    else:
        openroad_path = resolve_openroad(
            flow_dir=plan.flow_dir,
            explicit=openroad,
        )

    command = [
        (
            str(openroad_path)
            if openroad_path is not None
            else "<openroad>"
        ),
        "-exit",
        str(
            plan.tcl_script
        ),
    ]

    metadata: Dict[str, Any] = {
        "schema": SCHEMA,
        "ok": False,
        "dry_run": bool(
            dry_run
        ),
        "design": {
            "design_id": plan.design,
            "platform": plan.platform,
        },
        "flow_variant": plan.variant,
        "evaluation": {
            "stage": plan.stage,
        },
        "source": source_identity(
            plan
        ),
        "report_dir": str(
            plan.report_dir
        ),
        "tcl_script": str(
            plan.tcl_script
        ),
        "openroad": (
            str(openroad_path)
            if openroad_path is not None
            else None
        ),
        "command": command,
        "command_shell_display": (
            shlex.join(
                command
            )
        ),
        "returncode": None,
        "reports": None,
    }

    print()
    print("=" * 64)
    print("nEBULA stage-consistent timing reports")
    print("=" * 64)
    print(f"Design       : {plan.design}")
    print(f"Platform     : {plan.platform}")
    print(f"Flow Variant : {plan.variant}")
    print(f"Stage        : {plan.stage}")
    print(f"ODB          : {plan.odb}")
    print(f"SDC          : {plan.sdc}")
    print(f"Reports      : {plan.report_dir}")
    print("=" * 64)
    print()
    print("OpenROAD command:")
    print(
        shlex.join(
            command
        )
    )
    print()

    if dry_run:
        metadata["ok"] = True
        metadata["reports"] = {
            "status": "not_run",
        }

        write_json(
            plan.metadata_path,
            metadata,
        )

        print(
            "DRY RUN: OpenROAD was not executed."
        )
        print(
            f"Tcl script : {plan.tcl_script}"
        )
        print(
            f"Metadata   : {plan.metadata_path}"
        )

        return metadata

    assert openroad_path is not None

    proc = subprocess.run(
        [
            str(openroad_path),
            "-exit",
            str(plan.tcl_script),
        ],
        cwd=str(
            plan.flow_dir
        ),
        text=True,
    )

    metadata["returncode"] = (
        proc.returncode
    )

    inventory = report_inventory(
        plan.report_dir,
        report_script=plan.report_checks_tcl,
    )
    metadata["reports"] = (
        inventory
    )

    metadata["ok"] = bool(
        proc.returncode == 0
        and inventory["ok"]
    )

    write_json(
        plan.metadata_path,
        metadata,
    )

    if proc.returncode != 0:
        raise StageReportError(
            "OpenROAD stage-report run failed with "
            f"return code {proc.returncode}. "
            f"Metadata: {plan.metadata_path}"
        )

    if not inventory["ok"]:
        raise StageReportError(
            "OpenROAD returned success but required timing reports "
            "are missing: "
            + ", ".join(
                inventory[
                    "missing_required"
                ]
            )
        )

    print()
    print("=" * 64)
    print("Stage reports generated")
    print("=" * 64)
    print(
        f"Stage        : {plan.stage}"
    )
    print(
        f"Reports      : {plan.report_dir}"
    )
    print(
        "Files        : "
        + ", ".join(
            inventory["present"]
        )
    )
    print(
        f"Metadata     : {plan.metadata_path}"
    )
    print("=" * 64)

    return metadata


# =============================================================================
# Self-test
# =============================================================================

def run_self_test() -> None:
    with tempfile.TemporaryDirectory(
        prefix="nebula_stage_reports_"
    ) as tmp:
        root = Path(tmp).resolve()
        flow = root / "flow"

        flow.mkdir(
            parents=True,
            exist_ok=True,
        )

        (flow / "Makefile").write_text(
            "# self-test\n",
            encoding="utf-8",
        )

        platform = "nangate45"
        design = "demo"
        variant = "run_demo"
        stage = "global_route"

        platform_dir = (
            flow
            / "platforms"
            / platform
        )

        lib_dir = (
            platform_dir
            / "lib"
        )

        lib_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        liberty = (
            lib_dir
            / "NangateOpenCellLibrary_typical.lib"
        )

        liberty.write_text(
            "library(test) {}\n",
            encoding="utf-8",
        )

        setrc = (
            platform_dir
            / "setRC.tcl"
        )

        setrc.write_text(
            "# self-test RC\n",
            encoding="utf-8",
        )

        timing_dir = (
            flow
            / "timing_automation"
        )

        timing_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        report_checks = (
            timing_dir
            / "report_checks.tcl"
        )

        report_checks.write_text(
            "set report_dir $::env(REPORTS_DIR)\n"
            "file mkdir $report_dir\n"
            "set f [open [file join $report_dir critical_paths.rpt] w]\n"
            "puts $f \"self-test\"\n"
            "close $f\n",
            encoding="utf-8",
        )

        result_dir = (
            flow
            / "results"
            / platform
            / design
            / variant
        )

        result_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        odb = (
            result_dir
            / "5_1_grt.odb"
        )
        sdc = (
            result_dir
            / "5_1_grt.sdc"
        )

        odb.write_bytes(
            b"fake-odb"
        )
        sdc.write_text(
            "create_clock -period 1.0 [get_ports clk]\n",
            encoding="utf-8",
        )

        plan = build_plan(
            variant=variant,
            design=design,
            platform=platform,
            stage=stage,
            flow_dir=flow,
        )

        assert (
            plan.odb
            == odb.resolve()
        )
        assert (
            plan.sdc
            == sdc.resolve()
        )
        assert (
            plan.liberty_files
            == (
                liberty.resolve(),
            )
        )

        script = build_stage_tcl(
            plan
        )

        expected_report_dir = (
            flow
            / "reports"
            / platform
            / design
            / variant
            / "stages"
            / "global_route"
        ).resolve()

        assert (
            plan.report_dir
            == expected_report_dir
        )

        assert (
            "set ::env(REPORTS_DIR)"
            in script
        )
        assert (
            str(
                expected_report_dir
            )
            in script
        )
        assert (
            "read_liberty"
            in script
        )
        assert (
            str(
                liberty.resolve()
            )
            in script
        )
        assert (
            "read_db"
            in script
        )
        assert (
            str(
                odb.resolve()
            )
            in script
        )
        assert (
            "read_sdc"
            in script
        )
        assert (
            str(
                sdc.resolve()
            )
            in script
        )
        assert (
            "source "
            + tcl_quote(
                setrc.resolve()
            )
            in script
        )
        assert (
            "estimate_parasitics -global_routing"
            in script
        )
        assert (
            "source "
            + tcl_quote(
                report_checks.resolve()
            )
            in script
        )

        # Generic alternate Tcl scripts (notably report_witness.tcl) must be
        # isolated in their own report subdirectory with explicit env data.
        report_witness = timing_dir / "report_witness.tcl"
        report_witness.write_text("# witness self-test\n", encoding="utf-8")
        witness_plan = build_plan(
            variant=variant, design=design, platform=platform, stage=stage, flow_dir=flow,
            report_checks_tcl=report_witness, report_subdir="witness_clk",
            env_overrides=(("NEBULA_WITNESS_REQUEST", "/tmp/request.json"),),
        )
        witness_script = build_stage_tcl(witness_plan)
        assert witness_plan.report_dir.name == "witness_clk"
        assert "NEBULA_WITNESS_REQUEST" in witness_script
        assert "/tmp/request.json" in witness_script
        witness_plan.report_dir.mkdir(parents=True, exist_ok=True)
        (witness_plan.report_dir / "witness_w000.json").write_text("{}\n", encoding="utf-8")
        assert report_inventory(witness_plan.report_dir, report_script=report_witness)["ok"] is True

        metadata = run_stage_reports(
            variant=variant,
            design=design,
            platform=platform,
            stage=stage,
            flow_dir=flow,
            dry_run=True,
        )

        assert metadata["ok"] is True
        assert (
            metadata[
                "evaluation"
            ]["stage"]
            == "global_route"
        )
        assert (
            metadata[
                "report_dir"
            ]
            == str(
                expected_report_dir
            )
        )
        assert (
            metadata[
                "source"
            ]["odb"]["sha256"]
            == sha256_file(
                odb
            )
        )

        # Must fail instead of guessing if multiple Liberty corners exist.
        second_lib = (
            lib_dir
            / "second_corner.lib"
        )
        second_lib.write_text(
            "library(test2) {}\n",
            encoding="utf-8",
        )

        try:
            discover_liberty_files(
                flow_dir=flow,
                platform=platform,
                explicit=(),
            )
        except StageReportError as exc:
            assert (
                "multiple Liberty files"
                in str(exc)
            )
        else:
            raise AssertionError(
                "multiple Liberty files were guessed silently"
            )

        # Explicit corner selection must then work.
        explicit = discover_liberty_files(
            flow_dir=flow,
            platform=platform,
            explicit=(
                str(liberty),
            ),
        )

        assert explicit == (
            liberty.resolve(),
        )

        # Unknown stages must fail closed.
        try:
            build_plan(
                variant=variant,
                design=design,
                platform=platform,
                stage="bogus",
                flow_dir=flow,
                liberty=(
                    str(liberty),
                ),
            )
        except StageReportError as exc:
            assert (
                "unsupported stage"
                in str(exc)
            )
        else:
            raise AssertionError(
                "unsupported stage was accepted"
            )

    print(
        "PASS: run_stage_reports self-test"
    )
    print(
        "PASS: global_route maps exactly to 5_1_grt.odb / 5_1_grt.sdc"
    )
    print(
        "PASS: REPORTS_DIR redirected to stages/global_route"
    )
    print(
        "PASS: original final-stage report directory is not targeted"
    )
    print(
        "PASS: Liberty + ODB + SDC + setRC ordering represented"
    )
    print(
        "PASS: global-route parasitics explicitly re-estimated"
    )
    print(
        "PASS: existing report_checks.tcl reused unchanged"
    )
    print(
        "PASS: source-stage SHA identities recorded"
    )
    print(
        "PASS: multiple Liberty corners rejected unless explicit"
    )
    print(
        "PASS: unsupported physical stages fail closed"
    )


# =============================================================================
# CLI
# =============================================================================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate stage-consistent nEBULA timing reports "
            "from an existing ORFS physical-stage handoff."
        )
    )

    parser.add_argument(
        "--variant",
        help="Existing ORFS FLOW_VARIANT.",
    )

    parser.add_argument(
        "--design",
        help="ORFS DESIGN_NAME / nEBULA design_id.",
    )

    parser.add_argument(
        "--platform",
        default="nangate45",
        help="ORFS platform.",
    )

    parser.add_argument(
        "--stage",
        choices=SUPPORTED_STAGES,
        default="global_route",
        help="Physical stage whose exact handoff should be reported.",
    )

    parser.add_argument(
        "--flow-dir",
        default=str(
            DEFAULT_FLOW_DIR
        ),
        help="ORFS flow directory containing Makefile.",
    )

    parser.add_argument(
        "--openroad",
        default=None,
        help="Optional explicit OpenROAD executable.",
    )

    parser.add_argument(
        "--liberty",
        action="append",
        default=[],
        help=(
            "Exact Liberty file to load. May be repeated. "
            "If omitted, auto-discovery is allowed only when the platform "
            "contains exactly one .lib file."
        ),
    )

    parser.add_argument(
        "--report-checks-tcl",
        default=None,
        help=(
            "Optional report_checks.tcl path. "
            "Defaults to timing_automation/report_checks.tcl."
        ),
    )

    parser.add_argument(
        "--tcl-script",
        default=None,
        help="Generic reporting Tcl script; alias/superset of --report-checks-tcl.",
    )

    parser.add_argument(
        "--report-subdir",
        default=None,
        help="Optional subdirectory below stages/<stage>, e.g. witness.",
    )

    parser.add_argument(
        "--env",
        action="append",
        default=[],
        help="Environment override passed into OpenROAD Tcl as KEY=VALUE; may repeat.",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Write/print the Tcl plan without invoking OpenROAD.",
    )

    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Regenerate an existing stage report directory.",
    )

    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run standalone regression tests without OpenROAD.",
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

    if not args.variant:
        missing.append(
            "--variant"
        )

    if not args.design:
        missing.append(
            "--design"
        )

    if missing:
        print(
            "ERROR: missing required argument(s): "
            + ", ".join(
                missing
            ),
            file=sys.stderr,
        )
        return 2

    try:
        env_overrides = []
        for raw in args.env:
            if "=" not in raw:
                raise StageReportError("--env must use KEY=VALUE")
            key, value = raw.split("=", 1)
            env_overrides.append((key, value))
        selected_tcl = args.tcl_script or args.report_checks_tcl

        result = run_stage_reports(
            variant=args.variant,
            design=args.design,
            platform=args.platform,
            stage=args.stage,
            flow_dir=Path(
                args.flow_dir
            ),
            openroad=args.openroad,
            liberty=args.liberty,
            report_checks_tcl=(Path(selected_tcl) if selected_tcl else None),
            report_subdir=args.report_subdir,
            env_overrides=env_overrides,
            dry_run=args.dry_run,
            overwrite=args.overwrite,
        )
    except StageReportError as exc:
        print(
            f"ERROR: {exc}",
            file=sys.stderr,
        )
        return 2
    except Exception as exc:
        print(
            f"ERROR: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 2

    return (
        0
        if result.get("ok") is True
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(
        main()
    )
