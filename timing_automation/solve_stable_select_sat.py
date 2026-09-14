#!/usr/bin/env python3

from __future__ import annotations

import argparse
import copy
import json
import re
import subprocess
import tempfile

from pathlib import Path
from typing import Any, Dict, List, Tuple

try:
    from solve_select_sat import (
        collect_select_cones, find_top, load_json, norm,
        regenerate_preabc_json, verify_graph_identity, sha256_file,
    )
except ImportError:
    from timing_automation.solve_select_sat import (
        collect_select_cones, find_top, load_json, norm,
        regenerate_preabc_json, verify_graph_identity, sha256_file,
    )


SCHEMA = "nebula_stable_select_sat/1"


def canonical_launch_bit(
    mapping: Dict[str, Any],
) -> int | None:
    """
    launch_q_bit is currently the compatibility field used by
    map_to_rtl for PI and register launch boundaries alike.
    Prefer a future launch_bit field if/when one exists.
    """

    value = mapping.get("launch_bit")

    if isinstance(value, int):
        return value

    value = mapping.get("launch_q_bit")

    if isinstance(value, int):
        return value

    return None


def raw_primary_input_bits(
    module: Dict[str, Any],
) -> set[int]:

    result: set[int] = set()

    for port in (
        module.get("ports")
        or {}
    ).values():

        direction = norm(
            port.get("direction")
        ).lower()

        if direction not in (
            "input",
            "inout",
        ):
            continue

        for bit in (
            port.get("bits")
            or []
        ):

            if isinstance(bit, int):
                result.add(bit)

    return result


def make_scalar_port(
    ports: Dict[str, Any],
    netnames: Dict[str, Any],
    name: str,
    bit: int,
    direction: str,
) -> None:

    ports[name] = {
        "direction": direction,
        "bits": [bit],
    }

    netnames[name] = {
        "hide_name": 0,
        "bits": [bit],
        "attributes": {},
    }


def parse_scalar(
    output: str,
    signal: str,
) -> str | None:

    pattern = re.compile(
        rf"^\s*\\?{re.escape(signal)}"
        rf"\s+.*?\s+([01xXzZ])\s*$",
        re.MULTILINE,
    )

    match = pattern.search(output)

    if match is None:
        return None

    return match.group(1).lower()


def collect_path_pairs(
    mapped: Dict[str, Any],
) -> Dict[Tuple[int, int], Dict[str, Any]]:

    pairs: Dict[
        Tuple[int, int],
        Dict[str, Any],
    ] = {}

    for path_index, item in enumerate(
        mapped.get("paths")
        or []
    ):

        if not isinstance(item, dict):
            continue

        mapping = (
            item.get("rtl_mapping")
            or {}
        )

        if not isinstance(mapping, dict):
            continue

        # Legacy/earlier physical evidence has precedence.  Do not spend
        # SAT/STA work on a path that is already physically resolved.
        if (mapping.get("physical_narrowing") or {}).get("resolved") is True:
            continue

        branch_points = (
            mapping.get(
                "logical_branch_points"
            )
            or []
        )

        if not branch_points:
            continue

        launch_bit = canonical_launch_bit(
            mapping
        )

        if launch_bit is None:
            raise RuntimeError(
                f"path {path_index}: logical "
                "reconvergence exists but no "
                "canonical launch bit is available"
            )

        for point in branch_points:

            if not isinstance(point, dict):
                continue

            cone_by_bit = {
                int(cone["bit"]): cone

                for cone in (
                    point.get(
                        "select_cones"
                    )
                    or []
                )

                if isinstance(cone, dict)
                and cone.get("bit")
                is not None
            }

            for raw_select in (
                point.get(
                    "select_bits"
                )
                or []
            ):

                select_bit = int(
                    raw_select
                )

                cone = cone_by_bit.get(
                    select_bit
                )

                if cone is None:
                    raise RuntimeError(
                        f"path {path_index}: "
                        f"select bit {select_bit} "
                        "has no select-cone metadata"
                    )

                key = (
                    select_bit,
                    launch_bit,
                )

                occurrence = {
                    "path_index": path_index,
                    "startpoint": (
                        item.get("startpoint")
                        or item.get(
                            "startpoint_raw"
                        )
                    ),
                    "endpoint": (
                        item.get("endpoint")
                        or item.get(
                            "endpoint_raw"
                        )
                    ),
                    "cell": point.get("cell"),
                }

                previous = pairs.get(
                    key
                )

                if previous is None:

                    pairs[key] = {
                        "select_bit": (
                            select_bit
                        ),
                        "launch_bit": (
                            launch_bit
                        ),
                        "cone": cone,
                        "occurrences": [
                            occurrence
                        ],
                    }

                    continue

                if json.dumps(
                    previous["cone"],
                    sort_keys=True,
                ) != json.dumps(
                    cone,
                    sort_keys=True,
                ):

                    raise RuntimeError(
                        "conflicting select-cone "
                        "metadata for "
                        f"select={select_bit}, "
                        f"launch={launch_bit}"
                    )

                previous[
                    "occurrences"
                ].append(
                    occurrence
                )

    return pairs


def run_stable_query(
    *,
    base_raw: Dict[str, Any],
    top_name: str,
    rtl_top: str,
    yosys: Path,
    select_bit: int,
    launch_bit: int,
    boundary_bits: List[int],
    requested_value: int,
    timeout_s: int,
    work_dir: Path,
) -> Dict[str, Any]:

    design = copy.deepcopy(
        base_raw
    )

    module = design[
        "modules"
    ][
        top_name
    ]

    ports = (
        module.get("ports")
        or {}
    )

    # --------------------------------------------------------
    # Keep original inputs, but remove original outputs.
    #
    # The selector becomes the only observable child output.
    # flatten + opt_clean can therefore prune unrelated logic,
    # including unrelated sequential cells.
    # --------------------------------------------------------

    for port_name in list(
        ports
    ):

        direction = norm(
            ports[
                port_name
            ].get(
                "direction"
            )
        ).lower()

        if direction == "output":

            del ports[
                port_name
            ]

        elif direction == "inout":

            return {
                "status": "unsupported",
                "reason": (
                    "top_level_inout_present"
                ),
                "requested_value": (
                    requested_value
                ),
                "case_assignment": {},
            }

    module.setdefault(
        "netnames",
        {}
    )[
        "nebula_sat_select"
    ] = {
        "hide_name": 0,
        "bits": [select_bit],
        "attributes": {},
    }

    ports[
        "nebula_sat_select"
    ] = {
        "direction": "output",
        "bits": [select_bit],
    }

    # --------------------------------------------------------
    # Two-copy wrapper:
    #
    #   copy 0: launch = 0
    #   copy 1: launch = 1
    #
    # Every other PI is physically the same wrapper net in
    # both copies.
    # --------------------------------------------------------

    wrapper_name = (
        "nebula_stable_select_miter"
    )

    wrapper_ports: Dict[
        str,
        Any,
    ] = {}

    wrapper_netnames: Dict[
        str,
        Any,
    ] = {}

    shared_bits: Dict[
        int,
        int,
    ] = {}

    next_bit = 2

    for port_name, info in (
        ports.items()
    ):

        direction = norm(
            info.get("direction")
        ).lower()

        if direction != "input":
            continue

        for raw_bit in (
            info.get("bits")
            or []
        ):

            if not isinstance(
                raw_bit,
                int,
            ):
                continue

            if raw_bit == launch_bit:
                continue

            if raw_bit in shared_bits:
                continue

            wrapper_bit = next_bit
            next_bit += 1

            shared_bits[
                raw_bit
            ] = wrapper_bit

            make_scalar_port(
                wrapper_ports,
                wrapper_netnames,
                (
                    f"nebula_in_"
                    f"{raw_bit}"
                ),
                wrapper_bit,
                "input",
            )

    select_0_bit = next_bit
    next_bit += 1

    select_1_bit = next_bit
    next_bit += 1

    make_scalar_port(
        wrapper_ports,
        wrapper_netnames,
        "nebula_select_0",
        select_0_bit,
        "output",
    )

    make_scalar_port(
        wrapper_ports,
        wrapper_netnames,
        "nebula_select_1",
        select_1_bit,
        "output",
    )

    def build_instance(
        launch_value: int,
        selector_output: int,
    ) -> Dict[str, Any]:

        connections: Dict[
            str,
            Any,
        ] = {}

        directions: Dict[
            str,
            str,
        ] = {}

        for port_name, info in (
            ports.items()
        ):

            direction = norm(
                info.get("direction")
            ).lower()

            directions[
                port_name
            ] = direction

            if (
                port_name
                == "nebula_sat_select"
            ):

                connections[
                    port_name
                ] = [
                    selector_output
                ]

                continue

            if direction != "input":

                raise RuntimeError(
                    "unexpected child port "
                    f"{port_name}: "
                    f"{direction}"
                )

            connected_bits = []

            for raw_bit in (
                info.get("bits")
                or []
            ):

                if isinstance(
                    raw_bit,
                    int,
                ):

                    if (
                        raw_bit
                        == launch_bit
                    ):

                        connected_bits.append(
                            str(
                                launch_value
                            )
                        )

                    else:

                        if (
                            raw_bit
                            not in shared_bits
                        ):

                            raise RuntimeError(
                                "missing shared PI "
                                f"for bit {raw_bit}"
                            )

                        connected_bits.append(
                            shared_bits[
                                raw_bit
                            ]
                        )

                else:

                    # Preserve Yosys constant bits.
                    connected_bits.append(
                        raw_bit
                    )

            connections[
                port_name
            ] = connected_bits

        return {
            "hide_name": 0,
            "type": top_name,
            "parameters": {},
            "attributes": {},
            "port_directions": (
                directions
            ),
            "connections": (
                connections
            ),
        }

    wrapper = {
        "attributes": {},
        "ports": wrapper_ports,
        "cells": {
            "u_launch_0": (
                build_instance(
                    0,
                    select_0_bit,
                )
            ),
            "u_launch_1": (
                build_instance(
                    1,
                    select_1_bit,
                )
            ),
        },
        "netnames": (
            wrapper_netnames
        ),
    }

    design[
        "modules"
    ][
        wrapper_name
    ] = wrapper

    model_path = (
        work_dir
        / (
            f"stable_select_"
            f"{select_bit}_"
            f"launch_{launch_bit}_"
            f"value_{requested_value}"
            ".json"
        )
    )

    model_path.write_text(
        json.dumps(
            design
        )
    )

    case_bits = sorted(
        bit
        for bit in boundary_bits
        if bit != launch_bit
    )

    for bit in case_bits:

        if bit not in shared_bits:

            return {
                "status": "solver_error",
                "reason": (
                    "boundary_bit_not_"
                    "available_as_primary_input"
                ),
                "missing_bit": bit,
                "requested_value": (
                    requested_value
                ),
                "case_assignment": {},
            }

    show_args = " ".join(
        f"-show nebula_in_{bit}"
        for bit in case_bits
    )

    script = "\n".join(
        [
            (
                "read_json "
                f'"{model_path}"'
            ),
            (
                "hierarchy -check -top "
                f"{wrapper_name}"
            ),
            "flatten",
            "opt_clean",
            (
                "sat "
                f"-timeout {timeout_s} "
                "-set-def-inputs "
                "-set nebula_select_0 "
                f"{requested_value} "
                "-set nebula_select_1 "
                f"{requested_value} "
                "-show nebula_select_0 "
                "-show nebula_select_1 "
                f"{show_args}"
            ),
        ]
    )

    result = subprocess.run(
        [
            str(yosys),
            "-Q",
            "-p",
            script,
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    output = (
        result.stdout
        or ""
    )

    if result.returncode != 0:

        return {
            "status": "solver_error",
            "reason": (
                "yosys_nonzero_exit"
            ),
            "requested_value": (
                requested_value
            ),
            "returncode": (
                result.returncode
            ),
            "case_assignment": {},
            "log_tail": (
                output
                .splitlines()[-40:]
            ),
        }

    if (
        "SAT solving finished - "
        "model found:"
        in output
    ):

        status = "sat"

    elif (
        "SAT solving finished - "
        "no model found"
        in output
    ):

        return {
            "status": "unsat",
            "requested_value": (
                requested_value
            ),
            "case_assignment": {},
            "launch_excluded": True,
        }

    else:

        return {
            "status": "solver_error",
            "reason": (
                "unrecognized_solver_result"
            ),
            "requested_value": (
                requested_value
            ),
            "case_assignment": {},
            "log_tail": (
                output
                .splitlines()[-40:]
            ),
        }

    select_0 = parse_scalar(
        output,
        "nebula_select_0",
    )

    select_1 = parse_scalar(
        output,
        "nebula_select_1",
    )

    if (
        select_0
        != str(requested_value)
        or select_1
        != str(requested_value)
    ):

        return {
            "status": "solver_error",
            "reason": (
                "stable_select_model_"
                "mismatch"
            ),
            "requested_value": (
                requested_value
            ),
            "observed_select_0": (
                select_0
            ),
            "observed_select_1": (
                select_1
            ),
            "case_assignment": {},
        }

    assignment: Dict[
        str,
        int,
    ] = {}

    for bit in case_bits:

        value = parse_scalar(
            output,
            f"nebula_in_{bit}",
        )

        if value not in (
            "0",
            "1",
        ):

            return {
                "status": "solver_error",
                "reason": (
                    "incomplete_case_model"
                ),
                "missing_bit": bit,
                "requested_value": (
                    requested_value
                ),
                "case_assignment": {},
                "log_tail": (
                    output
                    .splitlines()[-40:]
                ),
            }

        assignment[
            str(bit)
        ] = int(
            value
        )

    return {
        "status": status,
        "requested_value": (
            requested_value
        ),
        "case_assignment": (
            assignment
        ),
        "launch_excluded": True,
        "method": (
            "two_copy_stable_control_sat"
        ),
    }


def stable_class(
    zero_status: str,
    one_status: str,
) -> str:

    if (
        zero_status == "sat"
        and one_status == "sat"
    ):
        return "both_stable"

    if (
        zero_status == "sat"
        and one_status == "unsat"
    ):
        return "only_a_stable"

    if (
        zero_status == "unsat"
        and one_status == "sat"
    ):
        return "only_b_stable"

    if (
        zero_status == "unsat"
        and one_status == "unsat"
    ):
        return "no_stable_branch"

    return "unresolved"


def main() -> None:

    parser = argparse.ArgumentParser(
        description=(
            "Find path-aware mux-select "
            "assignments that remain stable "
            "while the timing launch bit toggles."
        )
    )

    parser.add_argument(
        "--mapped-json",
        required=True,
    )

    parser.add_argument(
        "--rtl-graph",
        required=True,
    )

    parser.add_argument(
        "--output",
        required=True,
    )

    parser.add_argument(
        "--yosys",
        default=None,
    )

    parser.add_argument(
        "--timeout",
        type=int,
        default=20,
    )

    args = parser.parse_args()

    mapped_path = Path(
        args.mapped_json
    ).resolve()

    graph_path = Path(
        args.rtl_graph
    ).resolve()

    output_path = Path(
        args.output
    ).resolve()

    mapped = load_json(
        mapped_path
    )

    graph = load_json(
        graph_path
    )

    rtl_top = norm(
        graph.get("rtl_top")
        or graph.get("module")
        or graph.get("design")
    )

    checkpoint = Path(
        str(
            graph.get(
                "source_checkpoint"
            )
            or ""
        )
    ).resolve()

    if not checkpoint.exists():

        raise FileNotFoundError(
            "Canonical RTLIL checkpoint "
            f"not found: {checkpoint}"
        )

    yosys = Path(
        args.yosys
        or str(
            graph.get(
                "yosys_executable"
            )
            or ""
        )
    ).resolve()

    if not yosys.exists():

        raise FileNotFoundError(
            f"Yosys not found: {yosys}"
        )

    pairs = collect_path_pairs(
        mapped
    )

    # Reuse the ordinary select-cone identity verification.
    cones, occurrences = (
        collect_select_cones(
            mapped
        )
    )

    with tempfile.TemporaryDirectory(
        prefix=(
            "nebula_stable_select_sat_"
        )
    ) as tmp:

        work_dir = Path(tmp)

        raw_path = (
            work_dir
            / "preabc.json"
        )

        regenerate_preabc_json(
            checkpoint=checkpoint,
            rtl_top=rtl_top,
            yosys=yosys,
            output=raw_path,
        )

        raw = load_json(
            raw_path
        )

        top_name, top = find_top(
            raw,
            rtl_top,
        )

        verify_graph_identity(
            raw=raw,
            rtl_top=rtl_top,
            cones=cones,
            occurrences=occurrences,
        )

        primary_inputs = (
            raw_primary_input_bits(
                top
            )
        )

        results = []

        for (
            select_bit,
            launch_bit,
        ), info in sorted(
            pairs.items()
        ):

            cone = info[
                "cone"
            ]

            base = {
                "select_bit": (
                    select_bit
                ),
                "launch_bit": (
                    launch_bit
                ),
                "select_kind": (
                    cone.get("kind")
                ),
                "select_cone_status": (
                    cone.get("status")
                ),
                "boundary_input_bits": (
                    cone.get(
                        "boundary_input_bits"
                    )
                    or []
                ),
                "boundary_register_bits": (
                    cone.get(
                        "boundary_register_bits"
                    )
                    or []
                ),
                "occurrences": (
                    info[
                        "occurrences"
                    ]
                ),
            }

            if (
                cone.get("status")
                != "resolved"
                or cone.get(
                    "unresolved_bits"
                )
            ):

                results.append(
                    {
                        **base,
                        "status": (
                            "unsupported"
                        ),
                        "reason": (
                            "unresolved_select_cone"
                        ),
                        "zero": {
                            "status": (
                                "unsupported"
                            )
                        },
                        "one": {
                            "status": (
                                "unsupported"
                            )
                        },
                        "stable_feasibility": (
                            "unresolved"
                        ),
                    }
                )

                continue

            register_bits = list(
                cone.get(
                    "boundary_register_bits"
                )
                or []
            )

            if register_bits:

                results.append(
                    {
                        **base,
                        "status": (
                            "unsupported"
                        ),
                        "reason": (
                            "sequential_select_"
                            "boundary_not_yet_"
                            "supported"
                        ),
                        "zero": {
                            "status": (
                                "unsupported"
                            )
                        },
                        "one": {
                            "status": (
                                "unsupported"
                            )
                        },
                        "stable_feasibility": (
                            "unresolved"
                        ),
                    }
                )

                continue

            if (
                launch_bit
                not in primary_inputs
            ):

                results.append(
                    {
                        **base,
                        "status": (
                            "unsupported"
                        ),
                        "reason": (
                            "launch_boundary_not_"
                            "primary_input"
                        ),
                        "zero": {
                            "status": (
                                "unsupported"
                            )
                        },
                        "one": {
                            "status": (
                                "unsupported"
                            )
                        },
                        "stable_feasibility": (
                            "unresolved"
                        ),
                    }
                )

                continue

            boundary_bits = [
                int(bit)
                for bit in (
                    cone.get(
                        "boundary_input_bits"
                    )
                    or []
                )
            ]

            zero = run_stable_query(
                base_raw=raw,
                top_name=top_name,
                rtl_top=rtl_top,
                yosys=yosys,
                select_bit=select_bit,
                launch_bit=launch_bit,
                boundary_bits=boundary_bits,
                requested_value=0,
                timeout_s=args.timeout,
                work_dir=work_dir,
            )

            one = run_stable_query(
                base_raw=raw,
                top_name=top_name,
                rtl_top=rtl_top,
                yosys=yosys,
                select_bit=select_bit,
                launch_bit=launch_bit,
                boundary_bits=boundary_bits,
                requested_value=1,
                timeout_s=args.timeout,
                work_dir=work_dir,
            )

            feasibility = stable_class(
                zero["status"],
                one["status"],
            )

            statuses = {
                zero["status"],
                one["status"],
            }

            if "solver_error" in statuses:

                status = "solver_error"

            elif "unsupported" in statuses:

                status = "unsupported"

            else:

                status = "resolved"

            results.append(
                {
                    **base,
                    "status": status,
                    "zero": zero,
                    "one": one,
                    "stable_feasibility": (
                        feasibility
                    ),
                }
            )

    resolved_count = sum(
        1
        for item in results
        if item.get("status")
        == "resolved"
    )

    both_stable_count = sum(
        1
        for item in results
        if item.get(
            "stable_feasibility"
        )
        == "both_stable"
    )

    unsupported_count = sum(
        1
        for item in results
        if item.get("status")
        == "unsupported"
    )

    solver_error_count = sum(
        1
        for item in results
        if item.get("status")
        == "solver_error"
    )

    document = {
        "schema": SCHEMA,
        "design": graph.get("design"),
        "platform": graph.get(
            "platform"
        ),
        "flow_variant": graph.get(
            "flow_variant"
        ),
        "rtl_top": rtl_top,
        "source_checkpoint": str(
            checkpoint
        ),
        "yosys_executable": str(
            yosys
        ),
        "graph_identity_verified": True,
        "source_mapped_json": str(mapped_path),
        "source_mapped_sha256": sha256_file(mapped_path),
        "source_rtl_graph": str(graph_path),
        "source_rtl_graph_sha256": sha256_file(graph_path),
        "results": results,
        "summary": {
            "pair_count": len(
                results
            ),
            "resolved_count": (
                resolved_count
            ),
            "both_stable_count": (
                both_stable_count
            ),
            "unsupported_count": (
                unsupported_count
            ),
            "solver_error_count": (
                solver_error_count
            ),
        },
    }

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    output_path.write_text(
        json.dumps(
            document,
            indent=2,
        )
    )

    print()
    print("=" * 72)
    print("STABLE SELECT SAT")
    print("=" * 72)

    print(
        f"Design         : "
        f"{graph.get('design')}"
    )

    print(
        f"Pairs          : "
        f"{len(results)}"
    )

    print(
        f"Resolved       : "
        f"{resolved_count}"
    )

    print(
        f"Both stable    : "
        f"{both_stable_count}"
    )

    print(
        f"Unsupported    : "
        f"{unsupported_count}"
    )

    print(
        f"Solver errors  : "
        f"{solver_error_count}"
    )

    for item in results:

        print(
            f"select="
            f"{item['select_bit']:<4} "
            f"launch="
            f"{item['launch_bit']:<4} "
            f"S=0:"
            f"{item['zero']['status']:<12} "
            f"S=1:"
            f"{item['one']['status']:<12} "
            f"{item['stable_feasibility']}"
        )

    print()
    print(
        f"Stable SAT JSON: "
        f"{output_path}"
    )

    print("=" * 72)

    if solver_error_count:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
