#!/usr/bin/env python3

import argparse
import hashlib
import json
import os
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path


# =========================================================
# Directory setup
# =========================================================

# This file:
# OpenROAD-flow-scripts/flow/timing_automation/run_flow.py
SCRIPT_DIR = Path(__file__).resolve().parent

# ORFS flow directory:
# OpenROAD-flow-scripts/flow/
FLOW_DIR = SCRIPT_DIR.parent

# Timing report script
REPORT_CHECKS_TCL = SCRIPT_DIR / "report_checks.tcl"

# Run metadata directory
AI_RUNS_DIR = FLOW_DIR / "ai_runs"



# =========================================================
# Flow-input cryptographic provenance
# =========================================================

FLOW_INPUT_SCHEMA_VERSION = 2

# Files in a design tree that are orchestration/benchmark metadata rather
# than inputs consumed by ORFS. These must not invalidate physical-run
# provenance when they are updated after a run.
NON_FLOW_METADATA_FILES = {
    "dr_rtl_manifest.json",
}


def _is_non_flow_metadata(path):
    """Return True for files that cannot affect the ORFS implementation."""
    return Path(path).name in NON_FLOW_METADATA_FILES


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path):
    """
    Hash the exact file bytes.
    """
    h = hashlib.sha256()

    with path.open("rb") as f:
        for chunk in iter(
            lambda: f.read(1024 * 1024),
            b"",
        ):
            h.update(chunk)

    return h.hexdigest()


def _snapshot_file(path, role):
    resolved = path.resolve()
    stat = resolved.stat()

    return {
        "path": str(resolved),
        "role": role,
        "sha256": sha256_file(resolved),
        "size_bytes": int(stat.st_size),
    }


def _standard_flow_input_files(
    design,
    platform,
):
    """
    Conservatively discover all normal ORFS design inputs.

    Rather than attempting to partially evaluate arbitrary Make syntax in
    config.mk, hash every regular file under the two standard design trees:

        designs/src/<design>/
        designs/<platform>/<design>/

    plus report_checks.tcl.

    This is deliberately conservative: an unrelated file added/changed in one
    of these design trees may force a fresh ORFS run, but an RTL/SDC/config
    change inside the normal ORFS design structure cannot be silently reused.
    """

    roots = [
        (
            "design_source_tree",
            FLOW_DIR
            / "designs"
            / "src"
            / design,
        ),
        (
            "platform_design_tree",
            FLOW_DIR
            / "designs"
            / platform
            / design,
        ),
    ]

    discovered = {}

    for role, root in roots:
        if not root.exists():
            continue

        for path in root.rglob("*"):
            try:
                if not path.is_file():
                    continue
            except OSError:
                continue

            resolved = path.resolve()

            # Avoid transient editor/OS files becoming part of provenance.
            # Real design backups should live outside the active design tree.
            if resolved.name.endswith(
                (
                    "~",
                    ".swp",
                    ".swo",
                    ".tmp",
                )
            ):
                continue

            # Benchmark/orchestration manifests may be updated after ORFS
            # completes. They are not consumed by ORFS and therefore are not
            # legitimate physical-flow inputs.
            if _is_non_flow_metadata(resolved):
                continue

            discovered[str(resolved)] = (
                resolved,
                role,
            )

    if REPORT_CHECKS_TCL.exists():
        resolved = REPORT_CHECKS_TCL.resolve()
        discovered[str(resolved)] = (
            resolved,
            "report_checks_tcl",
        )

    return [
        discovered[key]
        for key in sorted(discovered)
    ]


def _aggregate_snapshot(files):
    """
    Hash the ordered (path, role, SHA-256, size) manifest itself.
    """
    h = hashlib.sha256()

    for item in sorted(
        files,
        key=lambda entry: entry["path"],
    ):
        record = (
            f"{item['path']}\0"
            f"{item['role']}\0"
            f"{item['sha256']}\0"
            f"{item['size_bytes']}\n"
        )
        h.update(
            record.encode("utf-8")
        )

    return h.hexdigest()


def capture_flow_input_snapshot(
    design,
    platform,
):
    """
    Capture the current cryptographic state of all standard design inputs.

    This snapshot is the contract tying an ORFS FLOW_VARIANT to the source,
    configuration/constraint tree, and report-generation script used for it.
    """

    files = []

    for path, role in _standard_flow_input_files(
        design,
        platform,
    ):
        try:
            files.append(
                _snapshot_file(
                    path,
                    role,
                )
            )
        except OSError as exc:
            raise RuntimeError(
                "Could not hash flow input "
                f"{path}: {exc}"
            )

    if not files:
        raise RuntimeError(
            "No standard ORFS design inputs were found "
            f"for design={design!r}, platform={platform!r}."
        )

    return {
        "schema_version": FLOW_INPUT_SCHEMA_VERSION,
        "hash_algorithm": "sha256",
        "capture_scope": (
            "standard_orfs_design_trees_plus_report_checks_excluding_non_flow_metadata"
        ),
        "captured_at": utc_now(),
        "files": files,
        "file_count": len(files),
        "aggregate_sha256": _aggregate_snapshot(
            files
        ),
    }


def compare_flow_input_snapshots(
    expected,
    current,
):
    """
    Compare two complete flow-input snapshots.

    Non-flow metadata is removed from BOTH snapshots before comparison.

    This is intentionally backward-compatible with early DR_RTL runs whose
    snapshots accidentally included dr_rtl_manifest.json. Therefore an old
    physical run can still be resumed when that manifest changed, while any
    real RTL/config/SDC/report input change continues to invalidate reuse.
    """

    def filtered_items(snapshot):
        items = []

        for item in snapshot.get(
            "files",
            [],
        ):
            if not isinstance(
                item,
                dict,
            ):
                continue

            path = item.get(
                "path"
            )

            if not path:
                continue

            if _is_non_flow_metadata(
                path
            ):
                continue

            items.append(
                item
            )

        return items

    expected_items = filtered_items(
        expected
    )

    current_items = filtered_items(
        current
    )

    expected_files = {
        str(item.get("path")): item
        for item in expected_items
    }

    current_files = {
        str(item.get("path")): item
        for item in current_items
    }

    expected_paths = set(
        expected_files
    )

    current_paths = set(
        current_files
    )

    added = sorted(
        current_paths
        - expected_paths
    )

    removed = sorted(
        expected_paths
        - current_paths
    )

    modified = []

    for path in sorted(
        expected_paths
        & current_paths
    ):
        before = expected_files[
            path
        ]

        after = current_files[
            path
        ]

        if (
            before.get("sha256")
            != after.get("sha256")
            or before.get("size_bytes")
            != after.get("size_bytes")
            or before.get("role")
            != after.get("role")
        ):
            modified.append(
                {
                    "path": path,
                    "role": (
                        after.get("role")
                        or before.get("role")
                    ),
                    "expected_sha256": before.get(
                        "sha256"
                    ),
                    "current_sha256": after.get(
                        "sha256"
                    ),
                    "expected_size_bytes": before.get(
                        "size_bytes"
                    ),
                    "current_size_bytes": after.get(
                        "size_bytes"
                    ),
                }
            )

    # Recompute both aggregates over the filtered physical-input view.
    # Stored aggregate hashes from older snapshots may include metadata
    # files that are now intentionally excluded.
    expected_aggregate = _aggregate_snapshot(
        expected_items
    )

    current_aggregate = _aggregate_snapshot(
        current_items
    )

    matches = (
        not added
        and not removed
        and not modified
        and expected_aggregate
        == current_aggregate
    )

    ignored_expected = sorted(
        str(item.get("path"))
        for item in expected.get(
            "files",
            [],
        )
        if isinstance(
            item,
            dict,
        )
        and item.get(
            "path"
        )
        and _is_non_flow_metadata(
            item.get("path")
        )
    )

    ignored_current = sorted(
        str(item.get("path"))
        for item in current.get(
            "files",
            [],
        )
        if isinstance(
            item,
            dict,
        )
        and item.get(
            "path"
        )
        and _is_non_flow_metadata(
            item.get("path")
        )
    )

    return {
        "matches": bool(
            matches
        ),
        "expected_aggregate_sha256": expected_aggregate,
        "current_aggregate_sha256": current_aggregate,
        "added_files": added,
        "removed_files": removed,
        "modified_files": modified,
        "ignored_non_flow_metadata": sorted(
            set(
                ignored_expected
                + ignored_current
            )
        ),
    }


def format_flow_input_mismatch(
    differences,
):
    lines = [
        "ORFS flow-input provenance mismatch.",
    ]

    for item in differences.get(
        "modified_files",
        [],
    ):
        lines.extend(
            [
                "",
                f"Modified: {item.get('path')}",
                (
                    "  flow SHA   : "
                    f"{item.get('expected_sha256')}"
                ),
                (
                    "  current SHA: "
                    f"{item.get('current_sha256')}"
                ),
            ]
        )

    for path in differences.get(
        "added_files",
        [],
    ):
        lines.append(
            f"Added after flow run: {path}"
        )

    for path in differences.get(
        "removed_files",
        [],
    ):
        lines.append(
            f"Removed after flow run: {path}"
        )

    lines.extend(
        [
            "",
            (
                "The current design inputs no longer match "
                "the inputs used for this physical run."
            ),
            (
                "Start a fresh ORFS analysis; do not reuse "
                "this FLOW_VARIANT."
            ),
        ]
    )

    return "\n".join(
        lines
    )


def write_run_metadata_atomic(
    path,
    data,
):
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    tmp = path.with_suffix(
        path.suffix + ".tmp"
    )

    tmp.write_text(
        json.dumps(
            data,
            indent=4,
        )
        + "\n",
        encoding="utf-8",
    )

    os.replace(
        tmp,
        path,
    )


# =========================================================
# Generate unique FLOW_VARIANT
# =========================================================

def generate_flow_variant(prefix="run"):
    """
    Generate a unique ORFS FLOW_VARIANT.

    Example:
        run_20260906_021530_a83f21
    """

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    unique_id = uuid.uuid4().hex[:6]

    return f"{prefix}_{timestamp}_{unique_id}"


# =========================================================
# Validate required files
# =========================================================

def validate_inputs(design, platform):
    """
    Check that the design configuration and report script exist.
    """

    config = (
        FLOW_DIR
        / "designs"
        / platform
        / design
        / "config.mk"
    )

    if not config.exists():
        raise FileNotFoundError(
            "\nDesign configuration not found:\n"
            f"  {config}\n\n"
            "Expected directory structure:\n"
            f"  designs/{platform}/{design}/config.mk"
        )

    if not REPORT_CHECKS_TCL.exists():
        raise FileNotFoundError(
            "\nreport_checks.tcl not found:\n"
            f"  {REPORT_CHECKS_TCL}"
        )

    return config


# =========================================================
# Run ORFS
# =========================================================

def run_flow(
    design="timing_demo",
    platform="nangate45",
    prefix="run",
):
    """
    Run OpenROAD Flow Scripts using a unique FLOW_VARIANT.

    The run manifest is written before ORFS starts and contains a SHA-256
    snapshot of the standard ORFS design inputs. After ORFS finishes, those
    inputs are hashed again. Any added/removed/modified file fails the run.

    Returns:
        Dictionary containing verified run metadata.
    """

    # -----------------------------------------------------
    # Validate design files
    # -----------------------------------------------------

    config = validate_inputs(
        design,
        platform,
    ).resolve()

    # -----------------------------------------------------
    # Generate unique run name
    # -----------------------------------------------------

    flow_variant = generate_flow_variant(
        prefix
    )

    report_dir = (
        FLOW_DIR
        / "reports"
        / platform
        / design
        / flow_variant
    ).resolve()

    result_dir = (
        FLOW_DIR
        / "results"
        / platform
        / design
        / flow_variant
    ).resolve()

    log_dir = (
        FLOW_DIR
        / "logs"
        / platform
        / design
        / flow_variant
    ).resolve()

    object_dir = (
        FLOW_DIR
        / "objects"
        / platform
        / design
        / flow_variant
    ).resolve()

    AI_RUNS_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    metadata_file = (
        AI_RUNS_DIR
        / f"{flow_variant}.json"
    )

    # -----------------------------------------------------
    # Capture exact pre-flow provenance
    # -----------------------------------------------------

    pre_flow_snapshot = (
        capture_flow_input_snapshot(
            design,
            platform,
        )
    )

    run_info = {
        "schema_version": 2,
        "design": design,
        "platform": platform,
        "flow_variant": flow_variant,
        "status": "running",
        "started_at": utc_now(),
        "completed_at": None,
        "config": str(
            config
        ),
        "report_checks_tcl": str(
            REPORT_CHECKS_TCL.resolve()
        ),
        "report_dir": str(
            report_dir
        ),
        "result_dir": str(
            result_dir
        ),
        "log_dir": str(
            log_dir
        ),
        "object_dir": str(
            object_dir
        ),
        "flow_input_integrity": {
            "status": "captured",
            "pre_flow": pre_flow_snapshot,
            "post_flow": None,
            "comparison": None,
        },
    }

    write_run_metadata_atomic(
        metadata_file,
        run_info,
    )

    print()
    print("========================================")
    print("Starting ORFS flow")
    print("========================================")
    print(f"Design       : {design}")
    print(f"Platform     : {platform}")
    print(f"Flow Variant : {flow_variant}")
    print(f"Config       : {config}")
    print(
        "Input SHA    : "
        f"{pre_flow_snapshot['aggregate_sha256']}"
    )
    print(
        "Input files  : "
        f"{pre_flow_snapshot['file_count']}"
    )
    print("========================================")
    print()

    # -----------------------------------------------------
    # ORFS command
    # -----------------------------------------------------

    command = [
        "make",
        f"DESIGN_CONFIG={config}",
        f"FLOW_VARIANT={flow_variant}",
        (
            "POST_FINAL_REPORT_TCL="
            f"{REPORT_CHECKS_TCL.resolve()}"
        ),
    ]

    print("Running command:")
    print(
        " ".join(
            str(x)
            for x in command
        )
    )
    print()

    # -----------------------------------------------------
    # Run from ORFS flow directory
    # -----------------------------------------------------

    result = subprocess.run(
        command,
        cwd=FLOW_DIR,
        text=True,
    )

    if result.returncode != 0:
        run_info[
            "status"
        ] = "failed"

        run_info[
            "completed_at"
        ] = utc_now()

        run_info[
            "failure_reason"
        ] = (
            "ORFS flow returned "
            f"{result.returncode}"
        )

        write_run_metadata_atomic(
            metadata_file,
            run_info,
        )

        raise RuntimeError(
            "\nORFS flow failed.\n"
            f"Return code : {result.returncode}\n"
            f"Design      : {design}\n"
            f"Platform    : {platform}\n"
            f"Variant     : {flow_variant}"
        )

    # -----------------------------------------------------
    # Verify design inputs did not change during ORFS
    # -----------------------------------------------------

    post_flow_snapshot = (
        capture_flow_input_snapshot(
            design,
            platform,
        )
    )

    comparison = (
        compare_flow_input_snapshots(
            pre_flow_snapshot,
            post_flow_snapshot,
        )
    )

    run_info[
        "flow_input_integrity"
    ] = {
        "status": (
            "verified"
            if comparison[
                "matches"
            ]
            else "mismatch"
        ),
        "pre_flow": pre_flow_snapshot,
        "post_flow": post_flow_snapshot,
        "comparison": comparison,
    }

    if not comparison[
        "matches"
    ]:
        run_info[
            "status"
        ] = "failed"

        run_info[
            "completed_at"
        ] = utc_now()

        run_info[
            "failure_reason"
        ] = (
            "Flow inputs changed during ORFS"
        )

        write_run_metadata_atomic(
            metadata_file,
            run_info,
        )

        raise RuntimeError(
            format_flow_input_mismatch(
                comparison
            )
        )

    run_info[
        "status"
    ] = "completed"

    run_info[
        "completed_at"
    ] = utc_now()

    write_run_metadata_atomic(
        metadata_file,
        run_info,
    )

    # -----------------------------------------------------
    # Final summary
    # -----------------------------------------------------

    print()
    print("========================================")
    print("ORFS flow completed")
    print("========================================")
    print(f"Design       : {design}")
    print(f"Platform     : {platform}")
    print(f"Flow Variant : {flow_variant}")
    print()
    print(
        "Flow inputs  : VERIFIED"
    )
    print(
        "Input SHA    : "
        f"{pre_flow_snapshot['aggregate_sha256']}"
    )
    print()
    print(f"Reports      : {report_dir}")
    print(f"Results      : {result_dir}")
    print(f"Logs         : {log_dir}")
    print(f"Objects      : {object_dir}")
    print()
    print(f"Run metadata : {metadata_file}")
    print("========================================")
    print()

    return run_info


# =========================================================
# Command-line interface
# =========================================================

def parse_arguments():

    parser = argparse.ArgumentParser(
        description="Run ORFS with a unique FLOW_VARIANT."
    )

    parser.add_argument(
        "--design",
        default="timing_demo",
        help="ORFS design name (default: timing_demo)",
    )

    parser.add_argument(
        "--platform",
        default="nangate45",
        help="ORFS platform name (default: nangate45)",
    )

    parser.add_argument(
        "--prefix",
        default="run",
        help="FLOW_VARIANT prefix (default: run)",
    )

    return parser.parse_args()


# =========================================================
# Entry point
# =========================================================

if __name__ == "__main__":

    args = parse_arguments()

    run_flow(
        design=args.design,
        platform=args.platform,
        prefix=args.prefix,
    )