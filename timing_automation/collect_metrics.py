#!/usr/bin/env python3
"""
collect_metrics.py

Collect canonical, stage-aware PPA metrics for one ORFS FLOW_VARIANT.

Canonical metric contract
-------------------------
Every comparable baseline/candidate metric artifact emits:

    {
        "wns_ns": <float>,
        "tns_ns": <float>,
        "area_um2": <float>,
        "power_mw": <float>
    }

All values are normalized to six decimal places.

Stage-aware safety rule
-----------------------
When a physical_recipe/1 is supplied, canonical PPA values are read ONLY from
the ORFS JSON file belonging to that recipe stage:

    synth        -> 1_synth.json -> synth__...
    global_route -> 5_1_grt.json -> globalroute__...
    final        -> 6_report.json -> finish__...

The collector never borrows canonical PPA values from a later stage.

This matters because a full ORFS run contains metrics from many stages.
Stamping finish metrics as "global_route" would make baseline/candidate
comparisons invalid even if the recipe fingerprint matched.

Current ORFS limitation
-----------------------
On the tested ORFS revision, 1_synth.json exposes synthesis area but does not
emit synthesis-stage WNS/TNS/power. Therefore a synth recipe will currently
produce flow_complete=false unless those synth metrics exist in that ORFS run.
The collector deliberately does NOT substitute floorplan/global-route/final
metrics.

Without --physical-recipe, legacy behavior is preserved: final finish-stage
metrics are collected and evaluation.stage remains null.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

try:
    from physical_recipe import PhysicalRecipe
except ImportError:
    from timing_automation.physical_recipe import PhysicalRecipe


# =============================================================================
# Paths / schemas
# =============================================================================

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_FLOW_DIR = SCRIPT_DIR.parent

OUTPUT_SCHEMA = "ppa_metrics/2"
SCHEMA_VERSION = "2.2"
METRIC_SCHEMA_VERSION = "1.0"
METRIC_DECIMALS = 6

STAGE_SYNTH = "synth"
STAGE_GLOBAL_ROUTE = "global_route"
STAGE_FINAL = "final"

VALID_STAGES = (
    STAGE_SYNTH,
    STAGE_GLOBAL_ROUTE,
    STAGE_FINAL,
)

CANONICAL_METRIC_KEYS = (
    "wns_ns",
    "tns_ns",
    "area_um2",
    "power_mw",
)

NUMBER_RE = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"


# =============================================================================
# Stage contract
# =============================================================================

@dataclass(frozen=True)
class StageMetricSpec:
    stage: str
    json_filename: str
    prefix: str
    wns_key: str
    tns_key: str
    area_key: str
    power_key: str


def _stage_spec(
    stage: str,
    json_filename: str,
    prefix: str,
) -> StageMetricSpec:
    return StageMetricSpec(
        stage=stage,
        json_filename=json_filename,
        prefix=prefix,
        wns_key=f"{prefix}__timing__setup__ws",
        tns_key=f"{prefix}__timing__setup__tns",
        area_key=f"{prefix}__design__instance__area",
        power_key=f"{prefix}__power__total",
    )


STAGE_METRIC_SPECS: Dict[str, StageMetricSpec] = {
    STAGE_SYNTH: _stage_spec(
        STAGE_SYNTH,
        "1_synth.json",
        "synth",
    ),
    STAGE_GLOBAL_ROUTE: _stage_spec(
        STAGE_GLOBAL_ROUTE,
        "5_1_grt.json",
        "globalroute",
    ),
    STAGE_FINAL: _stage_spec(
        STAGE_FINAL,
        "6_report.json",
        "finish",
    ),
}


# =============================================================================
# Numeric helpers
# =============================================================================

def as_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None

    if isinstance(value, str):
        match = re.search(NUMBER_RE, value)
        if match:
            try:
                number = float(match.group(0))
            except ValueError:
                return None
            return number if math.isfinite(number) else None

    return None


def normalize_metric(value: Any) -> Optional[float]:
    number = as_float(value)
    if number is None:
        return None

    number = round(number, METRIC_DECIMALS)

    if number == 0:
        return 0.0

    return number


def normalize_diagnostic(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value

    if isinstance(value, int):
        return value

    number = as_float(value)
    if number is not None:
        return normalize_metric(number)

    return value


def canonical_metric_object(
    *,
    wns_ns: Any,
    tns_ns: Any,
    area_um2: Any,
    power_mw: Any,
) -> Dict[str, Optional[float]]:
    return {
        "wns_ns": normalize_metric(wns_ns),
        "tns_ns": normalize_metric(tns_ns),
        "area_um2": normalize_metric(area_um2),
        "power_mw": normalize_metric(power_mw),
    }


# =============================================================================
# ORFS JSON loading
# =============================================================================

def load_json_object(path: Path) -> Dict[str, Any]:
    path = Path(path)

    if not path.is_file():
        raise FileNotFoundError(f"ORFS metric JSON not found:\n{path}")

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"invalid ORFS metric JSON {path}: {exc}"
        ) from exc

    if not isinstance(value, dict):
        raise ValueError(
            f"ORFS metric JSON root must be an object: {path}"
        )

    return value


def load_all_orfs_metrics(
    log_dir: Path,
) -> Tuple[Dict[str, Any], Dict[str, str], List[Dict[str, str]]]:
    """
    Load all stage JSONs for non-canonical diagnostics only.

    Canonical PPA extraction never uses this merged object.
    """
    if not log_dir.exists():
        raise FileNotFoundError(
            f"ORFS log directory does not exist:\n{log_dir}"
        )

    json_files = sorted(log_dir.glob("*.json"))

    if not json_files:
        raise FileNotFoundError(
            f"No ORFS JSON metric files found in:\n{log_dir}"
        )

    all_metrics: Dict[str, Any] = {}
    metric_sources: Dict[str, str] = {}
    malformed_files: List[Dict[str, str]] = []

    for json_file in json_files:
        try:
            data = json.loads(json_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            malformed_files.append(
                {
                    "file": str(json_file),
                    "error": str(exc),
                }
            )
            continue

        if not isinstance(data, dict):
            continue

        for key, value in data.items():
            all_metrics[key] = value
            metric_sources[key] = str(json_file)

    return all_metrics, metric_sources, malformed_files


def load_stage_metrics(
    log_dir: Path,
    stage: str,
) -> Tuple[StageMetricSpec, Path, Dict[str, Any]]:
    if stage not in STAGE_METRIC_SPECS:
        raise ValueError(
            f"unsupported evaluation stage {stage!r}; "
            f"expected one of {list(VALID_STAGES)}"
        )

    spec = STAGE_METRIC_SPECS[stage]
    stage_file = log_dir / spec.json_filename
    payload = load_json_object(stage_file)

    return spec, stage_file, payload


# =============================================================================
# Final-stage power fallback
# =============================================================================

def extract_report_power_w(text: str) -> Optional[float]:
    pattern = re.compile(
        rf"^\s*Total\s+"
        rf"{NUMBER_RE}\s+"
        rf"{NUMBER_RE}\s+"
        rf"{NUMBER_RE}\s+"
        rf"(?P<total>{NUMBER_RE})\s+"
        rf"100(?:\.0+)?%\s*$",
        flags=re.IGNORECASE | re.MULTILINE,
    )

    values = [
        float(match.group("total"))
        for match in pattern.finditer(text)
    ]

    return max(values) if values else None


def collect_final_power_w_from_report(
    report_dir: Path,
) -> Tuple[Optional[float], Optional[str]]:
    """
    Text fallback is allowed only for final-stage collection.

    It is unsafe to use a finish report as a fallback for synth/global_route.
    """
    candidates: List[Path] = []

    for preferred_name in (
        "6_finish.rpt",
        "6_report.rpt",
    ):
        preferred = report_dir / preferred_name
        if preferred.exists():
            candidates.append(preferred)

    if report_dir.exists():
        for path in sorted(report_dir.glob("*finish*.rpt")):
            if path not in candidates:
                candidates.append(path)

    for path in candidates:
        try:
            text = path.read_text(
                encoding="utf-8",
                errors="replace",
            )
        except OSError:
            continue

        power_w = extract_report_power_w(text)
        if power_w is not None:
            return power_w, str(path)

    return None, None


# =============================================================================
# Clock extraction
# =============================================================================

def extract_clocks(
    flow_dir: Path,
    platform: str,
    design: str,
) -> List[Dict[str, Any]]:
    design_dir = flow_dir / "designs" / platform / design

    if not design_dir.exists():
        return []

    clocks: List[Dict[str, Any]] = []

    for sdc_file in sorted(design_dir.rglob("*.sdc")):
        try:
            text = sdc_file.read_text(
                encoding="utf-8",
                errors="replace",
            )
        except OSError:
            continue

        text = text.replace("\\\n", " ")

        commands = re.findall(
            r"create_clock[^;\n]*",
            text,
            flags=re.IGNORECASE,
        )

        for command in commands:
            period_match = re.search(
                r"-period\s+([0-9]*\.?[0-9]+)",
                command,
                flags=re.IGNORECASE,
            )

            if not period_match:
                continue

            period = normalize_metric(
                float(period_match.group(1))
            )

            name_match = re.search(
                r"-name\s+([^\s\]]+)",
                command,
                flags=re.IGNORECASE,
            )

            clocks.append(
                {
                    "name": (
                        name_match.group(1)
                        if name_match
                        else "unnamed"
                    ),
                    "period_ns": period,
                    "source": str(sdc_file),
                }
            )

    return clocks


# =============================================================================
# Recipe checks
# =============================================================================

def validate_physical_recipe_for_collection(
    *,
    physical_recipe: PhysicalRecipe,
    design: str,
    platform: str,
) -> Dict[str, Any]:
    physical_recipe.validate()

    if physical_recipe.design_id != design:
        raise ValueError(
            "physical recipe design mismatch: "
            f"recipe={physical_recipe.design_id!r}, "
            f"collector={design!r}"
        )

    if physical_recipe.platform != platform:
        raise ValueError(
            "physical recipe platform mismatch: "
            f"recipe={physical_recipe.platform!r}, "
            f"collector={platform!r}"
        )

    if physical_recipe.stage not in VALID_STAGES:
        raise ValueError(
            f"unsupported physical recipe stage "
            f"{physical_recipe.stage!r}"
        )

    verification = physical_recipe.verify_frozen_files()

    if verification.get("ok") is not True:
        bad = [
            item
            for item in verification.get("files", [])
            if item.get("matches") is not True
        ]

        details = "; ".join(
            f"{item.get('role')}:{item.get('path')}"
            for item in bad
        ) or "unknown frozen-file mismatch"

        raise ValueError(
            "physical recipe verification failed; frozen "
            f"experiment inputs changed: {details}"
        )

    return verification


# =============================================================================
# Diagnostics
# =============================================================================

def collect_stage_diagnostics(
    *,
    stage: str,
    stage_payload: Mapping[str, Any],
    all_metrics: Mapping[str, Any],
    all_sources: Mapping[str, str],
) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """
    Keep diagnostics separate from the canonical PPA schema.

    Future-stage diagnostics are not exposed for earlier evaluation stages.
    """
    spec = STAGE_METRIC_SPECS[stage]

    diagnostics: Dict[str, Any] = {}
    sources: Dict[str, str] = {}

    stage_keys = {
        "setup_violations": (
            f"{spec.prefix}__timing__drv__setup_violation_count"
        ),
        "hold_violations": (
            f"{spec.prefix}__timing__drv__hold_violation_count"
        ),
        "wns_percent_delay": (
            f"{spec.prefix}__timing__wns_percent_delay"
        ),
    }

    for friendly, key in stage_keys.items():
        value = stage_payload.get(key)
        diagnostics[friendly] = normalize_diagnostic(value)

    synth_key_candidates = (
        "synth__design__instance__area",
        "synth__design__instance__area__stdcell",
    )

    synth_area = None
    synth_source = None

    for key in synth_key_candidates:
        if key in all_metrics:
            synth_area = normalize_diagnostic(all_metrics.get(key))
            synth_source = all_sources.get(key)
            break

    diagnostics["synth_area_um2"] = synth_area
    if synth_source:
        sources["synth_area_um2"] = synth_source

    if stage == STAGE_FINAL:
        final_only = {
            "drc_errors": (
                "detailedroute__route__drc_errors"
            ),
            "wirelength_um": (
                "detailedroute__route__wirelength"
            ),
        }

        for friendly, key in final_only.items():
            value = all_metrics.get(key)
            diagnostics[friendly] = normalize_diagnostic(value)
            source = all_sources.get(key)
            if value is not None and source:
                sources[friendly] = source
    else:
        diagnostics["drc_errors"] = None
        diagnostics["wirelength_um"] = None

    return diagnostics, sources


# =============================================================================
# Main collection API
# =============================================================================

def collect_metrics(
    variant: str,
    design: str = "timing_demo",
    platform: str = "nangate45",
    flow_dir: Path = DEFAULT_FLOW_DIR,
    physical_recipe: Optional[PhysicalRecipe] = None,
) -> Dict[str, Any]:
    flow_dir = Path(flow_dir).resolve()

    recipe_verification: Optional[Dict[str, Any]] = None

    if physical_recipe is not None:
        recipe_verification = (
            validate_physical_recipe_for_collection(
                physical_recipe=physical_recipe,
                design=design,
                platform=platform,
            )
        )
        selected_stage = physical_recipe.stage

        evaluation: Dict[str, Any] = {
            "stage": selected_stage,
            "recipe_fingerprint_sha256": (
                physical_recipe.fingerprint_sha256.lower()
            ),
            "recipe_id": physical_recipe.recipe_id,
            "recipe_verified": True,
        }
    else:
        # Preserve legacy behavior: final metrics, no claimed evaluation
        # context/fingerprint.
        selected_stage = STAGE_FINAL
        evaluation = {
            "stage": None,
            "recipe_fingerprint_sha256": None,
            "recipe_id": None,
            "recipe_verified": False,
        }

    log_dir = (
        flow_dir
        / "logs"
        / platform
        / design
        / variant
    )

    report_dir = (
        flow_dir
        / "reports"
        / platform
        / design
        / variant
    )

    print("\n========================================")
    print("Collecting ORFS metrics")
    print("========================================")
    print(f"Design       : {design}")
    print(f"Platform     : {platform}")
    print(f"Flow Variant : {variant}")
    print(f"Metric Stage : {selected_stage}")

    if physical_recipe is not None:
        print(f"Eval Stage   : {selected_stage}")
        print(
            "Recipe SHA   : "
            f"{evaluation['recipe_fingerprint_sha256']}"
        )

    print(f"Log Dir      : {log_dir}")
    print(f"Report Dir   : {report_dir}")
    print("========================================\n")

    (
        all_metrics,
        all_metric_sources,
        malformed_files,
    ) = load_all_orfs_metrics(log_dir)

    spec, stage_file, stage_payload = load_stage_metrics(
        log_dir,
        selected_stage,
    )

    # ------------------------------------------------------------------
    # Canonical PPA metrics: ONLY from selected stage.
    # ------------------------------------------------------------------

    wns_raw = stage_payload.get(spec.wns_key)
    tns_raw = stage_payload.get(spec.tns_key)
    area_raw = stage_payload.get(spec.area_key)
    power_w_raw = stage_payload.get(spec.power_key)

    power_w = as_float(power_w_raw)
    power_source: Optional[str] = None
    power_source_kind: Optional[str] = None

    if power_w is not None:
        power_source = str(stage_file)
        power_source_kind = "orfs_stage_metric_json"

    elif selected_stage == STAGE_FINAL:
        power_w, power_source = (
            collect_final_power_w_from_report(
                report_dir
            )
        )

        if power_w is not None:
            power_source_kind = (
                "orfs_final_report_power_text"
            )

    power_mw = (
        power_w * 1000.0
        if power_w is not None
        else None
    )

    metrics = canonical_metric_object(
        wns_ns=wns_raw,
        tns_ns=tns_raw,
        area_um2=area_raw,
        power_mw=power_mw,
    )

    metric_sources_out: Dict[str, Optional[str]] = {
        "wns_ns": (
            str(stage_file)
            if wns_raw is not None
            else None
        ),
        "tns_ns": (
            str(stage_file)
            if tns_raw is not None
            else None
        ),
        "area_um2": (
            str(stage_file)
            if area_raw is not None
            else None
        ),
        "power_mw": power_source,
    }

    diagnostics, diagnostic_sources = (
        collect_stage_diagnostics(
            stage=selected_stage,
            stage_payload=stage_payload,
            all_metrics=all_metrics,
            all_sources=all_metric_sources,
        )
    )

    clocks = extract_clocks(
        flow_dir,
        platform,
        design,
    )

    missing_required = [
        key
        for key in CANONICAL_METRIC_KEYS
        if metrics.get(key) is None
    ]

    flow_complete = len(missing_required) == 0

    return {
        "schema": OUTPUT_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "metric_schema_version": (
            METRIC_SCHEMA_VERSION
        ),
        "metric_precision_decimals": (
            METRIC_DECIMALS
        ),
        "design": design,
        "platform": platform,
        "flow_variant": variant,
        "evaluation": evaluation,
        "flow_complete": flow_complete,
        "metrics": metrics,
        "metric_units": {
            "wns_ns": "ns",
            "tns_ns": "ns",
            "area_um2": "um^2",
            "power_mw": "mW",
        },
        "stage_source": {
            "stage": selected_stage,
            "json_file": str(stage_file),
            "prefix": spec.prefix,
            "orfs_keys": {
                "wns_ns": spec.wns_key,
                "tns_ns": spec.tns_key,
                "area_um2": spec.area_key,
                "power_w": spec.power_key,
            },
        },
        "diagnostics": diagnostics,
        "clocks": clocks,
        "sources": {
            "metrics": metric_sources_out,
            "diagnostics": diagnostic_sources,
            "power_source_kind": power_source_kind,
            "physical_recipe": (
                physical_recipe.recipe_id
                if physical_recipe is not None
                else None
            ),
        },
        "missing_required_metrics": (
            missing_required
        ),
        "malformed_metric_files": (
            malformed_files
        ),
        "recipe_file_verification": (
            recipe_verification
        ),
    }


# =============================================================================
# Save result
# =============================================================================

def save_metrics(
    result: Dict[str, Any],
    flow_dir: Path,
    output_file: Optional[Path] = None,
) -> Path:
    flow_dir = Path(flow_dir).resolve()

    if output_file is None:
        output_dir = flow_dir / "ai_runs"
        output_dir.mkdir(
            parents=True,
            exist_ok=True,
        )
        output_file = (
            output_dir
            / f"{result['flow_variant']}_metrics.json"
        )
    else:
        output_file = Path(output_file)
        output_file.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

    output_file.write_text(
        json.dumps(
            result,
            indent=2,
            sort_keys=False,
        )
        + "\n",
        encoding="utf-8",
    )

    return output_file


# =============================================================================
# Self-test helpers
# =============================================================================

def _selftest_write_stage_files(log_dir: Path) -> None:
    (log_dir / "1_synth.json").write_text(
        json.dumps(
            {
                # This intentionally mirrors the tested ORFS behavior:
                # synth area exists, synth WNS/TNS/power do not.
                "synth__design__instance__area": 1106.5600004,
            }
        ),
        encoding="utf-8",
    )

    (log_dir / "5_1_grt.json").write_text(
        json.dumps(
            {
                "globalroute__timing__setup__ws": 0.1805226,
                "globalroute__timing__setup__tns": 0,
                "globalroute__design__instance__area": 1228.9200004,
                "globalroute__power__total": 0.00220963,
                "globalroute__timing__drv__setup_violation_count": 0,
            }
        ),
        encoding="utf-8",
    )

    (log_dir / "6_report.json").write_text(
        json.dumps(
            {
                "finish__timing__setup__ws": 0.1960956,
                "finish__timing__setup__tns": 0,
                "finish__design__instance__area": 1228.9200004,
                "finish__power__total": 0.00213873,
                "finish__timing__drv__setup_violation_count": 0,
            }
        ),
        encoding="utf-8",
    )

    # Future-stage diagnostic used to prove that global_route collection does
    # not expose detailed-route diagnostics.
    (log_dir / "5_2_route.json").write_text(
        json.dumps(
            {
                "detailedroute__route__drc_errors": 0,
                "detailedroute__route__wirelength": 4858,
            }
        ),
        encoding="utf-8",
    )


def _selftest_recipe(
    *,
    flow_dir: Path,
    design: str,
    platform: str,
    stage: str,
) -> PhysicalRecipe:
    try:
        from physical_recipe import (
            build_physical_recipe,
        )
    except ImportError:
        from timing_automation.physical_recipe import (
            build_physical_recipe,
        )

    spec = {
        "schema": "physical_recipe_spec/1",
        "recipe_id": f"demo_{stage}_v1",
        "design": {
            "design_id": design,
            "top": design,
            "platform": platform,
        },
        "stage": stage,
        "files": [
            {
                "role": "design_config",
                "path": (
                    f"designs/{platform}/"
                    f"{design}/config.mk"
                ),
            },
            {
                "role": "constraints",
                "path": (
                    f"designs/{platform}/"
                    f"{design}/constraint.sdc"
                ),
            },
        ],
        "flow_variables": {
            "CORE_UTILIZATION": 40,
        },
        "stage_settings": {
            "stop_after": stage,
        },
        "toolchain": {
            "orfs_revision": "selftest",
            "openroad_revision": "selftest",
            "yosys_revision": "selftest",
        },
        "metadata": {},
    }

    return build_physical_recipe(
        spec,
        base_dir=flow_dir,
    )


# =============================================================================
# Self-test
# =============================================================================

def run_self_test() -> None:
    import tempfile

    with tempfile.TemporaryDirectory(
        prefix="nebula_collect_metrics_"
    ) as tmp:
        flow_dir = Path(tmp).resolve()
        design = "demo"
        platform = "nangate45"
        variant = "run_selftest"

        log_dir = (
            flow_dir
            / "logs"
            / platform
            / design
            / variant
        )

        report_dir = (
            flow_dir
            / "reports"
            / platform
            / design
            / variant
        )

        design_dir = (
            flow_dir
            / "designs"
            / platform
            / design
        )

        log_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        report_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        design_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        _selftest_write_stage_files(log_dir)

        config = design_dir / "config.mk"
        sdc = design_dir / "constraint.sdc"

        config.write_text(
            "export PLATFORM = nangate45\n"
            "export DESIGN_NAME = demo\n"
            "export CORE_UTILIZATION = 40\n",
            encoding="utf-8",
        )

        sdc.write_text(
            "create_clock -name clk -period 1.0 "
            "[get_ports clk]\n",
            encoding="utf-8",
        )

        # --------------------------------------------------------------
        # global_route must use 5_1_grt.json, not finish metrics.
        # --------------------------------------------------------------

        global_recipe = _selftest_recipe(
            flow_dir=flow_dir,
            design=design,
            platform=platform,
            stage=STAGE_GLOBAL_ROUTE,
        )

        global_result = collect_metrics(
            variant=variant,
            design=design,
            platform=platform,
            flow_dir=flow_dir,
            physical_recipe=global_recipe,
        )

        assert global_result["flow_complete"] is True
        assert global_result["metrics"] == {
            "wns_ns": 0.180523,
            "tns_ns": 0.0,
            "area_um2": 1228.92,
            "power_mw": 2.20963,
        }

        assert (
            global_result["stage_source"]["json_file"]
            == str(log_dir / "5_1_grt.json")
        )

        assert (
            global_result["stage_source"]["prefix"]
            == "globalroute"
        )

        assert (
            global_result["evaluation"]["stage"]
            == STAGE_GLOBAL_ROUTE
        )

        assert (
            global_result["diagnostics"]["drc_errors"]
            is None
        )

        # --------------------------------------------------------------
        # final must use 6_report.json / finish__...
        # --------------------------------------------------------------

        final_recipe = _selftest_recipe(
            flow_dir=flow_dir,
            design=design,
            platform=platform,
            stage=STAGE_FINAL,
        )

        final_result = collect_metrics(
            variant=variant,
            design=design,
            platform=platform,
            flow_dir=flow_dir,
            physical_recipe=final_recipe,
        )

        assert final_result["flow_complete"] is True
        assert final_result["metrics"] == {
            "wns_ns": 0.196096,
            "tns_ns": 0.0,
            "area_um2": 1228.92,
            "power_mw": 2.13873,
        }

        assert (
            final_result["stage_source"]["json_file"]
            == str(log_dir / "6_report.json")
        )

        assert (
            final_result["diagnostics"]["drc_errors"]
            == 0
        )

        # --------------------------------------------------------------
        # synth may not borrow later timing/power metrics.
        # --------------------------------------------------------------

        synth_recipe = _selftest_recipe(
            flow_dir=flow_dir,
            design=design,
            platform=platform,
            stage=STAGE_SYNTH,
        )

        synth_result = collect_metrics(
            variant=variant,
            design=design,
            platform=platform,
            flow_dir=flow_dir,
            physical_recipe=synth_recipe,
        )

        assert synth_result["flow_complete"] is False

        assert synth_result["metrics"] == {
            "wns_ns": None,
            "tns_ns": None,
            "area_um2": 1106.56,
            "power_mw": None,
        }

        assert set(
            synth_result["missing_required_metrics"]
        ) == {
            "wns_ns",
            "tns_ns",
            "power_mw",
        }

        # --------------------------------------------------------------
        # Legacy no-recipe behavior remains final-stage.
        # --------------------------------------------------------------

        legacy = collect_metrics(
            variant=variant,
            design=design,
            platform=platform,
            flow_dir=flow_dir,
        )

        assert legacy["flow_complete"] is True
        assert legacy["evaluation"]["stage"] is None
        assert legacy["metrics"]["wns_ns"] == 0.196096
        assert legacy["metrics"]["power_mw"] == 2.13873

        # --------------------------------------------------------------
        # Frozen experiment files may not mutate.
        # --------------------------------------------------------------

        sdc.write_text(
            "create_clock -name clk -period 0.8 "
            "[get_ports clk]\n",
            encoding="utf-8",
        )

        try:
            collect_metrics(
                variant=variant,
                design=design,
                platform=platform,
                flow_dir=flow_dir,
                physical_recipe=global_recipe,
            )
        except ValueError as exc:
            assert (
                "physical recipe verification failed"
                in str(exc)
            )
        else:
            raise AssertionError(
                "mutated frozen constraint should "
                "reject metric collection"
            )

        print("PASS: collect_metrics self-test")
        print(
            "PASS: canonical WNS/TNS/area/power "
            "six-decimal normalization preserved"
        )
        print(
            "PASS: global_route reads only "
            "5_1_grt.json globalroute__ metrics"
        )
        print(
            "PASS: final reads only "
            "6_report.json finish__ metrics"
        )
        print(
            "PASS: synth does not borrow timing/power "
            "from later stages"
        )
        print(
            "PASS: future detailed-route diagnostics "
            "excluded from global_route result"
        )
        print(
            "PASS: physical recipe stage and fingerprint "
            "stamped into metrics"
        )
        print(
            "PASS: frozen recipe files re-verified "
            "before collection"
        )
        print(
            "PASS: legacy no-recipe collection remains "
            "final-stage compatible"
        )


# =============================================================================
# CLI
# =============================================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Collect stage-aware canonical WNS/TNS/area/power "
            "metrics from an ORFS FLOW_VARIANT."
        )
    )

    parser.add_argument(
        "--variant",
        required=False,
        help="FLOW_VARIANT generated by run_flow.py",
    )

    parser.add_argument(
        "--design",
        default="timing_demo",
    )

    parser.add_argument(
        "--platform",
        default="nangate45",
    )

    parser.add_argument(
        "--flow-dir",
        default=str(DEFAULT_FLOW_DIR),
    )

    parser.add_argument(
        "--physical-recipe",
        default=None,
        help=(
            "Optional physical_recipe/1 JSON. "
            "Its stage selects the exact ORFS metric JSON "
            "and its fingerprint is stamped into the output."
        ),
    )

    parser.add_argument(
        "--output",
        default=None,
    )

    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run standalone stage-aware regression tests.",
    )

    args = parser.parse_args()

    if args.self_test:
        run_self_test()
        return 0

    if not args.variant:
        parser.error(
            "--variant is required unless --self-test is used"
        )

    recipe = (
        PhysicalRecipe.read_json(
            Path(args.physical_recipe)
        )
        if args.physical_recipe
        else None
    )

    result = collect_metrics(
        variant=args.variant,
        design=args.design,
        platform=args.platform,
        flow_dir=Path(args.flow_dir),
        physical_recipe=recipe,
    )

    output_file = save_metrics(
        result,
        Path(args.flow_dir),
        (
            Path(args.output)
            if args.output
            else None
        ),
    )

    metrics = result["metrics"]
    diagnostics = result["diagnostics"]

    print("========================================")
    print("Canonical PPA Metrics")
    print("========================================")

    def render(
        value: Any,
        unit: str = "",
    ) -> str:
        if value is None:
            return "MISSING"

        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
        ):
            return (
                f"{float(value):.{METRIC_DECIMALS}f}"
                f"{unit}"
            )

        return f"{value}{unit}"

    print(
        f"WNS              : "
        f"{render(metrics['wns_ns'], ' ns')}"
    )
    print(
        f"TNS              : "
        f"{render(metrics['tns_ns'], ' ns')}"
    )
    print(
        f"Area             : "
        f"{render(metrics['area_um2'], ' um^2')}"
    )
    print(
        f"Power            : "
        f"{render(metrics['power_mw'], ' mW')}"
    )

    print("\nStage source")
    print("----------------------------------------")
    print(
        f"Stage            : "
        f"{result['stage_source']['stage']}"
    )
    print(
        f"JSON             : "
        f"{result['stage_source']['json_file']}"
    )
    print(
        f"ORFS prefix      : "
        f"{result['stage_source']['prefix']}"
    )

    print("\nDiagnostics")
    print("----------------------------------------")
    print(
        f"Synthesis Area   : "
        f"{diagnostics.get('synth_area_um2')}"
    )
    print(
        f"Setup Violations : "
        f"{diagnostics.get('setup_violations')}"
    )
    print(
        f"Hold Violations  : "
        f"{diagnostics.get('hold_violations')}"
    )
    print(
        f"DRC Errors       : "
        f"{diagnostics.get('drc_errors')}"
    )
    print(
        f"Wirelength       : "
        f"{diagnostics.get('wirelength_um')}"
    )

    if result["clocks"]:
        print("\nClock constraints:")
        for clock in result["clocks"]:
            print(
                f"  {clock['name']}: "
                f"{clock['period_ns']} ns"
            )

    if result["missing_required_metrics"]:
        print(
            "\nERROR: Missing required canonical metrics:"
        )
        for metric in result[
            "missing_required_metrics"
        ]:
            print(f"  - {metric}")

    if result["malformed_metric_files"]:
        print(
            "\nWARNING: Some ORFS metric JSON files "
            "could not be read:"
        )
        for item in result[
            "malformed_metric_files"
        ]:
            print(
                f"  - {item['file']}: "
                f"{item['error']}"
            )

    print("\n========================================")
    print(
        "Run status: VALID"
        if result["flow_complete"]
        else "Run status: INCOMPLETE"
    )
    print("========================================")

    print(
        f"\nMetrics written to:\n{output_file}"
    )

    return 0 if result["flow_complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
