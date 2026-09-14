#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
import re

from pathlib import Path
from typing import Any, Dict


SCHEMA = "nebula_case_witness_request/1"


def load(path: Path) -> Dict[str, Any]:
    with path.open() as f:
        obj = json.load(f)
    if not isinstance(obj, dict):
        raise RuntimeError(f"expected JSON object: {path}")
    return obj



def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def capture_d_pin(endpoint: str) -> str:
    clean = norm(endpoint)
    return clean if clean.endswith("/D") else f"{clean}/D"


def norm(value: Any) -> str:
    s = str(value or "").strip()
    if s.startswith("\\"):
        s = s[1:]
    return s


def exact_regex(signal: str) -> str:
    return "^" + re.escape(signal) + "$"


def build_port_lookup(
    graph: Dict[str, Any],
    wanted_direction: str,
) -> Dict[int, str]:

    ports = graph.get("ports") or {}
    bit_names = graph.get("bit_names") or {}

    result: Dict[int, str] = {}

    for port_name, port in ports.items():

        if not isinstance(port, dict):
            continue

        direction = norm(
            port.get("direction")
        ).lower()

        if direction != wanted_direction:
            continue

        pname = norm(port_name)

        for bit in port.get("bits") or []:

            if not isinstance(bit, int):
                continue

            names = [
                norm(x)
                for x in (
                    bit_names.get(str(bit), [])
                    or []
                )
            ]

            candidates = []

            for name in names:

                if name == pname:
                    candidates.append(name)
                    continue

                if re.fullmatch(
                    re.escape(pname) + r"\[-?\d+\]",
                    name,
                ):
                    candidates.append(name)

            candidates = sorted(
                set(candidates)
            )

            if len(candidates) != 1:
                raise RuntimeError(
                    f"bit {bit}: cannot uniquely map "
                    f"{wanted_direction} port; "
                    f"names={names}, "
                    f"candidates={candidates}"
                )

            result[bit] = candidates[0]

    return result


def canonical_launch_bit(
    mapping: Dict[str, Any],
) -> int | None:

    bit = mapping.get("launch_bit")

    if isinstance(bit, int):
        return bit

    bit = mapping.get("launch_q_bit")

    if isinstance(bit, int):
        return bit

    return None


def main() -> None:

    ap = argparse.ArgumentParser()

    ap.add_argument(
        "--case-witness",
        required=True,
    )

    ap.add_argument(
        "--mapped-json",
        required=True,
    )

    ap.add_argument(
        "--rtl-graph",
        required=True,
    )

    ap.add_argument(
        "--output",
        required=True,
    )

    ap.add_argument(
        "--path-index",
        type=int,
        default=None,
    )

    ap.add_argument(
        "--select-bit",
        type=int,
        default=None,
    )

    args = ap.parse_args()

    case_path = Path(
        args.case_witness
    ).resolve()

    mapped_path = Path(
        args.mapped_json
    ).resolve()

    graph_path = Path(
        args.rtl_graph
    ).resolve()

    output_path = Path(
        args.output
    ).resolve()

    case_doc = load(case_path)
    mapped = load(mapped_path)
    graph = load(graph_path)

    if (
        case_doc.get("schema")
        != "nebula_case_analysis_witness/1"
    ):
        raise RuntimeError(
            "bad case witness schema"
        )

    for key in (
        "design",
        "platform",
        "flow_variant",
    ):
        if (
            case_doc.get(key)
            != graph.get(key)
        ):
            raise RuntimeError(
                f"{key} mismatch between "
                "case witness and graph"
            )

    graph_sha = sha256_file(graph_path)
    expected_graph_sha = case_doc.get("source_rtl_graph_sha256")
    if isinstance(expected_graph_sha, str) and expected_graph_sha and expected_graph_sha.lower() != graph_sha.lower():
        raise RuntimeError("case witness RTL-graph SHA-256 does not match supplied graph")

    stable_source = case_doc.get("source_stable_sat")
    stable_sha = case_doc.get("source_stable_sat_sha256")
    if isinstance(stable_source, str) and isinstance(stable_sha, str):
        stable_path = Path(stable_source)
        if not stable_path.is_file() or sha256_file(stable_path).lower() != stable_sha.lower():
            raise RuntimeError("case witness stable-SAT provenance is stale or missing")

    paths = mapped.get("paths") or []

    input_lookup = build_port_lookup(
        graph,
        "input",
    )

    output_lookup = build_port_lookup(
        graph,
        "output",
    )

    requests = []
    unsupported = []

    for witness in (
        case_doc.get("witnesses")
        or []
    ):

        if (
            witness.get("status")
            != "ready"
        ):
            continue

        select_bit = int(
            witness["select_bit"]
        )

        launch_bit = int(
            witness["launch_bit"]
        )

        if (
            args.select_bit is not None
            and select_bit
            != args.select_bit
        ):
            continue

        launch_signal = input_lookup.get(
            launch_bit
        )

        if launch_signal is None:
            unsupported.append({
                "select_bit": select_bit,
                "launch_bit": launch_bit,
                "reason": "launch_boundary_not_primary_input",
            })
            continue

        for occurrence in (
            witness.get("occurrences")
            or []
        ):

            path_index = occurrence.get(
                "path_index"
            )

            if not isinstance(
                path_index,
                int,
            ):
                continue

            if (
                args.path_index is not None
                and path_index
                != args.path_index
            ):
                continue

            if not (
                0 <= path_index < len(paths)
            ):
                raise RuntimeError(
                    f"invalid path index "
                    f"{path_index}"
                )

            path = paths[path_index]

            mapping = (
                path.get("rtl_mapping")
                or {}
            )

            mapped_launch = (
                canonical_launch_bit(
                    mapping
                )
            )

            if mapped_launch != launch_bit:
                raise RuntimeError(
                    f"path {path_index}: "
                    "launch-bit mismatch "
                    f"{mapped_launch} != "
                    f"{launch_bit}"
                )

            end_bit = mapping.get(
                "end_bit"
            )

            if not isinstance(
                end_bit,
                int,
            ):
                raise RuntimeError(
                    f"path {path_index}: "
                    "no canonical end_bit"
                )

            endpoint_kind = mapping.get("endpoint_kind")
            if endpoint_kind == "primary_output":
                endpoint_signal = output_lookup.get(end_bit)
                if endpoint_signal is None:
                    raise RuntimeError(
                        f"path {path_index}: output bit {end_bit} not mapped to a port"
                    )
                to_kind = "primary_output"
                to_regex = exact_regex(endpoint_signal)
            elif endpoint_kind == "register_capture":
                raw_endpoint = path.get("endpoint_raw") or path.get("endpoint")
                if not isinstance(raw_endpoint, str) or not raw_endpoint:
                    unsupported.append({
                        "path_index": path_index,
                        "select_bit": select_bit,
                        "reason": "capture_endpoint_name_unavailable",
                    })
                    continue
                endpoint_signal = capture_d_pin(raw_endpoint)
                to_kind = "register_capture"
                to_regex = exact_regex(endpoint_signal)
            else:
                unsupported.append({
                    "path_index": path_index,
                    "select_bit": select_bit,
                    "reason": f"unsupported_endpoint_kind:{endpoint_kind}",
                })
                continue

            for branch_name in (
                "mux_a",
                "mux_b",
            ):

                branch = (
                    witness.get(
                        "branches",
                        {},
                    ).get(
                        branch_name
                    )
                    or {}
                )

                if (
                    branch.get("status")
                    != "ready"
                ):
                    continue

                case_analysis = []

                for item in (
                    branch.get(
                        "case_analysis"
                    )
                    or []
                ):

                    bit = int(
                        item["bit"]
                    )

                    if bit == launch_bit:
                        raise RuntimeError(
                            "launch bit appears in "
                            "physical case analysis"
                        )

                    signal = norm(
                        item["signal"]
                    )

                    value = int(
                        item["value"]
                    )

                    if value not in (0, 1):
                        raise RuntimeError(
                            "case value must be 0/1"
                        )

                    case_analysis.append(
                        {
                            "bit": bit,
                            "signal": signal,
                            "port_regex": (
                                exact_regex(signal)
                            ),
                            "value": value,
                        }
                    )

                requests.append(
                    {
                        "witness_id": (
                            f"c{len(requests):04d}"
                        ),
                        "path_index": (
                            path_index
                        ),
                        "select_bit": (
                            select_bit
                        ),
                        "launch_bit": (
                            launch_bit
                        ),
                        "branch": (
                            branch_name
                        ),
                        "select_value": (
                            branch.get(
                                "select_value"
                            )
                        ),
                        "derivation_status": (
                            "ready"
                        ),
                        "from_kind": (
                            "primary_input"
                        ),
                        "from_signal": (
                            launch_signal
                        ),
                        "from_regex": exact_regex(launch_signal),
                        "from_port_regex": exact_regex(launch_signal),
                        "to_kind": to_kind,
                        "to_signal": endpoint_signal,
                        "to_regex": to_regex,
                        "to_port_regex": to_regex if to_kind == "primary_output" else None,
                        "to_pin_regex": to_regex if to_kind == "register_capture" else None,
                        "case_analysis": (
                            case_analysis
                        ),
                    }
                )

    document = {
        "schema": SCHEMA,
        "design": graph.get(
            "design"
        ),
        "platform": graph.get(
            "platform"
        ),
        "flow_variant": graph.get(
            "flow_variant"
        ),
        "requests": requests,
        "summary": {
            "request_count": len(
                requests
            ),
            "ready_count": len(requests),
            "unsupported_count": len(unsupported),
        },
        "unsupported": unsupported,
        "source_case_witness": str(case_path),
        "source_case_witness_sha256": sha256_file(case_path),
        "source_mapped_json": str(mapped_path),
        "source_mapped_sha256": sha256_file(mapped_path),
        "source_rtl_graph": str(graph_path),
        "source_rtl_graph_sha256": sha256_file(graph_path),
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

    print(
        json.dumps(
            document["summary"],
            indent=2,
        )
    )

    for request in requests:
        rendered = " ".join(
            f"{x['signal']}={x['value']}"
            for x in request[
                "case_analysis"
            ]
        )

        print(
            request["witness_id"],
            f"path={request['path_index']}",
            f"select={request['select_bit']}",
            request["branch"],
            f"{request['from_signal']} -> "
            f"{request['to_signal']}",
            rendered,
        )


if __name__ == "__main__":
    main()
