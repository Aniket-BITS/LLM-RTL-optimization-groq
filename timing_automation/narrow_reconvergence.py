#!/usr/bin/env python3
"""
Resolve genuine RTL reconvergence using physical STA witness paths.

Example:

RTL:

    add_path = a_reg + b_reg
    xor_path = a_reg ^ c_reg
    result   = sel_reg ? add_path : xor_path

For:

    a_reg[27] -> y[27]

both RTL branches are logically possible:

    a -> add -> mux -> y
    a -> xor -> mux -> y

But:

    b_reg[27] -> y[27]

can only originate from the ADD branch, while:

    c_reg[27] -> y[27]

can only originate from the XOR branch.

This script compares the FINAL PHYSICAL MUX INSTANCE + INPUT PIN
used by the ambiguous path with those witness paths.

If ambiguous path and ADD witness enter the same physical mux input:
    -> ADD branch selected

If ambiguous path and XOR witness enter the same physical mux input:
    -> XOR branch selected

If physical evidence does not distinguish them:
    -> unresolved

This script deliberately DOES NOT infer branch identity from cell names
such as HA_X1/XNOR2_X1.  It uses design-derived witness paths.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_FLOW_DIR = SCRIPT_DIR.parent

DFF_SUFFIX_RE = re.compile(r"\$_DFF_[A-Za-z0-9_]+_$")
PIN_SUFFIX_RE = re.compile(r"/(?:Q|QN|D|CK)$")


# ============================================================================
# Basic helpers
# ============================================================================

def normalize_signal(value: Any) -> Optional[str]:
    """
    Normalize names such as:

        a_reg[27]$_DFF_P_/Q
        a_reg[27]$_DFF_P_
        a_reg[27]

    into:

        a_reg[27]
    """

    if value is None:
        return None

    text = str(value).strip()

    if text.startswith("\\"):
        text = text[1:]

    text = PIN_SUFFIX_RE.sub("", text)
    text = DFF_SUFFIX_RE.sub("", text)

    return text or None


def resolve_file(
    value: str,
    flow_dir: Path,
) -> Path:
    """
    Resolve a CLI path.

    Search order:
      1. absolute path
      2. current working directory
      3. flow directory
    """

    path = Path(value).expanduser()

    if path.is_absolute():
        candidate = path
    else:
        cwd_candidate = Path.cwd() / path

        if cwd_candidate.exists():
            candidate = cwd_candidate
        else:
            candidate = flow_dir / path

    candidate = candidate.resolve()

    if not candidate.exists():
        raise FileNotFoundError(
            f"File not found:\n  {candidate}"
        )

    return candidate


def load_json(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(
            path.read_text()
        )
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Invalid JSON file:\n  {path}"
        ) from exc

    if not isinstance(data, dict):
        raise ValueError(
            f"Expected top-level JSON object:\n  {path}"
        )

    return data


# ============================================================================
# Timing path selection
# ============================================================================

def path_slack(
    path: Dict[str, Any],
) -> float:
    """
    Sort helper. Lower slack = worse path.
    """

    value = path.get("slack_ns")

    try:
        return float(value)
    except (TypeError, ValueError):
        return float("inf")


def select_timing_path(
    timing_data: Dict[str, Any],
    source_file: Path,
) -> Dict[str, Any]:
    """
    Targeted reports should normally contain exactly one path.

    If several are present, use the worst-slack path instead of silently
    depending on JSON ordering.
    """

    paths = timing_data.get("paths")

    if not isinstance(paths, list) or not paths:
        raise ValueError(
            f"No timing paths found in:\n  {source_file}"
        )

    if len(paths) == 1:
        return paths[0]

    return min(
        paths,
        key=path_slack,
    )


# ============================================================================
# Physical STA cell helpers
# ============================================================================

def cell_type(
    cell: Dict[str, Any],
) -> str:
    """
    Support current parse_timing.py plus older naming.
    """

    return str(
        cell.get("type")
        or cell.get("cell")
        or ""
    ).strip()


def is_mux_cell(
    cell: Dict[str, Any],
) -> bool:
    """
    Detect explicit physical mux cells.

    Nangate45 examples:
        MUX2_X1
        MUX2_X2
    """

    typ = cell_type(cell).upper()

    return "MUX" in typ


def serialize_physical_stage(
    cell: Dict[str, Any],
    index: int,
) -> Dict[str, Any]:

    return {
        "stage_index": index,
        "instance": cell.get("instance"),
        "type": cell_type(cell),
        "input_pin": cell.get("input_pin"),
        "output_pin": cell.get("output_pin"),
        "input_net": cell.get("input_net"),
        "output_net": cell.get("output_net"),
        "delay_ns": (
            cell.get("delay_ns")
            if cell.get("delay_ns") is not None
            else cell.get("cell_delay_ns")
        ),
        "arrival_ns": (
            cell.get("arrival_ns")
            if cell.get("arrival_ns") is not None
            else cell.get("output_arrival_ns")
        ),
    }


def find_final_mux(
    path: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Starting from the endpoint, find the final explicit physical mux.

    We intentionally search backwards because the branch reconvergence mux
    should be the last mux before the capture register.
    """

    cells = path.get("cells") or []

    for index in range(
        len(cells) - 1,
        -1,
        -1,
    ):
        cell = cells[index]

        if is_mux_cell(cell):
            return {
                "status": "found",
                **serialize_physical_stage(
                    cell,
                    index,
                ),
            }

    return {
        "status": "not_found",
        "reason": (
            "No explicit physical MUX cell was found in the STA path. "
            "The mux may have been transformed into complex gates."
        ),
    }


# ============================================================================
# RTL ambiguity information
# ============================================================================

def find_matching_rtl_mapping(
    rtl_data: Dict[str, Any],
    timing_path: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """
    Find the RTL mapping corresponding to the ambiguous timing path.
    """

    paths = rtl_data.get("paths")

    if not isinstance(paths, list):
        return None

    wanted_start = normalize_signal(
        timing_path.get("startpoint")
        or timing_path.get("startpoint_raw")
    )

    wanted_end = normalize_signal(
        timing_path.get("endpoint")
        or timing_path.get("endpoint_raw")
    )

    matches = []

    for path in paths:

        start = normalize_signal(
            path.get("startpoint")
            or path.get("startpoint_raw")
        )

        end = normalize_signal(
            path.get("endpoint")
            or path.get("endpoint_raw")
        )

        if (
            start == wanted_start
            and end == wanted_end
        ):
            matches.append(path)

    if len(matches) == 1:
        return matches[0].get(
            "rtl_mapping"
        )

    if len(matches) > 1:
        worst = min(
            matches,
            key=path_slack,
        )

        return worst.get(
            "rtl_mapping"
        )

    if len(paths) == 1:
        return paths[0].get(
            "rtl_mapping"
        )

    return None


# ============================================================================
# Witness parsing
# ============================================================================

def parse_witness_argument(
    raw: str,
) -> Tuple[str, str]:
    """
    CLI form:

        --witness add=path/to/file.json
        --witness xor=path/to/file.json
    """

    if "=" not in raw:
        raise argparse.ArgumentTypeError(
            "--witness must use NAME=JSON_FILE format.\n"
            "Example:\n"
            "  --witness add=ai_runs/run_x_b27_to_y27_timing_paths.json"
        )

    name, filename = raw.split(
        "=",
        1,
    )

    name = name.strip()
    filename = filename.strip()

    if not name:
        raise argparse.ArgumentTypeError(
            "Witness branch name cannot be empty."
        )

    if not filename:
        raise argparse.ArgumentTypeError(
            "Witness JSON path cannot be empty."
        )

    return name, filename


def load_witness(
    branch: str,
    filename: str,
    flow_dir: Path,
) -> Dict[str, Any]:

    path = resolve_file(
        filename,
        flow_dir,
    )

    data = load_json(
        path
    )

    timing_path = select_timing_path(
        data,
        path,
    )

    mux = find_final_mux(
        timing_path
    )

    return {
        "branch": branch,
        "source_timing_json": str(path),
        "startpoint": normalize_signal(
            timing_path.get("startpoint")
            or timing_path.get("startpoint_raw")
        ),
        "endpoint": normalize_signal(
            timing_path.get("endpoint")
            or timing_path.get("endpoint_raw")
        ),
        "slack_ns": timing_path.get(
            "slack_ns"
        ),
        "final_mux": mux,
    }



def _find_matching_path_index(rtl_data: Dict[str, Any], timing_path: Dict[str, Any]) -> Optional[int]:
    wanted_start = normalize_signal(timing_path.get("startpoint") or timing_path.get("startpoint_raw"))
    wanted_end = normalize_signal(timing_path.get("endpoint") or timing_path.get("endpoint_raw"))
    matches: List[Tuple[int, float]] = []
    for idx, path in enumerate(rtl_data.get("paths") or []):
        start = normalize_signal(path.get("startpoint") or path.get("startpoint_raw"))
        end = normalize_signal(path.get("endpoint") or path.get("endpoint_raw"))
        if start == wanted_start and end == wanted_end:
            matches.append((idx, path_slack(path)))
    if not matches:
        return 0 if len(rtl_data.get("paths") or []) == 1 else None
    return min(matches, key=lambda item: item[1])[0]


def load_witness_directory(
    witness_dir: Path,
    rtl_data: Dict[str, Any],
    ambiguous_path: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Load dynamically generated witness timing JSONs by witness_id."""
    witness_dir = Path(witness_dir).expanduser().resolve()
    if not witness_dir.is_dir():
        raise FileNotFoundError(f"Witness directory not found: {witness_dir}")

    request_files = sorted(witness_dir.glob("*_witness_request.json"))
    if not request_files:
        direct = witness_dir / "witness_request.json"
        if direct.is_file():
            request_files = [direct]
    if not request_files:
        raise FileNotFoundError(
            f"No *_witness_request.json or witness_request.json found in {witness_dir}"
        )

    path_index = _find_matching_path_index(rtl_data, ambiguous_path)
    if path_index is None:
        raise RuntimeError("Could not identify ambiguous path index for witness directory")

    requests: List[Dict[str, Any]] = []
    for request_file in request_files:
        doc = load_json(request_file)
        if doc.get("schema") != "nebula_witness_request/1":
            continue
        for req in doc.get("requests") or []:
            if req.get("path_index") == path_index:
                requests.append(dict(req))

    witnesses: List[Dict[str, Any]] = []
    unavailable: List[Dict[str, Any]] = []
    for req in requests:
        witness_id = str(req.get("witness_id") or "")
        if req.get("derivation_status") != "ready" or not witness_id:
            unavailable.append({
                "witness_id": witness_id or None,
                "branch": req.get("branch"),
                "reason": req.get("reason") or "witness_derivation_unavailable",
            })
            continue
        timing_file = witness_dir / f"witness_{witness_id}_timing.json"
        if not timing_file.is_file():
            timing_file = witness_dir / f"witness_{witness_id}.timing.json"
        if not timing_file.is_file():
            unavailable.append({
                "witness_id": witness_id,
                "branch": req.get("branch"),
                "reason": "witness_timing_json_missing",
            })
            continue
        try:
            data = load_json(timing_file)
            timing_path = select_timing_path(data, timing_file)
        except Exception as exc:
            unavailable.append({
                "witness_id": witness_id,
                "branch": req.get("branch"),
                "reason": f"witness_timing_json_invalid:{type(exc).__name__}",
            })
            continue
        witnesses.append({
            "witness_id": witness_id,
            "branch": req.get("branch"),
            "operator": req.get("operator"),
            "source_timing_json": str(timing_file),
            "startpoint": normalize_signal(timing_path.get("startpoint") or timing_path.get("startpoint_raw")),
            "endpoint": normalize_signal(timing_path.get("endpoint") or timing_path.get("endpoint_raw")),
            "slack_ns": timing_path.get("slack_ns"),
            "final_mux": find_final_mux(timing_path),
            "cells": timing_path.get("cells", []),
        })
    return witnesses, unavailable


def run_self_test() -> None:
    import tempfile
    with tempfile.TemporaryDirectory(prefix="nebula_narrow_witness_") as tmp:
        root = Path(tmp)
        rtl = {
            "paths": [{
                "startpoint": "a_reg[0]/Q",
                "endpoint": "y_reg[0]/D",
                "slack_ns": -0.1,
                "rtl_mapping": {"remaining_logical_reconvergence": True},
            }]
        }
        request = {
            "schema": "nebula_witness_request/1",
            "requests": [
                {"witness_id": "w0000", "path_index": 0, "branch": "mux_a", "operator": "$xor", "derivation_status": "ready"},
                {"witness_id": "w0001", "path_index": 0, "branch": "mux_b", "operator": "$add", "derivation_status": "ready"},
            ],
        }
        (root / "demo_witness_request.json").write_text(json.dumps(request), encoding="utf-8")
        for wid, pin in (("w0000", "A1"), ("w0001", "A2")):
            payload = {"paths": [{
                "startpoint": "src/Q", "endpoint": "y_reg[0]/D", "slack_ns": -0.2,
                "cells": [{"instance": "u_mux", "type": "MUX2_X1", "input_pin": pin, "output_pin": "Z"}],
            }]}
            (root / f"witness_{wid}_timing.json").write_text(json.dumps(payload), encoding="utf-8")
        ambiguous = {
            "startpoint": "a_reg[0]/Q", "endpoint": "y_reg[0]/D", "slack_ns": -0.1,
            "cells": [{"instance": "u_mux", "type": "MUX2_X1", "input_pin": "A2", "output_pin": "Z"}],
        }
        witnesses, unavailable = load_witness_directory(root, rtl, ambiguous)
        assert len(witnesses) == 2 and not unavailable
        physical = resolve_branch(find_final_mux(ambiguous), witnesses, normalize_signal(ambiguous["endpoint"]))
        assert physical.get("status") == "resolved"
        assert physical.get("selected_branch") == "mux_b"

        factored_ambiguous = {
            "startpoint": "shared/Q",
            "endpoint": "y_reg[0]/D",
            "cells": [
                {
                    "instance": "u_shared_entry",
                    "type": "XOR2_X1",
                    "input_pin": "A",
                    "output_pin": "Z",
                },
                {
                    "instance": "u_common1",
                    "type": "AOI21_X1",
                    "input_pin": "A",
                    "output_pin": "ZN",
                },
                {
                    "instance": "u_common2",
                    "type": "OAI21_X1",
                    "input_pin": "B1",
                    "output_pin": "ZN",
                },
                {
                    "instance": "u_common3",
                    "type": "BUF_X1",
                    "input_pin": "A",
                    "output_pin": "Z",
                },
            ],
        }

        factored_witnesses = []

        for branch, pin in (
            ("mux_a", "A"),
            ("mux_b", "B"),
        ):
            factored_witnesses.append({
                "witness_id":
                    f"factored_{branch}",
                "branch":
                    branch,
                "endpoint":
                    "y_reg[0]/D",
                "final_mux": {
                    "status": "found",
                    "instance": "u_branch_mux",
                    "type": "MUX2_X1",
                    "input_pin": pin,
                    "output_pin": "Z",
                    "output_net": None,
                },
                "cells": [
                    {
                        "instance": "u_branch_mux",
                        "type": "MUX2_X1",
                        "input_pin": pin,
                        "output_pin": "Z",
                    },
                    {
                        "instance": "u_common1",
                        "type": "AOI21_X1",
                        "input_pin": "A",
                        "output_pin": "ZN",
                    },
                    {
                        "instance": "u_common2",
                        "type": "OAI21_X1",
                        "input_pin": "B1",
                        "output_pin": "ZN",
                    },
                    {
                        "instance": "u_common3",
                        "type": "BUF_X1",
                        "input_pin": "A",
                        "output_pin": "Z",
                    },
                ],
            })

        factored = resolve_branch(
            {"status": "not_found"},
            factored_witnesses,
            "y_reg[0]/D",
            factored_ambiguous,
        )

        assert factored.get("status") == "resolved"
        assert (
            factored.get("reason")
            == "physically_factored_shared_path"
        )
        assert factored.get("selected_branch") is None
        assert (
            factored.get("branch_selection_required")
            is False
        )
    print("PASS: narrow_reconvergence witness-directory self-test")


# ============================================================================
# Physical comparison
# ============================================================================

def same_mux_instance(
    first: Dict[str, Any],
    second: Dict[str, Any],
) -> bool:

    return (
        first.get("status") == "found"
        and second.get("status") == "found"
        and first.get("instance") is not None
        and first.get("instance")
        == second.get("instance")
    )


def same_mux_output(
    first: Dict[str, Any],
    second: Dict[str, Any],
) -> bool:
    """
    Output-net comparison is corroborating evidence.

    Instance equality is primary because the same physical mux must be used.
    """

    first_net = first.get(
        "output_net"
    )

    second_net = second.get(
        "output_net"
    )

    if (
        first_net is None
        or second_net is None
    ):
        return True

    return first_net == second_net


def same_input_pin(
    first: Dict[str, Any],
    second: Dict[str, Any],
) -> bool:

    return (
        first.get("input_pin") is not None
        and first.get("input_pin")
        == second.get("input_pin")
    )


def _physical_cell_signature(
    cell: Dict[str, Any],
) -> Tuple[str, str, str]:
    """
    Identity of a physical path after convergence.

    The input pin is intentionally NOT part of this signature.

    Different logical paths may enter the same physical gate through
    different inputs.  Once they leave the same physical instance
    through the same output pin, they have converged onto the same
    downstream physical cone.

    Therefore physical convergence is identified by:

        instance
        cell type
        output pin
    """

    return (
        str(cell.get("instance") or ""),
        str(
            cell.get("type")
            or cell.get("cell_type")
            or ""
        ),
        str(cell.get("output_pin") or ""),
    )


def _physical_cells(
    path: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    if not isinstance(path, dict):
        return []

    result = []

    for cell in path.get("cells") or []:
        if (
            isinstance(cell, dict)
            and cell.get("instance")
        ):
            result.append(cell)

    return result


def _common_physical_suffix(
    first: List[Dict[str, Any]],
    second: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Return the exact common suffix of two physical timing paths.

    Comparison is intentionally strict: same instance, type,
    input pin and output pin.
    """

    result = []

    i = len(first) - 1
    j = len(second) - 1

    while i >= 0 and j >= 0:
        if (
            _physical_cell_signature(first[i])
            != _physical_cell_signature(second[j])
        ):
            break

        result.append(first[i])

        i -= 1
        j -= 1

    result.reverse()
    return result


def _find_instance_index(
    cells: List[Dict[str, Any]],
    instance: str,
) -> Optional[int]:
    for index, cell in enumerate(cells):
        if str(cell.get("instance")) == instance:
            return index

    return None


def _resolve_factored_shared_path(
    witnesses: List[Dict[str, Any]],
    ambiguous_endpoint: Optional[str],
    ambiguous_path: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """
    Detect a synthesis-factored/shared physical implementation.

    This is NOT branch selection.

    We only accept the result when:

      1. two different logical branch witnesses terminate at the same
         capture endpoint;
      2. both traverse the same physical mux instance;
      3. they use different data inputs of that mux;
      4. after that mux they share a substantial exact physical suffix;
      5. the ambiguous critical path does not traverse that mux; and
      6. the ambiguous path joins that same exact downstream suffix.

    When all conditions hold, synthesis has removed the requirement to
    assign the shared critical path to one RTL branch.  The path is
    therefore resolved as physically factored, with no selected branch.
    """

    ambiguous_cells = _physical_cells(
        ambiguous_path
    )

    if not ambiguous_cells:
        return None

    candidates = []

    for first_index in range(len(witnesses)):
        first = witnesses[first_index]

        for second_index in range(
            first_index + 1,
            len(witnesses),
        ):
            second = witnesses[second_index]

            if (
                first.get("branch")
                == second.get("branch")
            ):
                continue

            if (
                ambiguous_endpoint
                and (
                    first.get("endpoint")
                    != ambiguous_endpoint
                    or second.get("endpoint")
                    != ambiguous_endpoint
                )
            ):
                continue

            first_mux = (
                first.get("final_mux")
                or {}
            )
            second_mux = (
                second.get("final_mux")
                or {}
            )

            if not same_mux_instance(
                first_mux,
                second_mux,
            ):
                continue

            if not same_mux_output(
                first_mux,
                second_mux,
            ):
                continue

            first_pin = first_mux.get(
                "input_pin"
            )
            second_pin = second_mux.get(
                "input_pin"
            )

            if (
                first_pin is None
                or second_pin is None
                or first_pin == second_pin
            ):
                continue

            mux_instance = str(
                first_mux.get("instance")
            )

            # The ambiguous path must NOT itself traverse the branch mux.
            if any(
                str(cell.get("instance"))
                == mux_instance
                for cell in ambiguous_cells
            ):
                continue

            first_cells = _physical_cells(
                first
            )
            second_cells = _physical_cells(
                second
            )

            first_mux_index = _find_instance_index(
                first_cells,
                mux_instance,
            )
            second_mux_index = _find_instance_index(
                second_cells,
                mux_instance,
            )

            if (
                first_mux_index is None
                or second_mux_index is None
            ):
                continue

            first_after_mux = first_cells[
                first_mux_index + 1:
            ]
            second_after_mux = second_cells[
                second_mux_index + 1:
            ]

            witness_shared_suffix = (
                _common_physical_suffix(
                    first_after_mux,
                    second_after_mux,
                )
            )

            if not witness_shared_suffix:
                continue

            ambiguous_shared_suffix = (
                _common_physical_suffix(
                    ambiguous_cells,
                    witness_shared_suffix,
                )
            )

            if not ambiguous_shared_suffix:
                continue

            candidates.append({
                "mux_instance": mux_instance,
                "branches": {
                    str(first.get("branch")):
                        str(first_pin),
                    str(second.get("branch")):
                        str(second_pin),
                },
                "witness_ids": [
                    first.get("witness_id"),
                    second.get("witness_id"),
                ],
                "witness_common_suffix_length":
                    len(witness_shared_suffix),
                "ambiguous_shared_suffix_length":
                    len(ambiguous_shared_suffix),
                "shared_suffix_instances": [
                    str(cell.get("instance"))
                    for cell
                    in ambiguous_shared_suffix
                ],
            })

    # Deduplicate equivalent evidence produced by duplicate witnesses.
    unique = {}

    for candidate in candidates:
        key = (
            candidate["mux_instance"],
            tuple(
                sorted(
                    candidate["branches"].items()
                )
            ),
            tuple(
                candidate[
                    "shared_suffix_instances"
                ]
            ),
        )
        unique[key] = candidate

    candidates = list(
        unique.values()
    )

    if len(candidates) != 1:
        return None

    evidence = candidates[0]

    return {
        "status": "resolved",
        "reason": "physically_factored_shared_path",
        "selected_branch": None,
        "branch_selection_required": False,
        "confidence": "high",
        "factored_evidence": evidence,
    }



def _path_cells_for_topology(
    obj: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
    Extract the physical cell sequence from either a timing path directly
    or one of the witness wrapper objects.

    This intentionally does not depend on an explicit physical mux.
    """

    if not isinstance(obj, dict):
        return []

    cells = obj.get("cells")

    if isinstance(cells, list):
        return [
            cell
            for cell in cells
            if isinstance(cell, dict)
        ]

    for key in (
        "timing_path",
        "path",
        "physical_path",
    ):
        nested = obj.get(key)

        if not isinstance(nested, dict):
            continue

        cells = nested.get("cells")

        if isinstance(cells, list):
            return [
                cell
                for cell in cells
                if isinstance(cell, dict)
            ]

    return []


def _topology_stage_signature(
    cell: Dict[str, Any],
) -> Tuple[str, str, str]:
    """
    Identify a physical path stage after the signal has traversed the cell.

    Input pin is deliberately excluded: paths entering different pins of
    the same physical cell have converged once they leave through the same
    output pin.
    """

    return (
        str(cell.get("instance") or ""),
        cell_type(cell),
        str(cell.get("output_pin") or ""),
    )


def _common_topology_suffix_length(
    left: List[Dict[str, Any]],
    right: List[Dict[str, Any]],
) -> int:
    """
    Count identical downstream physical stages.

    No arbitrary minimum suffix length is used.
    """

    i = len(left) - 1
    j = len(right) - 1
    count = 0

    while i >= 0 and j >= 0:

        left_sig = _topology_stage_signature(
            left[i]
        )

        right_sig = _topology_stage_signature(
            right[j]
        )

        # Refuse to prove convergence from incomplete stage identity.
        if (
            not left_sig[0]
            or not left_sig[2]
            or not right_sig[0]
            or not right_sig[2]
        ):
            break

        if left_sig != right_sig:
            break

        count += 1
        i -= 1
        j -= 1

    return count


def _resolve_unique_branch_by_topology(
    witnesses: List[Dict[str, Any]],
    ambiguous_endpoint: Optional[str],
    ambiguous_path: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """
    Resolve a branch without requiring an explicit physical mux.

    For witness Wi, let:

        A_i = common downstream suffix(ambiguous path, Wi)

        R_i = longest common downstream suffix(
                  Wi,
                  any witness from another branch
              )

    R_i represents the point after which the branch witnesses have already
    physically converged.

    If A_i > R_i, the ambiguous path joins Wi *before* inter-branch
    convergence.  That is branch-specific physical evidence.

    This is purely topology based:
      - no RTL operator names
      - no standard-cell type assumptions
      - no PDK assumptions
      - no minimum suffix threshold
    """

    if not isinstance(
        ambiguous_path,
        dict,
    ):
        return None

    ambiguous_cells = (
        _path_cells_for_topology(
            ambiguous_path
        )
    )

    if not ambiguous_cells:
        return None

    usable = []

    for witness in witnesses:

        if not isinstance(
            witness,
            dict,
        ):
            continue

        branch = witness.get(
            "branch"
        )

        if branch is None:
            continue

        if (
            ambiguous_endpoint
            and witness.get("endpoint")
            != ambiguous_endpoint
        ):
            continue

        cells = _path_cells_for_topology(
            witness
        )

        if not cells:
            continue

        usable.append(
            {
                "witness": witness,
                "branch": str(branch),
                "cells": cells,
            }
        )

    if len({
        item["branch"]
        for item in usable
    }) < 2:
        return None

    candidates = []

    for index, item in enumerate(
        usable
    ):

        competing_suffixes = []

        for other_index, other in enumerate(
            usable
        ):

            if other_index == index:
                continue

            if (
                other["branch"]
                == item["branch"]
            ):
                continue

            competing_suffixes.append(
                _common_topology_suffix_length(
                    item["cells"],
                    other["cells"],
                )
            )

        if not competing_suffixes:
            continue

        branch_convergence_suffix = max(
            competing_suffixes
        )

        ambiguous_suffix = (
            _common_topology_suffix_length(
                ambiguous_cells,
                item["cells"],
            )
        )

        # Strict inequality is the proof:
        #
        # the ambiguous path is already following this witness before
        # different branch witnesses have physically reconverged.
        if (
            ambiguous_suffix
            > branch_convergence_suffix
        ):
            candidates.append(
                {
                    "branch": item["branch"],
                    "witness": item["witness"],
                    "ambiguous_common_suffix_length":
                        ambiguous_suffix,
                    "branch_convergence_suffix_length":
                        branch_convergence_suffix,
                }
            )

    candidate_branches = {
        candidate["branch"]
        for candidate in candidates
    }

    if len(candidate_branches) != 1:
        return None

    selected_branch = next(
        iter(candidate_branches)
    )

    selected = max(
        (
            candidate
            for candidate in candidates
            if candidate["branch"]
            == selected_branch
        ),
        key=lambda candidate:
            candidate[
                "ambiguous_common_suffix_length"
            ],
    )

    witness = selected[
        "witness"
    ]

    return {
        "status": "resolved",
        "reason":
            "physical_topology_unique_branch_before_convergence",
        "selected_branch":
            selected_branch,
        "branch_selection_required":
            True,
        "confidence":
            "high",
        "topology_evidence": {
            "witness_id":
                witness.get("witness_id"),
            "ambiguous_common_suffix_length":
                selected[
                    "ambiguous_common_suffix_length"
                ],
            "branch_convergence_suffix_length":
                selected[
                    "branch_convergence_suffix_length"
                ],
        },
    }



def resolve_branch(
    ambiguous_mux: Dict[str, Any],
    witnesses: List[Dict[str, Any]],
    ambiguous_endpoint: Optional[str],
    ambiguous_path: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:

    valid_witnesses = []

    for witness in witnesses:

        mux = witness.get(
            "final_mux"
        ) or {}

        if mux.get(
            "status"
        ) != "found":
            continue

        # Witness must terminate at the same capture signal.
        if (
            ambiguous_endpoint
            and witness.get("endpoint")
            != ambiguous_endpoint
        ):
            continue

        valid_witnesses.append(
            witness
        )

    if not valid_witnesses:

        # An explicit physical mux is useful evidence, but it is not a
        # requirement for a valid witness. Synthesis may absorb RTL muxing
        # into complex gates. In that case, try to prove a unique branch
        # directly from physical path convergence topology.
        topology_resolution = (
            _resolve_unique_branch_by_topology(
                witnesses,
                ambiguous_endpoint,
                ambiguous_path,
            )
        )

        if topology_resolution is not None:
            return topology_resolution

        return {
            "status": "unresolved",
            "reason": "no_valid_physical_witnesses",
            "selected_branch": None,
            "confidence": "none",
        }

    # ----------------------------------------------------------------------
    # Ensure branch witnesses themselves provide meaningful separation.
    # ----------------------------------------------------------------------

    pin_to_branches: Dict[
        Tuple[str, str],
        List[str],
    ] = {}

    for witness in valid_witnesses:

        mux = witness["final_mux"]

        key = (
            str(
                mux.get("instance")
            ),
            str(
                mux.get("input_pin")
            ),
        )

        pin_to_branches.setdefault(
            key,
            [],
        ).append(
            witness["branch"]
        )

    witness_collisions = {
        key: branches
        for key, branches
        in pin_to_branches.items()
        if len(branches) > 1
    }

    if witness_collisions:

        return {
            "status": "unresolved",
            "reason": "witness_branch_collision",
            "selected_branch": None,
            "confidence": "none",
            "matches": [],
            "witness_collisions": [
                {
                    "mux_instance": key[0],
                    "input_pin": key[1],
                    "branches": branches,
                }
                for key, branches
                in witness_collisions.items()
            ],
        }

    # ----------------------------------------------------------------------
    # The ambiguous path may no longer contain the RTL branch mux because
    # synthesis factored the common logic.  Prove that case conservatively
    # before declaring it unresolved.
    # ----------------------------------------------------------------------

    if ambiguous_mux.get(
        "status"
    ) != "found":

        factored = (
            _resolve_factored_shared_path(
                valid_witnesses,
                ambiguous_endpoint,
                ambiguous_path,
            )
        )

        if factored is not None:
            return factored

        return {
            "status": "unresolved",
            "reason": "ambiguous_path_has_no_explicit_physical_mux",
            "selected_branch": None,
            "confidence": "none",
        }

    # ----------------------------------------------------------------------
    # Existing strict physical mux-input matching.
    # ----------------------------------------------------------------------

    comparisons = []
    matches = []

    for witness in valid_witnesses:

        witness_mux = witness[
            "final_mux"
        ]

        instance_match = same_mux_instance(
            ambiguous_mux,
            witness_mux,
        )

        output_match = same_mux_output(
            ambiguous_mux,
            witness_mux,
        )

        pin_match = same_input_pin(
            ambiguous_mux,
            witness_mux,
        )

        matched = (
            instance_match
            and output_match
            and pin_match
        )

        comparison = {
            "branch": witness[
                "branch"
            ],
            "same_mux_instance":
                instance_match,
            "same_mux_output":
                output_match,
            "same_mux_input_pin":
                pin_match,
            "matched":
                matched,
            "witness_mux":
                witness_mux,
        }

        comparisons.append(
            comparison
        )

        if matched:
            matches.append(
                witness["branch"]
            )

    if len(matches) == 1:

        return {
            "status": "resolved",
            "reason":
                "physical_mux_input_matches_unique_branch_witness",
            "selected_branch":
                matches[0],
            "branch_selection_required":
                True,
            "confidence": "high",
            "matches": matches,
            "comparisons": comparisons,
        }

    if len(matches) == 0:

        # An unrelated physical mux may be present on the ambiguous
        # critical path while the actual RTL branch mux was factored out.
        # Allow the same strict factored-path proof as a fallback.
        factored = (
            _resolve_factored_shared_path(
                valid_witnesses,
                ambiguous_endpoint,
                ambiguous_path,
            )
        )

        if factored is not None:
            factored["comparisons"] = comparisons
            return factored

        return {
            "status": "unresolved",
            "reason":
                "ambiguous_mux_input_matches_no_witness",
            "selected_branch": None,
            "confidence": "none",
            "matches": [],
            "comparisons": comparisons,
        }

    return {
        "status": "unresolved",
        "reason":
            "ambiguous_mux_input_matches_multiple_witnesses",
        "selected_branch": None,
        "confidence": "none",
        "matches": matches,
        "comparisons": comparisons,
    }


# ============================================================================
# Main
# ============================================================================

def main() -> None:

    parser = argparse.ArgumentParser(
        description=(
            "Resolve genuine RTL reconvergence using physical STA "
            "branch-witness paths."
        )
    )

    parser.add_argument(
        "--variant",
        required=False,
        help="ORFS FLOW_VARIANT",
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
        default=str(
            DEFAULT_FLOW_DIR
        ),
    )

    parser.add_argument(
        "--ambiguous-timing-json",
        required=False,
        help=(
            "Parsed timing JSON for the genuinely ambiguous path."
        ),
    )

    parser.add_argument(
        "--ambiguous-rtl-json",
        required=False,
        help=(
            "map_to_rtl.py output for the genuinely ambiguous path."
        ),
    )

    parser.add_argument(
        "--witness",
        action="append",
        default=[],
        help=(
            "Branch witness in NAME=TIMING_JSON format. "
            "May be repeated. Legacy/manual mode."
        ),
    )

    parser.add_argument(
        "--witness-dir",
        default=None,
        help=(
            "Directory containing nebula_witness_request/1 plus "
            "witness_<id>_timing.json files."
        ),
    )

    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run synthetic dynamic-witness tests.",
    )

    parser.add_argument(
        "--output",
        default=None,
    )

    args = parser.parse_args()

    if args.self_test:
        run_self_test()
        return

    missing = [name for name, value in (("--variant", args.variant), ("--ambiguous-timing-json", args.ambiguous_timing_json), ("--ambiguous-rtl-json", args.ambiguous_rtl_json)) if not value]
    if missing:
        parser.error("missing required argument(s): " + ", ".join(missing))
    if not args.witness and not args.witness_dir:
        parser.error("supply at least one --witness or --witness-dir")

    flow_dir = Path(
        args.flow_dir
    ).resolve()

    # ----------------------------------------------------------------------
    # Load ambiguous physical timing path.
    # ----------------------------------------------------------------------

    ambiguous_timing_file = resolve_file(
        args.ambiguous_timing_json,
        flow_dir,
    )

    ambiguous_timing_data = load_json(
        ambiguous_timing_file
    )

    ambiguous_path = select_timing_path(
        ambiguous_timing_data,
        ambiguous_timing_file,
    )

    ambiguous_start = normalize_signal(
        ambiguous_path.get("startpoint")
        or ambiguous_path.get("startpoint_raw")
    )

    ambiguous_endpoint = normalize_signal(
        ambiguous_path.get("endpoint")
        or ambiguous_path.get("endpoint_raw")
    )

    ambiguous_mux = find_final_mux(
        ambiguous_path
    )

    # ----------------------------------------------------------------------
    # Load RTL ambiguity result.
    # ----------------------------------------------------------------------

    rtl_file = resolve_file(
        args.ambiguous_rtl_json,
        flow_dir,
    )

    rtl_data = load_json(
        rtl_file
    )

    rtl_mapping = find_matching_rtl_mapping(
        rtl_data,
        ambiguous_path,
    )

    if rtl_mapping is None:
        raise RuntimeError(
            "Could not find corresponding rtl_mapping in:\n"
            f"  {rtl_file}"
        )

    logical_ambiguity = bool(
        rtl_mapping.get(
            "remaining_logical_reconvergence"
        )
    )

    if not logical_ambiguity:
        raise RuntimeError(
            "The supplied RTL mapping is not marked as genuinely "
            "reconvergent.\n"
            "narrow_reconvergence.py should only be used after "
            "map_to_rtl.py reports:\n"
            "  remaining_logical_reconvergence = true"
        )

    # ----------------------------------------------------------------------
    # Load physical branch witnesses.
    # ----------------------------------------------------------------------

    unavailable_witnesses: List[Dict[str, Any]] = []
    if args.witness_dir:
        witnesses, unavailable_witnesses = load_witness_directory(
            Path(args.witness_dir),
            rtl_data,
            ambiguous_path,
        )
    else:
        witness_specs = [parse_witness_argument(raw) for raw in args.witness]
        witness_names = [name for name, _ in witness_specs]
        if len(set(witness_names)) != len(witness_names):
            raise ValueError("Witness branch names must be unique.")
        witnesses = [
            load_witness(branch, filename, flow_dir)
            for branch, filename in witness_specs
        ]

    # ----------------------------------------------------------------------
    # Resolve physical branch.
    # ----------------------------------------------------------------------

    physical_resolution = resolve_branch(
        ambiguous_mux,
        witnesses,
        ambiguous_endpoint,
        ambiguous_path,
    )
    if unavailable_witnesses and physical_resolution.get("status") != "resolved":
        physical_resolution = dict(physical_resolution)
        physical_resolution["reason"] = "witness_unavailable"
        physical_resolution["unavailable_witnesses"] = unavailable_witnesses

    selected_branch = physical_resolution.get(
        "selected_branch"
    )

    # Optional convenience field for our current experiment.
    selected_operator = None

    common_branch_operators = {
        "add": "$add",
        "xor": "$xor",
        "mux": "$mux",
    }

    if selected_branch:
        for witness in witnesses:
            if witness.get("branch") == selected_branch and witness.get("operator"):
                selected_operator = witness.get("operator")
                break
        if selected_operator is None:
            selected_operator = common_branch_operators.get(selected_branch.lower())

    output_data = {
        "schema_version": 1,

        "analysis": (
            "Physical witness-assisted narrowing of a genuine "
            "pre-ABC RTL reconvergence."
        ),

        "design": args.design,
        "platform": args.platform,
        "flow_variant": args.variant,

        "source_ambiguous_timing_json": str(
            ambiguous_timing_file
        ),

        "source_ambiguous_rtl_json": str(
            rtl_file
        ),

        "ambiguous_path": {
            "startpoint": ambiguous_start,
            "endpoint": ambiguous_endpoint,
            "slack_ns": ambiguous_path.get(
                "slack_ns"
            ),

            "logical_operator_candidates": rtl_mapping.get(
                "operator_counts",
                {},
            ),

            "logical_branch_points": rtl_mapping.get(
                "logical_branch_points",
                [],
            ),

            "remaining_logical_reconvergence": (
                logical_ambiguity
            ),

            "final_physical_mux": ambiguous_mux,
        },

        "witnesses": witnesses,
        "unavailable_witnesses": unavailable_witnesses,

        "physical_resolution": physical_resolution,

        "selected_rtl_branch": selected_branch,

        "selected_rtl_operator": selected_operator,

        "resolved": (
            physical_resolution.get(
                "status"
            )
            == "resolved"
        ),
    }

    output_file = (
        Path(
            args.output
        ).resolve()

        if args.output

        else (
            flow_dir
            / "ai_runs"
            / f"{args.variant}_reconvergence_narrowed.json"
        ).resolve()
    )

    output_file.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    output_file.write_text(
        json.dumps(
            output_data,
            indent=2,
        )
    )

    # ----------------------------------------------------------------------
    # Console report
    # ----------------------------------------------------------------------

    print()
    print(
        "=" * 68
    )
    print(
        "Physical Reconvergence Narrowing"
    )
    print(
        "=" * 68
    )

    print(
        f"Design              : {args.design}"
    )

    print(
        f"Flow Variant        : {args.variant}"
    )

    print(
        f"Ambiguous path      : "
        f"{ambiguous_start} -> {ambiguous_endpoint}"
    )

    print(
        "Logical operators   : "
        f"{rtl_mapping.get('operator_counts', {})}"
    )

    print(
        "Logical ambiguity   : "
        f"{logical_ambiguity}"
    )

    print()

    if ambiguous_mux.get(
        "status"
    ) == "found":

        print(
            "Ambiguous physical mux"
        )

        print(
            "-" * 68
        )

        print(
            f"Instance : "
            f"{ambiguous_mux.get('instance')}"
        )

        print(
            f"Type     : "
            f"{ambiguous_mux.get('type')}"
        )

        print(
            f"Input pin: "
            f"{ambiguous_mux.get('input_pin')}"
        )

        print(
            f"Output   : "
            f"{ambiguous_mux.get('output_net')}"
        )

    else:

        print(
            "Ambiguous physical mux: NOT FOUND"
        )

    print()
    print(
        "Witnesses"
    )
    print(
        "-" * 68
    )

    for witness in witnesses:

        mux = witness.get(
            "final_mux",
            {},
        )

        print(
            f"{witness['branch']:>8} : "
            f"{witness.get('startpoint')} "
            f"-> {witness.get('endpoint')}"
        )

        if mux.get(
            "status"
        ) == "found":

            print(
                f"           mux="
                f"{mux.get('instance')} "
                f"pin={mux.get('input_pin')} "
                f"type={mux.get('type')}"
            )

        else:

            print(
                "           final mux not found"
            )

    print()
    print(
        "Resolution"
    )
    print(
        "-" * 68
    )

    print(
        f"Status       : "
        f"{physical_resolution.get('status')}"
    )

    print(
        f"Reason       : "
        f"{physical_resolution.get('reason')}"
    )

    print(
        f"Branch       : "
        f"{selected_branch}"
    )

    print(
        f"RTL operator : "
        f"{selected_operator}"
    )

    print(
        f"Confidence   : "
        f"{physical_resolution.get('confidence')}"
    )

    print()
    print(
        f"JSON written to:\n{output_file}"
    )

    print(
        "=" * 68
    )


if __name__ == "__main__":
    main()