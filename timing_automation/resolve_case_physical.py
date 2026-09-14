#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Tuple


SCHEMA = "nebula_case_physical_resolution/1"


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
        raise RuntimeError(
            f"expected JSON object: {path}"
        )

    return value


def select_one_path(
    doc: Dict[str, Any],
    source: Path,
) -> Dict[str, Any]:

    paths = doc.get("paths") or []

    if len(paths) != 1:
        raise RuntimeError(
            f"{source}: expected exactly "
            f"one timing path, got {len(paths)}"
        )

    if not isinstance(paths[0], dict):
        raise RuntimeError(
            f"{source}: timing path is not an object"
        )

    return paths[0]


def cell_type(
    cell: Dict[str, Any],
) -> str:
    return str(
        cell.get("type")
        or cell.get("cell_type")
        or cell.get("cell")
        or ""
    ).strip()


def physical_signature(
    path: Dict[str, Any],
) -> List[Tuple[Any, Any, Any]]:

    signature = []

    for cell in (
        path.get("cells")
        or []
    ):
        if not isinstance(cell, dict):
            continue

        instance = cell.get(
            "instance"
        )

        if not instance:
            continue

        signature.append(
            (
                instance,
                cell_type(cell),
                cell.get("output_pin"),
            )
        )

    return signature


def common_suffix_length(
    a: List[Tuple[Any, Any, Any]],
    b: List[Tuple[Any, Any, Any]],
) -> int:

    count = 0

    ia = len(a) - 1
    ib = len(b) - 1

    while (
        ia >= 0
        and ib >= 0
        and a[ia] == b[ib]
    ):
        count += 1
        ia -= 1
        ib -= 1

    return count


def normalized_boundary(
    path: Dict[str, Any],
) -> Tuple[Any, Any]:

    return (
        path.get("startpoint")
        or path.get("startpoint_raw"),
        path.get("endpoint")
        or path.get("endpoint_raw"),
    )


def main() -> None:

    ap = argparse.ArgumentParser(
        description=(
            "Resolve case-analysis physical witnesses "
            "using fail-closed exact physical signatures."
        )
    )

    ap.add_argument(
        "--original-timing",
        required=True,
    )

    ap.add_argument(
        "--case-request",
        required=True,
    )

    ap.add_argument(
        "--parsed-dir",
        required=True,
    )

    ap.add_argument(
        "--output",
        required=True,
    )

    args = ap.parse_args()

    original_path = Path(
        args.original_timing
    ).resolve()

    request_path = Path(
        args.case_request
    ).resolve()

    parsed_dir = Path(
        args.parsed_dir
    ).resolve()

    output_path = Path(
        args.output
    ).resolve()

    original_doc = load_json(
        original_path
    )

    request_doc = load_json(
        request_path
    )

    if (
        request_doc.get("schema")
        != "nebula_case_witness_request/1"
    ):
        raise RuntimeError(
            "unexpected case-witness request schema"
        )

    original_paths = (
        original_doc.get("paths")
        or []
    )

    if not isinstance(
        original_paths,
        list,
    ):
        raise RuntimeError(
            "original timing JSON has no paths list"
        )

    grouped: Dict[
        int,
        Dict[str, Dict[str, Any]],
    ] = defaultdict(dict)

    for request in (
        request_doc.get("requests")
        or []
    ):
        if not isinstance(
            request,
            dict,
        ):
            continue

        if (
            request.get("derivation_status")
            != "ready"
        ):
            continue

        path_index = request.get(
            "path_index"
        )

        branch = request.get(
            "branch"
        )

        witness_id = request.get(
            "witness_id"
        )

        if not isinstance(
            path_index,
            int,
        ):
            continue

        if branch not in (
            "mux_a",
            "mux_b",
        ):
            continue

        if not isinstance(
            witness_id,
            str,
        ):
            continue

        timing_file = (
            parsed_dir
            / f"{witness_id}.json"
        )

        if not timing_file.is_file():
            raise RuntimeError(
                f"missing parsed case witness: "
                f"{timing_file}"
            )

        timing_doc = load_json(
            timing_file
        )

        timing_path = select_one_path(
            timing_doc,
            timing_file,
        )

        if branch in grouped[
            path_index
        ]:
            raise RuntimeError(
                f"path {path_index}: duplicate "
                f"case witness for {branch}"
            )

        grouped[
            path_index
        ][
            branch
        ] = {
            "witness_id": (
                witness_id
            ),
            "request": (
                request
            ),
            "timing_path": (
                timing_path
            ),
            "signature": (
                physical_signature(
                    timing_path
                )
            ),
        }

    results = []

    for path_index in sorted(
        grouped
    ):
        if not (
            0
            <= path_index
            < len(original_paths)
        ):
            raise RuntimeError(
                f"path index out of range: "
                f"{path_index}"
            )

        original = (
            original_paths[
                path_index
            ]
        )

        if not isinstance(
            original,
            dict,
        ):
            raise RuntimeError(
                f"path {path_index}: "
                "original path is invalid"
            )

        original_sig = (
            physical_signature(
                original
            )
        )

        branches = grouped[
            path_index
        ]

        a = branches.get(
            "mux_a"
        )

        b = branches.get(
            "mux_b"
        )

        base = {
            "path_index": (
                path_index
            ),
            "startpoint": (
                normalized_boundary(
                    original
                )[0]
            ),
            "endpoint": (
                normalized_boundary(
                    original
                )[1]
            ),
            "original_signature": [
                list(x)
                for x in original_sig
            ],
        }

        if a is None or b is None:
            results.append(
                {
                    **base,
                    "status": "unresolved",
                    "resolution_method": (
                        "case_analysis_incomplete_pair"
                    ),
                    "selected_branch": None,
                    "branch_selection_required": True,
                    "reason": (
                        "both conditioned branches "
                        "were not available"
                    ),
                }
            )
            continue

        original_boundary = (
            normalized_boundary(
                original
            )
        )

        a_boundary = (
            normalized_boundary(
                a["timing_path"]
            )
        )

        b_boundary = (
            normalized_boundary(
                b["timing_path"]
            )
        )

        if (
            a_boundary
            != original_boundary
            or b_boundary
            != original_boundary
        ):
            results.append(
                {
                    **base,
                    "status": "unresolved",
                    "resolution_method": (
                        "case_analysis_boundary_mismatch"
                    ),
                    "selected_branch": None,
                    "branch_selection_required": True,
                    "reason": (
                        "conditioned physical path "
                        "does not preserve original "
                        "startpoint/endpoint"
                    ),
                }
            )
            continue

        a_sig = a[
            "signature"
        ]

        b_sig = b[
            "signature"
        ]

        original_matches_a = (
            original_sig
            == a_sig
        )

        original_matches_b = (
            original_sig
            == b_sig
        )

        branches_match = (
            a_sig
            == b_sig
        )

        suffix_a = (
            common_suffix_length(
                original_sig,
                a_sig,
            )
        )

        suffix_b = (
            common_suffix_length(
                original_sig,
                b_sig,
            )
        )

        evidence = {
            "original_matches": {
                "mux_a": (
                    original_matches_a
                ),
                "mux_b": (
                    original_matches_b
                ),
            },
            "conditioned_branches_match": (
                branches_match
            ),
            "common_suffix_length": {
                "mux_a": suffix_a,
                "mux_b": suffix_b,
            },
            "original_cell_count": (
                len(original_sig)
            ),
            "mux_a_cell_count": (
                len(a_sig)
            ),
            "mux_b_cell_count": (
                len(b_sig)
            ),
            "mux_a_witness_id": (
                a["witness_id"]
            ),
            "mux_b_witness_id": (
                b["witness_id"]
            ),
        }

        # ----------------------------------------------------
        # TRUSTED resolution rules.
        #
        # Exact equality only.
        # Suffix evidence is diagnostic and MUST NOT resolve.
        # ----------------------------------------------------

        if (
            original_matches_a
            and original_matches_b
        ):
            result = {
                **base,
                "status": "resolved",
                "resolution_method": (
                    "case_analysis_shared_"
                    "physical_implementation"
                ),
                "selected_branch": None,
                "branch_selection_required": False,
                "reason": (
                    "both conditioned branches "
                    "exactly match the original "
                    "physical path"
                ),
                "evidence": evidence,
            }

        elif (
            original_matches_a
            and not original_matches_b
        ):
            result = {
                **base,
                "status": "resolved",
                "resolution_method": (
                    "case_analysis_exact_"
                    "physical_match"
                ),
                "selected_branch": (
                    "mux_a"
                ),
                "branch_selection_required": True,
                "reason": (
                    "original physical path "
                    "exactly matches mux_a only"
                ),
                "evidence": evidence,
            }

        elif (
            original_matches_b
            and not original_matches_a
        ):
            result = {
                **base,
                "status": "resolved",
                "resolution_method": (
                    "case_analysis_exact_"
                    "physical_match"
                ),
                "selected_branch": (
                    "mux_b"
                ),
                "branch_selection_required": True,
                "reason": (
                    "original physical path "
                    "exactly matches mux_b only"
                ),
                "evidence": evidence,
            }

        else:
            result = {
                **base,
                "status": "unresolved",
                "resolution_method": (
                    "case_analysis_no_exact_match"
                ),
                "selected_branch": None,
                "branch_selection_required": True,
                "reason": (
                    "conditioned paths do not "
                    "exactly identify the original "
                    "physical path"
                ),
                "evidence": evidence,
            }

        results.append(
            result
        )

    selected_a = sum(
        1
        for r in results
        if (
            r.get("status")
            == "resolved"
            and r.get(
                "selected_branch"
            )
            == "mux_a"
        )
    )

    selected_b = sum(
        1
        for r in results
        if (
            r.get("status")
            == "resolved"
            and r.get(
                "selected_branch"
            )
            == "mux_b"
        )
    )

    shared = sum(
        1
        for r in results
        if (
            r.get("status")
            == "resolved"
            and r.get(
                "branch_selection_required"
            )
            is False
        )
    )

    unresolved = sum(
        1
        for r in results
        if r.get("status")
        != "resolved"
    )

    document = {
        "schema": SCHEMA,
        "design": (
            request_doc.get(
                "design"
            )
        ),
        "platform": (
            request_doc.get(
                "platform"
            )
        ),
        "flow_variant": (
            request_doc.get(
                "flow_variant"
            )
        ),
        "source_original_timing": str(original_path),
        "source_original_timing_sha256": sha256_file(original_path),
        "source_case_request": str(request_path),
        "source_case_request_sha256": sha256_file(request_path),
        "parsed_dir": str(parsed_dir),
        "results": results,
        "summary": {
            "path_count": (
                len(results)
            ),
            "resolved_count": (
                len(results)
                - unresolved
            ),
            "selected_mux_a_count": (
                selected_a
            ),
            "selected_mux_b_count": (
                selected_b
            ),
            "shared_count": (
                shared
            ),
            "unresolved_count": (
                unresolved
            ),
        },
        "policy": {
            "exact_signature_required": (
                True
            ),
            "suffix_evidence_can_resolve": (
                False
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
    print("=" * 78)
    print("CASE PHYSICAL RESOLUTION")
    print("=" * 78)

    for r in results:
        evidence = (
            r.get("evidence")
            or {}
        )

        matches = (
            evidence.get(
                "original_matches"
            )
            or {}
        )

        suffix = (
            evidence.get(
                "common_suffix_length"
            )
            or {}
        )

        print(
            f"path={r['path_index']:<3} "
            f"status={r['status']:<10} "
            f"selected="
            f"{str(r.get('selected_branch')):<6} "
            f"A={str(matches.get('mux_a')):<5} "
            f"B={str(matches.get('mux_b')):<5} "
            f"suffixA="
            f"{str(suffix.get('mux_a')):<3} "
            f"suffixB="
            f"{str(suffix.get('mux_b')):<3} "
            f"{r['resolution_method']}"
        )

    print()
    print("SUMMARY")
    print(
        json.dumps(
            document["summary"],
            indent=2,
        )
    )

    print()
    print(
        "Policy: suffix-only evidence "
        "does NOT resolve a branch."
    )

    print(
        f"Output: {output_path}"
    )

    print("=" * 78)


if __name__ == "__main__":
    main()
