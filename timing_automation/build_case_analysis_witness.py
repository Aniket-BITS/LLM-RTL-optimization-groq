#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
import re

from pathlib import Path
from typing import Any, Dict, List


SCHEMA = "nebula_case_analysis_witness/1"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_json(path: Path) -> Dict[str, Any]:
    with path.open() as f:
        value = json.load(f)

    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")

    return value


def norm(value: Any) -> str:
    text = str(value or "").strip()

    if text.startswith("\\"):
        text = text[1:]

    return text


def port_bit_lookup(
    graph: Dict[str, Any],
) -> Dict[int, Dict[str, Any]]:
    """
    Map pre-ABC Yosys bit IDs to exact top-level input-port references.

    Fail closed if a bit cannot be mapped uniquely.
    """

    ports = graph.get("ports") or {}
    bit_names = graph.get("bit_names") or {}

    lookup: Dict[int, Dict[str, Any]] = {}

    for port_name, port in ports.items():
        if not isinstance(port, dict):
            continue

        direction = norm(
            port.get("direction")
        ).lower()

        if direction not in (
            "input",
            "inout",
        ):
            continue

        raw_bits = port.get("bits") or []

        for bit in raw_bits:
            if not isinstance(bit, int):
                continue

            names = [
                norm(name)
                for name in (
                    bit_names.get(
                        str(bit),
                        [],
                    )
                    or []
                )
            ]

            scalar_name = norm(
                port_name
            )

            candidates = []

            for name in names:
                if name == scalar_name:
                    candidates.append(
                        {
                            "port": scalar_name,
                            "index": None,
                            "signal": scalar_name,
                        }
                    )
                    continue

                match = re.fullmatch(
                    re.escape(scalar_name)
                    + r"\[(-?\d+)\]",
                    name,
                )

                if match is not None:
                    index = int(
                        match.group(1)
                    )

                    candidates.append(
                        {
                            "port": scalar_name,
                            "index": index,
                            "signal": (
                                f"{scalar_name}"
                                f"[{index}]"
                            ),
                        }
                    )

            unique = {}

            for candidate in candidates:
                key = (
                    candidate["port"],
                    candidate["index"],
                    candidate["signal"],
                )

                unique[key] = candidate

            candidates = list(
                unique.values()
            )

            if len(candidates) != 1:
                raise RuntimeError(
                    "Cannot uniquely map "
                    f"top-level input bit {bit} "
                    f"for port {port_name}. "
                    f"bit_names={names}, "
                    f"candidates={candidates}"
                )

            candidate = candidates[0]

            if (
                bit in lookup
                and lookup[bit] != {
                    "bit": bit,
                    **candidate,
                }
            ):
                raise RuntimeError(
                    "Conflicting top-level "
                    f"port mappings for bit "
                    f"{bit}: "
                    f"{lookup[bit]} vs "
                    f"{candidate}"
                )

            lookup[bit] = {
                "bit": bit,
                **candidate,
            }

    return lookup


def convert_assignment(
    *,
    assignment: Dict[str, Any],
    launch_bit: int,
    lookup: Dict[int, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    result = []

    for raw_bit, raw_value in sorted(
        assignment.items(),
        key=lambda item: int(item[0]),
    ):
        bit = int(raw_bit)
        value = int(raw_value)

        if bit == launch_bit:
            raise RuntimeError(
                "Stable-control witness "
                "attempted to constrain "
                f"timing launch bit {bit}"
            )

        if value not in (0, 1):
            raise RuntimeError(
                f"Invalid case value "
                f"{value} for bit {bit}"
            )

        port_ref = lookup.get(bit)

        if port_ref is None:
            raise RuntimeError(
                "Case-analysis bit "
                f"{bit} is not a uniquely "
                "identified top-level input"
            )

        result.append(
            {
                "bit": bit,
                "port": port_ref["port"],
                "index": port_ref["index"],
                "signal": port_ref["signal"],
                "value": value,
            }
        )

    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Translate stable-select SAT "
            "bit assignments into exact "
            "top-level RTL case-analysis signals."
        )
    )

    parser.add_argument(
        "--stable-sat",
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

    args = parser.parse_args()

    stable_path = Path(
        args.stable_sat
    ).resolve()

    graph_path = Path(
        args.rtl_graph
    ).resolve()

    output_path = Path(
        args.output
    ).resolve()

    stable = load_json(
        stable_path
    )

    graph = load_json(
        graph_path
    )

    if (
        stable.get("schema")
        != "nebula_stable_select_sat/1"
    ):
        raise RuntimeError(
            "Unexpected stable-SAT schema: "
            f"{stable.get('schema')}"
        )

    if (
        stable.get("design")
        != graph.get("design")
    ):
        raise RuntimeError(
            "Design mismatch between "
            "stable SAT and RTL graph"
        )

    if (
        stable.get("platform")
        != graph.get("platform")
    ):
        raise RuntimeError(
            "Platform mismatch between "
            "stable SAT and RTL graph"
        )

    if (
        stable.get("flow_variant")
        != graph.get("flow_variant")
    ):
        raise RuntimeError(
            "Flow-variant mismatch between "
            "stable SAT and RTL graph"
        )

    expected_graph_sha = stable.get("source_rtl_graph_sha256")
    if isinstance(expected_graph_sha, str) and expected_graph_sha:
        if expected_graph_sha.lower() != sha256_file(graph_path).lower():
            raise RuntimeError("stable-SAT RTL-graph SHA-256 does not match supplied graph")

    if (
        stable.get(
            "graph_identity_verified"
        )
        is not True
    ):
        raise RuntimeError(
            "Stable SAT did not verify "
            "pre-ABC graph identity"
        )

    lookup = port_bit_lookup(
        graph
    )

    witnesses = []

    for item in (
        stable.get("results")
        or []
    ):
        if not isinstance(
            item,
            dict,
        ):
            continue

        select_bit = int(
            item["select_bit"]
        )

        launch_bit = int(
            item["launch_bit"]
        )

        base = {
            "select_bit": select_bit,
            "launch_bit": launch_bit,
            "stable_feasibility": (
                item.get(
                    "stable_feasibility"
                )
            ),
            "occurrences": (
                item.get(
                    "occurrences"
                )
                or []
            ),
        }

        if (
            item.get("status")
            != "resolved"
        ):
            witnesses.append(
                {
                    **base,
                    "status": "unsupported",
                    "reason": (
                        "stable_sat_not_resolved"
                    ),
                }
            )
            continue

        branches = {}

        for (
            side,
            value,
            branch,
        ) in (
            (
                "zero",
                0,
                "mux_a",
            ),
            (
                "one",
                1,
                "mux_b",
            ),
        ):
            query = (
                item.get(side)
                or {}
            )

            if (
                query.get("status")
                != "sat"
            ):
                branches[branch] = {
                    "status": (
                        query.get("status")
                        or "unsupported"
                    ),
                    "select_value": value,
                    "case_analysis": [],
                }
                continue

            assignment = (
                query.get(
                    "case_assignment"
                )
                or {}
            )

            case_analysis = (
                convert_assignment(
                    assignment=assignment,
                    launch_bit=launch_bit,
                    lookup=lookup,
                )
            )

            branches[branch] = {
                "status": "ready",
                "select_value": value,
                "case_analysis": (
                    case_analysis
                ),
                "excluded_launch_bit": (
                    launch_bit
                ),
                "source_method": (
                    query.get("method")
                ),
            }

        ready_count = sum(
            1
            for branch_doc
            in branches.values()
            if (
                branch_doc.get("status")
                == "ready"
            )
        )

        witnesses.append(
            {
                **base,
                "status": (
                    "ready"
                    if ready_count > 0
                    else "unsupported"
                ),
                "branches": branches,
            }
        )

    ready_pairs = sum(
        1
        for item in witnesses
        if item.get("status")
        == "ready"
    )

    document = {
        "schema": SCHEMA,
        "design": (
            graph.get("design")
        ),
        "platform": (
            graph.get("platform")
        ),
        "flow_variant": (
            graph.get(
                "flow_variant"
            )
        ),
        "rtl_top": (
            graph.get("rtl_top")
        ),
        "graph_identity_verified": True,
        "source_stable_sat": str(stable_path),
        "source_stable_sat_sha256": sha256_file(stable_path),
        "source_rtl_graph": str(graph_path),
        "source_rtl_graph_sha256": sha256_file(graph_path),
        "witnesses": witnesses,
        "summary": {
            "pair_count": len(
                witnesses
            ),
            "ready_pair_count": (
                ready_pairs
            ),
            "unsupported_pair_count": (
                len(witnesses)
                - ready_pairs
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
    print("CASE ANALYSIS WITNESS")
    print("=" * 72)

    print(
        f"Design      : "
        f"{document['design']}"
    )

    print(
        f"Pairs       : "
        f"{len(witnesses)}"
    )

    print(
        f"Ready       : "
        f"{ready_pairs}"
    )

    print(
        f"Unsupported : "
        f"{len(witnesses) - ready_pairs}"
    )

    for witness in witnesses:
        print()

        print(
            f"select="
            f"{witness['select_bit']} "
            f"launch="
            f"{witness['launch_bit']} "
            f"status="
            f"{witness['status']}"
        )

        for branch in (
            "mux_a",
            "mux_b",
        ):
            branch_doc = (
                witness.get(
                    "branches",
                    {},
                ).get(
                    branch,
                )
                or {}
            )

            if (
                branch_doc.get("status")
                != "ready"
            ):
                continue

            rendered = " ".join(
                (
                    f"{case['signal']}="
                    f"{case['value']}"
                )
                for case in (
                    branch_doc.get(
                        "case_analysis"
                    )
                    or []
                )
            )

            print(
                f"  {branch:<5} "
                f"S="
                f"{branch_doc['select_value']} "
                f"{rendered}"
            )

    print()
    print(
        f"Witness JSON: "
        f"{output_path}"
    )
    print("=" * 72)


if __name__ == "__main__":
    main()
