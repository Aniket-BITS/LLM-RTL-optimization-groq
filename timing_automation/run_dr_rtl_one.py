#!/usr/bin/env python3

"""
run_dr_rtl_one.py

General single-design DR_RTL -> ORFS baseline runner with adaptive floorplanning.

For ONE DR_RTL design, this script:

  1. Reads upstream DR_RTL metadata.
  2. Stages the original RTL without modifying it.
  3. Generates ORFS config.mk and constraint.sdc.
  4. Runs a Yosys compatibility preflight.
  5. Counts top-level IO bits.
  6. Starts ORFS with normal utilization-based auto floorplanning.
  7. If ORFS fails for a known floorplan reason:
       - PDN too small  -> generate a larger minimum explicit floorplan
       - IO perimeter   -> enlarge die/core from the required perimeter
     then retries with a fresh ORFS variant.
  8. Stops immediately for unknown failures.
  9. Prints canonical baseline PPA if run_analysis.py succeeds.

The original DR_RTL RTL is never edited.

Examples:

    python3 timing_automation/run_dr_rtl_one.py --list

    python3 timing_automation/run_dr_rtl_one.py --design ticket_machine

    python3 timing_automation/run_dr_rtl_one.py --design vending_machine

    python3 timing_automation/run_dr_rtl_one.py \
        --design controller \
        --clock-period 1.0

    python3 timing_automation/run_dr_rtl_one.py \
        --design ticket_machine \
        --prepare-only

"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Repository paths
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
FLOW_DIR = SCRIPT_DIR.parent
ORFS_ROOT = FLOW_DIR.parent

DR_RTL_ROOT = FLOW_DIR / "external" / "DR_RTL"
DR_RTL_DATASET = DR_RTL_ROOT / "rtl_dataset"
DR_RTL_METADATA = DR_RTL_ROOT / "syn_flow" / "design_all.json"

DESIGNS_DIR = FLOW_DIR / "designs"
AI_RUNS_DIR = FLOW_DIR / "ai_runs"

RUN_ANALYSIS = SCRIPT_DIR / "run_analysis.py"

DEFAULT_YOSYS = (
    ORFS_ROOT
    / "tools"
    / "install"
    / "yosys"
    / "bin"
    / "yosys"
)

PLATFORM = "nangate45"

# Default physical policy.
DEFAULT_CORE_UTILIZATION = 40.0
DEFAULT_PLACE_DENSITY = 0.60
DEFAULT_CORE_MARGIN_UM = 2.0

# Small safety margin added around a parsed PDN minimum.
PDN_EXTRA_CORE_MARGIN_UM = 10.0

# Add margin beyond the perimeter OpenROAD says is minimally required.
IO_PERIMETER_HEADROOM = 1.15

# Round explicit die dimensions to a clean grid.
DIE_ROUNDING_UM = 10.0


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def slugify(name: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_]+", "_", name.strip())
    value = value.strip("_").lower()
    return f"dr_{value}"


def round_up(value: float, quantum: float) -> float:
    return math.ceil(value / quantum) * quantum


def print_cmd(cmd: List[object]) -> None:
    print()
    print("  $ " + " ".join(str(x) for x in cmd))
    print()


def run_capture(cmd: List[object], cwd: Path) -> subprocess.CompletedProcess:
    print_cmd(cmd)
    return subprocess.run(
        [str(x) for x in cmd],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def run_streaming(
    cmd: List[object],
    cwd: Path,
    log_path: Optional[Path] = None,
) -> Tuple[int, str]:
    """
    Stream subprocess stdout to the terminal while also retaining it for
    adaptive failure classification.
    """
    print_cmd(cmd)

    proc = subprocess.Popen(
        [str(x) for x in cmd],
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    captured: List[str] = []

    log_file = None
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = log_path.open("w", encoding="utf-8")

    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            print(line, end="")
            captured.append(line)
            if log_file is not None:
                log_file.write(line)
    finally:
        if log_file is not None:
            log_file.close()

    rc = proc.wait()
    return rc, "".join(captured)


# ---------------------------------------------------------------------------
# DR_RTL metadata
# ---------------------------------------------------------------------------

def load_dr_metadata() -> Dict[str, object]:
    if not DR_RTL_METADATA.is_file():
        raise FileNotFoundError(
            "\nDR_RTL metadata not found:\n"
            f"  {DR_RTL_METADATA}\n\n"
            "Clone it first:\n\n"
            "  cd ~/OpenROAD-flow-scripts/flow\n"
            "  mkdir -p external\n"
            "  git clone --depth 1 "
            "https://github.com/hkust-zhiyao/DR_RTL.git "
            "external/DR_RTL\n"
        )

    data = json.loads(DR_RTL_METADATA.read_text(encoding="utf-8"))

    if not isinstance(data, dict):
        raise RuntimeError("design_all.json does not contain a JSON object")

    return data


def resolve_design_name(requested: str, metadata: Dict[str, object]) -> str:
    if requested in metadata:
        return requested

    matches = [
        name
        for name in metadata
        if name.lower() == requested.lower()
    ]

    if len(matches) == 1:
        return matches[0]

    available = ", ".join(sorted(metadata))
    raise ValueError(
        f"unknown DR_RTL design: {requested}\n\n"
        f"Available designs:\n{available}"
    )


def parse_design_metadata(design: str, raw: object) -> Dict[str, str]:
    """
    Expected upstream shape:

        "FIFO": ["fifo", "clk_in", "rst", "v"]

    -> top, clock, reset, extension
    """
    if not isinstance(raw, list) or len(raw) < 4:
        raise ValueError(f"invalid metadata for {design}: {raw!r}")

    return {
        "name": design,
        "top": str(raw[0]),
        "clock": str(raw[1]),
        "reset": str(raw[2]),
        "extension": str(raw[3]).lstrip("."),
    }


def locate_source(meta: Dict[str, str]) -> Path:
    expected = (
        DR_RTL_DATASET
        / f"{meta['name']}.v0.{meta['extension']}"
    )

    if expected.is_file():
        return expected

    candidates = list(
        DR_RTL_DATASET.glob(
            f"{meta['name']}*.{meta['extension']}"
        )
    )

    if len(candidates) == 1:
        return candidates[0]

    raise FileNotFoundError(
        f"could not locate RTL for {meta['name']}\n"
        f"Expected: {expected}"
    )


def resolve_yosys() -> Path:
    if DEFAULT_YOSYS.is_file() and os.access(DEFAULT_YOSYS, os.X_OK):
        return DEFAULT_YOSYS

    found = shutil.which("yosys")
    if found:
        return Path(found)

    raise FileNotFoundError(
        "Yosys not found.\n"
        f"Expected ORFS Yosys at:\n  {DEFAULT_YOSYS}"
    )


# ---------------------------------------------------------------------------
# Floorplan policy
# ---------------------------------------------------------------------------

def auto_floorplan_policy() -> Dict[str, object]:
    return {
        "mode": "auto",
        "core_utilization": DEFAULT_CORE_UTILIZATION,
        "place_density": DEFAULT_PLACE_DENSITY,
    }


def explicit_floorplan_policy(
    die_side_um: float,
    reason: str,
) -> Dict[str, object]:
    die_side_um = round_up(
        max(die_side_um, 20.0),
        DIE_ROUNDING_UM,
    )

    core_margin = DEFAULT_CORE_MARGIN_UM
    core_hi = die_side_um - core_margin

    if core_hi <= core_margin:
        raise ValueError(
            f"invalid explicit die size: {die_side_um} um"
        )

    return {
        "mode": "explicit",
        "die_side_um": round(die_side_um, 3),
        "core_margin_um": core_margin,
        "place_density": DEFAULT_PLACE_DENSITY,
        "reason": reason,
    }


def floorplan_lines(policy: Dict[str, object]) -> str:
    if policy["mode"] == "auto":
        return (
            f"export CORE_UTILIZATION = "
            f"{float(policy['core_utilization']):g}\n"
            f"export CORE_ASPECT_RATIO = 1.0\n"
            f"export CORE_MARGIN = "
            f"{DEFAULT_CORE_MARGIN_UM:g}\n"
            f"export PLACE_DENSITY = "
            f"{float(policy['place_density']):.2f}\n"
        )

    if policy["mode"] == "explicit":
        side = float(policy["die_side_um"])
        margin = float(policy["core_margin_um"])
        hi = side - margin

        return (
            f"export DIE_AREA = 0 0 {side:g} {side:g}\n"
            f"export CORE_AREA = "
            f"{margin:g} {margin:g} {hi:g} {hi:g}\n"
            f"export PLACE_DENSITY = "
            f"{float(policy['place_density']):.2f}\n"
        )

    raise ValueError(f"unknown floorplan mode: {policy['mode']}")


# ---------------------------------------------------------------------------
# ORFS design generation
# ---------------------------------------------------------------------------

def write_config(
    meta: Dict[str, str],
    design_id: str,
    staged_rtl: Path,
    sdc_path: Path,
    config_path: Path,
    policy: Dict[str, object],
) -> None:
    rtl_relative = (
        f"$(DESIGN_HOME)/src/"
        f"{design_id}/{staged_rtl.name}"
    )

    sdc_relative = (
        f"$(DESIGN_HOME)/{PLATFORM}/"
        f"{design_id}/{sdc_path.name}"
    )

    config = f"""\
# Auto-generated by timing_automation/run_dr_rtl_one.py
#
# DR_RTL source design : {meta["name"]}
# Verilog top          : {meta["top"]}
# Clock                : {meta["clock"]}
# Reset                : {meta["reset"]}
# Floorplan mode       : {policy["mode"]}

export PLATFORM = {PLATFORM}

# DESIGN_NAME is the Verilog top.
export DESIGN_NAME = {meta["top"]}

# Keep ORFS output directories aligned with our benchmark identifier.
export DESIGN_NICKNAME = {design_id}

export VERILOG_FILES = {rtl_relative}
export SDC_FILE = {sdc_relative}

{floorplan_lines(policy)}
"""

    config_path.write_text(config, encoding="utf-8")


def update_manifest(
    manifest_path: Path,
    updates: Dict[str, object],
) -> None:
    if manifest_path.is_file():
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
    else:
        data = {}

    data.update(updates)

    manifest_path.write_text(
        json.dumps(data, indent=2) + "\n",
        encoding="utf-8",
    )


def prepare_design(
    meta: Dict[str, str],
    source: Path,
    clock_period: float,
    force: bool,
) -> Dict[str, object]:
    design_id = slugify(meta["name"])

    src_dir = DESIGNS_DIR / "src" / design_id
    cfg_dir = DESIGNS_DIR / PLATFORM / design_id

    src_dir.mkdir(parents=True, exist_ok=True)
    cfg_dir.mkdir(parents=True, exist_ok=True)

    staged_rtl = src_dir / f"{design_id}.{meta['extension']}"
    config_path = cfg_dir / "config.mk"
    sdc_path = cfg_dir / "constraint.sdc"
    manifest_path = cfg_dir / "dr_rtl_manifest.json"

    source_hash = sha256_file(source)

    if staged_rtl.exists():
        staged_hash = sha256_file(staged_rtl)

        if staged_hash != source_hash:
            if not force:
                raise RuntimeError(
                    f"{staged_rtl} already exists and differs from "
                    "the upstream DR_RTL RTL.\n"
                    "Use --force to restage the original benchmark."
                )
            shutil.copy2(source, staged_rtl)
    else:
        shutil.copy2(source, staged_rtl)

    sdc = f"""\
# Auto-generated by run_dr_rtl_one.py
#
# Original DR_RTL design : {meta["name"]}
# Top module             : {meta["top"]}
# Clock port             : {meta["clock"]}
# Clock period           : {clock_period:.6f} ns

create_clock \\
    -name core_clock \\
    -period {clock_period:.6f} \\
    [get_ports {{{meta["clock"]}}}]

set_clock_uncertainty 0.000 [get_clocks core_clock]
"""

    sdc_path.write_text(sdc, encoding="utf-8")

    policy = auto_floorplan_policy()

    write_config(
        meta=meta,
        design_id=design_id,
        staged_rtl=staged_rtl,
        sdc_path=sdc_path,
        config_path=config_path,
        policy=policy,
    )

    manifest = {
        "suite": "DR_RTL",
        "source_repository": "https://github.com/hkust-zhiyao/DR_RTL",
        "source_design": meta["name"],
        "orfs_design": design_id,
        "top": meta["top"],
        "clock": meta["clock"],
        "reset": meta["reset"],
        "clock_period_ns": round(float(clock_period), 6),
        "language_extension": meta["extension"],
        "upstream_rtl": str(source.resolve()),
        "staged_rtl": str(staged_rtl.resolve()),
        "rtl_sha256": source_hash,
        "config": str(config_path.resolve()),
        "sdc": str(sdc_path.resolve()),
        "floorplan": policy,
        "adaptations": [],
    }

    manifest_path.write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )

    return {
        "design_id": design_id,
        "rtl": staged_rtl,
        "config": config_path,
        "sdc": sdc_path,
        "manifest": manifest_path,
        "floorplan": policy,
    }


# ---------------------------------------------------------------------------
# Yosys compatibility preflight
# ---------------------------------------------------------------------------

def yosys_preflight(
    yosys: Path,
    rtl: Path,
    top: str,
) -> Tuple[bool, str]:
    """
    Parse/elaborate/proc the RTL and reject known sequential forms that the
    current Nangate45 flow cannot safely map.
    """
    print()
    print("=" * 72)
    print("YOSYS PREFLIGHT")
    print("=" * 72)

    script = (
        f'read_verilog -sv "{rtl.resolve()}"; '
        f"hierarchy -check -top {top}; "
        f"proc; "
        f"opt; "
        f"check"
    )

    proc = run_capture(
        [
            yosys,
            "-p",
            script,
        ],
        cwd=FLOW_DIR,
    )

    output = (proc.stdout or "") + "\n" + (proc.stderr or "")

    if proc.returncode != 0:
        print("FAIL: Yosys elaboration")
        print(output[-5000:])
        return False, "yosys elaboration failed"

    incompatible_patterns = {
        "Async reset value": (
            "non-constant asynchronous reset/load detected"
        ),
        "$aldff": (
            "asynchronous-load flip-flop detected"
        ),
        "$_ALDFF_": (
            "unmapped asynchronous-load flip-flop detected"
        ),
    }

    problems = sorted(
        {
            reason
            for pattern, reason in incompatible_patterns.items()
            if pattern in output
        }
    )

    if problems:
        print("UNSUPPORTED FOR CURRENT NANGATE45 FLOW")
        for problem in problems:
            print(f"  - {problem}")

        return False, "; ".join(problems)

    print("PASS")
    print(
        f"Top module '{top}' elaborated and passed "
        "the technology-compatibility preflight."
    )

    return True, "pass"


def count_top_level_io_bits(
    yosys: Path,
    rtl: Path,
    top: str,
) -> Optional[Dict[str, int]]:
    """
    Count top-level port *bits*, not just port names.
    """
    fd, tmp_name = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    json_path = Path(tmp_name)

    try:
        script = (
            f'read_verilog -sv "{rtl.resolve()}"; '
            f"hierarchy -check -top {top}; "
            f"proc; opt; "
            f'write_json "{json_path}"'
        )

        proc = subprocess.run(
            [
                str(yosys),
                "-q",
                "-p",
                script,
            ],
            cwd=FLOW_DIR,
            capture_output=True,
            text=True,
            check=False,
        )

        if proc.returncode != 0:
            return None

        data = json.loads(json_path.read_text(encoding="utf-8"))
        modules = data.get("modules", {})

        module = modules.get(top)

        if module is None:
            for raw_name, candidate in modules.items():
                normalized = str(raw_name).lstrip("\\")
                if normalized == top:
                    module = candidate
                    break

        if module is None:
            return None

        counts = {
            "inputs": 0,
            "outputs": 0,
            "inouts": 0,
        }

        for port in module.get("ports", {}).values():
            width = len(port.get("bits", []))
            direction = port.get("direction")

            if direction == "input":
                counts["inputs"] += width
            elif direction == "output":
                counts["outputs"] += width
            elif direction == "inout":
                counts["inouts"] += width

        counts["total"] = (
            counts["inputs"]
            + counts["outputs"]
            + counts["inouts"]
        )

        return counts

    except (
        OSError,
        KeyError,
        json.JSONDecodeError,
    ):
        return None

    finally:
        json_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Adaptive ORFS failure classification
# ---------------------------------------------------------------------------

PDN_RE = re.compile(
    r"Insufficient width\s*"
    r"\(([0-9.]+)\s*um\).*?"
    r"total strap width\s*([0-9.]+)\s*um"
    r".*?offset\s*([0-9.]+)\s*um",
    re.IGNORECASE | re.DOTALL,
)

IO_RE = re.compile(
    r"Number of IO pins\s*\((\d+)\)"
    r".*?maximum number of available positions\s*\((\d+)\)"
    r".*?Increase the die perimeter from\s*"
    r"([0-9.]+)um\s*to\s*([0-9.]+)um",
    re.IGNORECASE | re.DOTALL,
)

INSTANCE_AREA_RE = re.compile(
    r"Total instances area:\s*([0-9.]+)\s*um\^2",
    re.IGNORECASE,
)

DESIGN_AREA_RE = re.compile(
    r"Design area\s+([0-9.]+)\s+um\^2",
    re.IGNORECASE,
)


def parse_latest_cell_area(output: str) -> Optional[float]:
    instance_matches = INSTANCE_AREA_RE.findall(output)
    if instance_matches:
        return float(instance_matches[-1])

    design_matches = DESIGN_AREA_RE.findall(output)
    if design_matches:
        return float(design_matches[-1])

    return None


def area_required_die_side(
    cell_area_um2: Optional[float],
) -> float:
    """
    Conservative square die estimate from observed instance area.
    """
    if not cell_area_um2 or cell_area_um2 <= 0:
        return 0.0

    usable_core_area = cell_area_um2 / (
        DEFAULT_CORE_UTILIZATION / 100.0
    )

    core_side = math.sqrt(usable_core_area)

    die_side = (
        core_side
        + 2.0 * DEFAULT_CORE_MARGIN_UM
    )

    # Additional breathing room for placement/routing.
    return die_side * 1.10


def classify_adaptive_failure(
    output: str,
    current_policy: Dict[str, object],
    max_die_side_um: float,
) -> Tuple[Optional[Dict[str, object]], Optional[Dict[str, object]]]:
    """
    Returns:
        (new_floorplan_policy, adaptation_record)

    or:
        (None, None)

    when the failure is not one of the known adaptive floorplan cases.
    """

    cell_area = parse_latest_cell_area(output)
    area_side = area_required_die_side(cell_area)

    # ---------------------------------------------------------
    # Tiny-core PDN failure
    # ---------------------------------------------------------

    pdn = PDN_RE.search(output)

    if pdn:
        observed_width = float(pdn.group(1))
        strap_width = float(pdn.group(2))
        offset = float(pdn.group(3))

        required_core_side = (
            strap_width
            + 2.0 * offset
            + PDN_EXTRA_CORE_MARGIN_UM
        )

        required_die_side = (
            required_core_side
            + 2.0 * DEFAULT_CORE_MARGIN_UM
        )

        die_side = max(
            50.0,
            required_die_side,
            area_side,
        )

        die_side = round_up(
            die_side,
            DIE_ROUNDING_UM,
        )

        if die_side > max_die_side_um:
            return None, {
                "type": "pdn_too_small",
                "status": "refused",
                "reason": (
                    f"required adaptive die side {die_side:.1f} um "
                    f"exceeds --max-die-side {max_die_side_um:.1f} um"
                ),
            }

        policy = explicit_floorplan_policy(
            die_side_um=die_side,
            reason="adaptive PDN minimum",
        )

        return policy, {
            "type": "pdn_too_small",
            "status": "adapt",
            "observed_core_width_um": observed_width,
            "strap_width_um": strap_width,
            "offset_um": offset,
            "estimated_cell_area_um2": cell_area,
            "new_die_side_um": die_side,
        }

    # ---------------------------------------------------------
    # Too many IOs for available die perimeter
    # ---------------------------------------------------------

    io = IO_RE.search(output)

    if io:
        io_pins = int(io.group(1))
        available = int(io.group(2))
        old_perimeter = float(io.group(3))
        required_perimeter = float(io.group(4))

        # Square die perimeter = 4 * side.
        io_die_side = (
            required_perimeter
            / 4.0
            * IO_PERIMETER_HEADROOM
        )

        die_side = max(
            io_die_side,
            area_side,
            50.0,
        )

        die_side = round_up(
            die_side,
            DIE_ROUNDING_UM,
        )

        if die_side > max_die_side_um:
            return None, {
                "type": "io_perimeter",
                "status": "refused",
                "reason": (
                    f"required adaptive die side {die_side:.1f} um "
                    f"exceeds --max-die-side {max_die_side_um:.1f} um"
                ),
                "io_pins": io_pins,
                "available_positions": available,
                "required_perimeter_um": required_perimeter,
            }

        policy = explicit_floorplan_policy(
            die_side_um=die_side,
            reason="adaptive IO perimeter",
        )

        return policy, {
            "type": "io_perimeter",
            "status": "adapt",
            "io_pins": io_pins,
            "available_positions": available,
            "old_perimeter_um": old_perimeter,
            "required_perimeter_um": required_perimeter,
            "estimated_cell_area_um2": cell_area,
            "new_die_side_um": die_side,
        }

    return None, None


# ---------------------------------------------------------------------------
# Analysis artifact discovery
# ---------------------------------------------------------------------------

def pipeline_manifests() -> set[Path]:
    AI_RUNS_DIR.mkdir(parents=True, exist_ok=True)
    return set(AI_RUNS_DIR.glob("*_pipeline.json"))


def manifest_matches_design(
    path: Path,
    design_id: str,
) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False

    return data.get("design") == design_id


def find_new_pipeline_manifest(
    before: set[Path],
    design_id: str,
    start_time: float,
) -> Optional[Path]:
    after = pipeline_manifests()

    new_files = [
        p
        for p in (after - before)
        if manifest_matches_design(p, design_id)
    ]

    if new_files:
        return max(new_files, key=lambda p: p.stat().st_mtime)

    candidates = [
        p
        for p in after
        if (
            manifest_matches_design(p, design_id)
            and p.stat().st_mtime >= start_time - 2.0
        )
    ]

    if candidates:
        return max(candidates, key=lambda p: p.stat().st_mtime)

    return None


def load_metrics_from_variant(
    variant: str,
) -> Tuple[Path, Optional[Dict[str, object]]]:
    path = AI_RUNS_DIR / f"{variant}_metrics.json"

    if not path.is_file():
        return path, None

    data = json.loads(path.read_text(encoding="utf-8"))
    metrics = data.get("metrics", data)

    if not isinstance(metrics, dict):
        return path, None

    return path, metrics


def print_metrics(metrics: Dict[str, object]) -> None:
    required = (
        "wns_ns",
        "tns_ns",
        "area_um2",
        "power_mw",
    )

    if not all(k in metrics for k in required):
        print("Canonical metric object was not complete.")
        return

    print()
    print("Canonical baseline PPA")
    print("-" * 72)
    print(f"WNS    : {float(metrics['wns_ns']):.6f} ns")
    print(f"TNS    : {float(metrics['tns_ns']):.6f} ns")
    print(f"Area   : {float(metrics['area_um2']):.6f} um^2")
    print(f"Power  : {float(metrics['power_mw']):.6f} mW")


# ---------------------------------------------------------------------------
# Adaptive analysis loop
# ---------------------------------------------------------------------------

def run_analysis_adaptive(
    meta: Dict[str, str],
    prepared: Dict[str, object],
    max_adaptations: int,
    max_die_side_um: float,
) -> Tuple[int, Optional[Path]]:
    design_id = str(prepared["design_id"])
    rtl = Path(prepared["rtl"])
    sdc = Path(prepared["sdc"])
    config = Path(prepared["config"])
    manifest = Path(prepared["manifest"])

    current_policy = dict(prepared["floorplan"])
    adaptations_used = 0

    while True:
        manifests_before = pipeline_manifests()
        start_time = time.time()

        attempt_number = adaptations_used + 1
        log_path = (
            AI_RUNS_DIR
            / f"{design_id}_baseline_attempt_{attempt_number:02d}.log"
        )

        print()
        print("=" * 72)
        print(
            f"BASELINE ORFS ATTEMPT {attempt_number} "
            f"(floorplan={current_policy['mode']})"
        )
        print("=" * 72)

        rc, output = run_streaming(
            [
                sys.executable,
                RUN_ANALYSIS,
                "--design",
                design_id,
                "--platform",
                PLATFORM,
            ],
            cwd=FLOW_DIR,
            log_path=log_path,
        )

        if rc == 0:
            pipeline_path = find_new_pipeline_manifest(
                before=manifests_before,
                design_id=design_id,
                start_time=start_time,
            )

            update_manifest(
                manifest,
                {
                    "floorplan": current_policy,
                    "baseline_status": "pass",
                    "baseline_attempts": attempt_number,
                    "last_baseline_log": str(log_path.resolve()),
                },
            )

            return 0, pipeline_path

        # Unknown failures stop immediately.
        new_policy, adaptation = classify_adaptive_failure(
            output=output,
            current_policy=current_policy,
            max_die_side_um=max_die_side_um,
        )

        if adaptation is None:
            update_manifest(
                manifest,
                {
                    "floorplan": current_policy,
                    "baseline_status": "fail",
                    "baseline_failure": "unknown/non-adaptive ORFS failure",
                    "last_baseline_log": str(log_path.resolve()),
                },
            )

            return rc or 1, None

        # Record even a refused adaptation.
        manifest_data = {}
        try:
            manifest_data = json.loads(
                manifest.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            pass

        history = manifest_data.get("adaptations", [])
        if not isinstance(history, list):
            history = []

        adaptation["attempt"] = attempt_number
        adaptation["log"] = str(log_path.resolve())
        history.append(adaptation)

        update_manifest(
            manifest,
            {
                "adaptations": history,
            },
        )

        if new_policy is None:
            print()
            print("=" * 72)
            print("ADAPTATION REFUSED")
            print("=" * 72)
            print(adaptation.get("reason", "adaptive limit reached"))

            update_manifest(
                manifest,
                {
                    "baseline_status": "skip",
                    "baseline_failure": adaptation.get(
                        "reason",
                        "adaptive limit reached",
                    ),
                },
            )

            return 2, None

        if adaptations_used >= max_adaptations:
            print()
            print("=" * 72)
            print("ADAPTATION BUDGET EXHAUSTED")
            print("=" * 72)

            update_manifest(
                manifest,
                {
                    "baseline_status": "fail",
                    "baseline_failure": (
                        f"needed more than {max_adaptations} "
                        "floorplan adaptations"
                    ),
                },
            )

            return 2, None

        adaptations_used += 1
        current_policy = new_policy

        print()
        print("=" * 72)
        print("ADAPTIVE FLOORPLAN")
        print("=" * 72)

        print(f"Reason        : {adaptation['type']}")
        print(
            f"New die side  : "
            f"{float(new_policy['die_side_um']):.1f} um"
        )
        print(
            f"Core margin   : "
            f"{float(new_policy['core_margin_um']):.1f} um"
        )
        print(
            "Action        : rewrite config.mk and retry "
            "with a fresh flow variant"
        )

        write_config(
            meta=meta,
            design_id=design_id,
            staged_rtl=rtl,
            sdc_path=sdc,
            config_path=config,
            policy=current_policy,
        )

        update_manifest(
            manifest,
            {
                "floorplan": current_policy,
            },
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare and test one DR_RTL benchmark through the "
            "nEBULA/OpenROAD analysis pipeline with adaptive floorplanning."
        )
    )

    parser.add_argument(
        "--design",
        help="One DR_RTL design, e.g. ticket_machine, DSP, controller.",
    )

    parser.add_argument(
        "--clock-period",
        type=float,
        default=0.8,
        help="Clock period in ns. Default: 0.8.",
    )

    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help=(
            "Prepare files and run Yosys compatibility checks, "
            "but do not run ORFS."
        ),
    )

    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Restage the original upstream RTL if the local staged copy "
            "has been modified."
        ),
    )

    parser.add_argument(
        "--list",
        action="store_true",
        help="List available DR_RTL designs.",
    )

    parser.add_argument(
        "--max-adaptations",
        type=int,
        default=2,
        help=(
            "Maximum automatic floorplan rewrites after the initial ORFS "
            "attempt. Default: 2."
        ),
    )

    parser.add_argument(
        "--max-die-side",
        type=float,
        default=1000.0,
        help=(
            "Maximum square die side the adaptive floorplanner may create, "
            "in um. Larger requirements are recorded as SKIP. Default: 1000."
        ),
    )

    args = parser.parse_args()

    if args.clock_period <= 0:
        parser.error("--clock-period must be > 0")

    if args.max_adaptations < 0:
        parser.error("--max-adaptations must be >= 0")

    if args.max_die_side <= 0:
        parser.error("--max-die-side must be > 0")

    try:
        metadata = load_dr_metadata()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if args.list:
        print()
        print("Available DR_RTL designs")
        print("=" * 72)

        for name in sorted(metadata, key=str.lower):
            try:
                meta = parse_design_metadata(name, metadata[name])
                print(
                    f"{name:<20} "
                    f"top={meta['top']:<32} "
                    f"clk={meta['clock']}"
                )
            except ValueError:
                print(f"{name:<20} INVALID METADATA")

        return 0

    if not args.design:
        parser.error("--design is required unless --list is used")

    try:
        dr_name = resolve_design_name(args.design, metadata)
        meta = parse_design_metadata(dr_name, metadata[dr_name])
        source = locate_source(meta)
        yosys = resolve_yosys()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print()
    print("=" * 72)
    print("DR_RTL SINGLE-DESIGN TEST")
    print("=" * 72)
    print(f"DR_RTL design : {dr_name}")
    print(f"ORFS design   : {slugify(dr_name)}")
    print(f"Top module    : {meta['top']}")
    print(f"Clock         : {meta['clock']}")
    print(f"Reset         : {meta['reset']}")
    print(f"RTL           : {source}")
    print(f"Clock period  : {args.clock_period:.6f} ns")

    print()
    print("[1] preparing ORFS design ...")

    try:
        prepared = prepare_design(
            meta=meta,
            source=source,
            clock_period=args.clock_period,
            force=args.force,
        )
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"    RTL      : {prepared['rtl']}")
    print(f"    config   : {prepared['config']}")
    print(f"    SDC      : {prepared['sdc']}")
    print(f"    manifest : {prepared['manifest']}")

    print()
    print("[2] Yosys technology-compatibility preflight ...")

    ok, reason = yosys_preflight(
        yosys=yosys,
        rtl=Path(prepared["rtl"]),
        top=meta["top"],
    )

    if not ok:
        update_manifest(
            Path(prepared["manifest"]),
            {
                "baseline_status": "skip",
                "baseline_failure": reason,
            },
        )

        print()
        print("=" * 72)
        print("RESULT: SKIP")
        print("=" * 72)
        print(reason)

        return 2

    print()
    print("[3] counting top-level IO bits ...")

    io_stats = count_top_level_io_bits(
        yosys=yosys,
        rtl=Path(prepared["rtl"]),
        top=meta["top"],
    )

    if io_stats is not None:
        print(f"    Inputs  : {io_stats['inputs']}")
        print(f"    Outputs : {io_stats['outputs']}")
        print(f"    Inouts  : {io_stats['inouts']}")
        print(f"    Total   : {io_stats['total']}")

        update_manifest(
            Path(prepared["manifest"]),
            {
                "top_level_io_bits": io_stats,
            },
        )
    else:
        print("    unavailable")

    if args.prepare_only:
        print()
        print("=" * 72)
        print("RESULT: PREPARED")
        print("=" * 72)
        return 0

    if not RUN_ANALYSIS.is_file():
        print(
            f"ERROR: run_analysis.py not found: {RUN_ANALYSIS}",
            file=sys.stderr,
        )
        return 1

    print()
    print("[4] running adaptive baseline analysis ...")

    rc, pipeline_path = run_analysis_adaptive(
        meta=meta,
        prepared=prepared,
        max_adaptations=args.max_adaptations,
        max_die_side_um=args.max_die_side,
    )

    if rc != 0:
        print()
        print("=" * 72)
        print("RESULT: FAIL/SKIP")
        print("=" * 72)
        print(
            "See the DR_RTL manifest and baseline attempt logs "
            "for the recorded reason."
        )
        return rc

    if pipeline_path is None:
        print()
        print("=" * 72)
        print("RESULT: PARTIAL")
        print("=" * 72)
        print(
            "run_analysis.py returned success but the pipeline manifest "
            "could not be located."
        )
        return 1

    pipeline = json.loads(
        pipeline_path.read_text(encoding="utf-8")
    )

    variant = pipeline.get("flow_variant")

    print()
    print("=" * 72)
    print("DR_RTL TEST RESULT")
    print("=" * 72)
    print(f"Design       : {dr_name}")
    print(f"ORFS design  : {prepared['design_id']}")
    print(f"Top          : {meta['top']}")
    print(f"Flow variant : {variant}")
    print(f"Pipeline     : {pipeline_path}")

    if variant:
        metrics_path, metrics = load_metrics_from_variant(str(variant))
        print(f"Metrics      : {metrics_path}")

        if metrics:
            print_metrics(metrics)

    optimizer_ready = pipeline.get("optimizer_ready")
    if optimizer_ready is None:
        optimizer_ready = pipeline.get("ready_for_optimizer")

    if optimizer_ready is not None:
        print(
            f"Optimizer    : "
            f"{'READY' if optimizer_ready else 'NOT READY'}"
        )

    print()
    print("=" * 72)
    print("RESULT: PASS")
    print("=" * 72)

    return 0


if __name__ == "__main__":
    sys.exit(main())
