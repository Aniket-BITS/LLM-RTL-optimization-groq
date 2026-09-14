#!/usr/bin/env python3

import argparse
import json
import re
from pathlib import Path


# =========================================================
# Paths
# =========================================================

SCRIPT_DIR = Path(__file__).resolve().parent

# timing_automation/ is inside flow/
DEFAULT_FLOW_DIR = SCRIPT_DIR.parent


# =========================================================
# Important ORFS metrics
# =========================================================

METRIC_MAP = {
    # Main optimization metrics
    "wns": "finish__timing__setup__ws",
    "tns": "finish__timing__setup__tns",
    "area": "finish__design__instance__area",

    # Useful validity/debug metrics
    "setup_violations": "finish__timing__drv__setup_violation_count",
    "hold_violations": "finish__timing__drv__hold_violation_count",
    "wns_percent_delay": "finish__timing__wns_percent_delay",

    # Physical-design information
    "drc_errors": "detailedroute__route__drc_errors",
    "wirelength": "detailedroute__route__wirelength",

    # Useful comparison against synthesized RTL
    "synth_area": "synth__design__instance__area__stdcell",
}


# =========================================================
# Load ORFS stage JSON files
# =========================================================

def load_orfs_metrics(log_dir: Path):
    """
    Read every ORFS *.json metrics file for this FLOW_VARIANT.

    Returns:
        all_metrics:
            {
                "finish__timing__setup__ws": 0.04,
                ...
            }

        metric_sources:
            {
                "finish__timing__setup__ws":
                    ".../6_report.json"
            }

        malformed_files:
            [...]
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

    all_metrics = {}
    metric_sources = {}
    malformed_files = []

    for json_file in json_files:

        try:
            with open(json_file, "r") as f:
                data = json.load(f)

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

            # Later ORFS stages overwrite earlier values if a key
            # happens to appear more than once.
            all_metrics[key] = value
            metric_sources[key] = str(json_file)

    return all_metrics, metric_sources, malformed_files


# =========================================================
# Clock extraction
# =========================================================

def extract_clocks(flow_dir: Path, platform: str, design: str):
    """
    Try to extract create_clock periods directly from the design SDC.

    This is deliberately separate from timing metrics because
    the clock period is a constraint, not a measured PPA result.
    """

    design_dir = (
        flow_dir
        / "designs"
        / platform
        / design
    )

    if not design_dir.exists():
        return []

    sdc_files = sorted(design_dir.rglob("*.sdc"))

    clocks = []

    for sdc_file in sdc_files:

        try:
            text = sdc_file.read_text()
        except OSError:
            continue

        # Collapse Tcl line continuations
        text = text.replace("\\\n", " ")

        # Look at create_clock commands
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

            period = float(period_match.group(1))

            name_match = re.search(
                r"-name\s+([^\s\]]+)",
                command,
                flags=re.IGNORECASE,
            )

            clock_name = (
                name_match.group(1)
                if name_match
                else "unnamed"
            )

            clocks.append(
                {
                    "name": clock_name,
                    "period_ns": period,
                    "source": str(sdc_file),
                }
            )

    return clocks


# =========================================================
# Collect metrics
# =========================================================

def collect_metrics(
    variant: str,
    design: str = "timing_demo",
    platform: str = "nangate45",
    flow_dir: Path = DEFAULT_FLOW_DIR,
):
    """
    Collect final PPA/timing metrics for one unique ORFS run.
    """

    flow_dir = Path(flow_dir).resolve()

    log_dir = (
        flow_dir
        / "logs"
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
    print(f"Log Dir      : {log_dir}")
    print("========================================\n")

    (
        all_metrics,
        metric_sources,
        malformed_files,
    ) = load_orfs_metrics(log_dir)

    # -----------------------------------------------------
    # Extract the metrics we care about
    # -----------------------------------------------------

    metrics = {}
    sources = {}

    for friendly_name, orfs_name in METRIC_MAP.items():

        value = all_metrics.get(orfs_name)

        metrics[friendly_name] = value

        if value is not None:
            sources[friendly_name] = metric_sources.get(
                orfs_name
            )

    # -----------------------------------------------------
    # Clock constraints
    # -----------------------------------------------------

    clocks = extract_clocks(
        flow_dir,
        platform,
        design,
    )

    # -----------------------------------------------------
    # Validate critical metrics
    # -----------------------------------------------------

    required_metrics = [
        "wns",
        "tns",
        "area",
    ]

    missing_required = [
        metric
        for metric in required_metrics
        if metrics.get(metric) is None
    ]

    flow_complete = len(missing_required) == 0

    # -----------------------------------------------------
    # Build output JSON
    # -----------------------------------------------------

    result = {
        "design": design,
        "platform": platform,
        "flow_variant": variant,

        "flow_complete": flow_complete,

        "metrics": metrics,

        "clocks": clocks,

        "sources": sources,

        "missing_required_metrics": missing_required,

        "malformed_metric_files": malformed_files,
    }

    return result


# =========================================================
# Save result
# =========================================================

def save_metrics(
    result: dict,
    flow_dir: Path,
    output_file=None,
):

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

    with open(output_file, "w") as f:
        json.dump(
            result,
            f,
            indent=4,
        )

    return output_file


# =========================================================
# CLI
# =========================================================

def main():

    parser = argparse.ArgumentParser(
        description=(
            "Collect WNS, TNS, area and related "
            "metrics from an ORFS FLOW_VARIANT."
        )
    )

    parser.add_argument(
        "--variant",
        required=True,
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
        "--output",
        default=None,
    )

    args = parser.parse_args()

    result = collect_metrics(
        variant=args.variant,
        design=args.design,
        platform=args.platform,
        flow_dir=Path(args.flow_dir),
    )

    output_file = save_metrics(
        result,
        Path(args.flow_dir),
        args.output,
    )

    # -----------------------------------------------------
    # Human-readable summary
    # -----------------------------------------------------

    print("========================================")
    print("PPA / Timing Summary")
    print("========================================")

    metrics = result["metrics"]

    print(f"WNS              : {metrics['wns']}")
    print(f"TNS              : {metrics['tns']}")
    print(f"Area             : {metrics['area']}")
    print(f"Synthesis Area   : {metrics['synth_area']}")
    print(f"Setup Violations : {metrics['setup_violations']}")
    print(f"Hold Violations  : {metrics['hold_violations']}")
    print(f"DRC Errors       : {metrics['drc_errors']}")
    print(f"Wirelength       : {metrics['wirelength']}")

    if result["clocks"]:

        print("\nClock constraints:")

        for clock in result["clocks"]:
            print(
                f"  {clock['name']}: "
                f"{clock['period_ns']} ns"
            )

    if result["missing_required_metrics"]:

        print("\nWARNING: Missing required metrics:")

        for metric in result["missing_required_metrics"]:
            print(f"  - {metric}")

    if result["malformed_metric_files"]:

        print("\nWARNING: Some JSON files could not be read:")

        for item in result["malformed_metric_files"]:
            print(f"  - {item['file']}")

    print("\n========================================")

    if result["flow_complete"]:
        print("Run status: VALID")
    else:
        print("Run status: INCOMPLETE")

    print("========================================")

    print(f"\nMetrics written to:\n{output_file}")


if __name__ == "__main__":
    main()