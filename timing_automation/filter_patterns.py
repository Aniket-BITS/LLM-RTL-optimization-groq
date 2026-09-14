#!/usr/bin/env python3
"""
filter_patterns.py

Deterministically filter the RTL optimization pattern library for exactly
one optimization unit.

P0 guarantees
-------------
1. --unit-id is mandatory.
2. No "first optimization-ready unit" fallback.
3. No region fallback.
4. No aggregation across unrelated critical paths.
5. If structural_summary is incomplete, only THIS unit's affected_paths
   may provide fallback classification evidence.
6. Only patterns allowed by this unit and the global optimization policy
   are exposed to the LLM.
7. Output records which unit produced the eligible-pattern set.

Example
-------

python3 timing_automation/filter_patterns.py \
    --optimizer-input ai_runs/<variant>_optimizer_input.json \
    --unit-id unit_000 \
    --patterns-dir timing_automation/patterns \
    --output ai_runs/<variant>_unit_000_eligible_patterns.json

Self-test
---------

python3 timing_automation/filter_patterns.py --self-test
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence, Set, Tuple


# ============================================================
# Paths
# ============================================================

SCRIPT_DIR = Path(__file__).resolve().parent

DEFAULT_PATTERNS_DIR = (
    SCRIPT_DIR
    / "patterns"
)


# ============================================================
# Exceptions
# ============================================================

class PatternFilterError(RuntimeError):
    """Raised when pattern eligibility cannot be determined safely."""


# ============================================================
# JSON helpers
# ============================================================

def load_json(
    path: Path,
) -> Dict[str, Any]:

    path = Path(path)

    if not path.is_file():
        raise PatternFilterError(
            f"JSON file not found: {path}"
        )

    try:
        value = json.loads(
            path.read_text(
                encoding="utf-8"
            )
        )

    except json.JSONDecodeError as exc:
        raise PatternFilterError(
            f"invalid JSON in {path}: {exc}"
        ) from exc

    if not isinstance(value, dict):
        raise PatternFilterError(
            f"JSON root must be an object: {path}"
        )

    return value


def write_json(
    path: Path,
    value: Mapping[str, Any],
) -> None:

    path = Path(path)

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    path.write_text(
        json.dumps(
            value,
            indent=2,
            sort_keys=False,
        )
        + "\n",
        encoding="utf-8",
    )


# ============================================================
# Pattern library
# ============================================================

def load_patterns(
    patterns_dir: Path,
) -> List[Dict[str, Any]]:
    """
    Load every pattern JSON except pattern.schema.json.

    Duplicate IDs are rejected.
    """

    patterns_dir = Path(
        patterns_dir
    )

    if not patterns_dir.is_dir():
        raise PatternFilterError(
            f"patterns directory not found: "
            f"{patterns_dir}"
        )

    patterns: List[
        Dict[str, Any]
    ] = []

    seen_ids: Set[str] = set()

    for path in sorted(
        patterns_dir.glob(
            "*.json"
        )
    ):

        if path.name == "pattern.schema.json":
            continue

        try:

            value = json.loads(
                path.read_text(
                    encoding="utf-8"
                )
            )

        except json.JSONDecodeError as exc:

            raise PatternFilterError(
                f"malformed pattern "
                f"{path.name}: {exc}"
            ) from exc

        if not isinstance(
            value,
            dict,
        ):
            raise PatternFilterError(
                f"pattern {path.name} "
                "must contain a JSON object"
            )

        pattern_id = value.get(
            "id"
        )

        if (
            not isinstance(
                pattern_id,
                str,
            )
            or not pattern_id
        ):
            raise PatternFilterError(
                f"pattern {path.name} "
                "has no valid id"
            )

        if pattern_id in seen_ids:
            raise PatternFilterError(
                f"duplicate pattern id: "
                f"{pattern_id!r}"
            )

        seen_ids.add(
            pattern_id
        )

        patterns.append(
            value
        )

    if not patterns:
        raise PatternFilterError(
            f"no pattern JSON files found in "
            f"{patterns_dir}"
        )

    return patterns


# ============================================================
# Unit selection
# ============================================================

def select_unit(
    optimizer_input: Mapping[str, Any],
    unit_id: str,
) -> Dict[str, Any]:
    """
    Select exactly one optimization unit.

    There is intentionally no automatic fallback.
    """

    units = (
        optimizer_input.get(
            "optimization_units"
        )
        or []
    )

    if not isinstance(
        units,
        list,
    ):
        raise PatternFilterError(
            "optimization_units must be an array"
        )

    matches = [
        unit
        for unit in units
        if isinstance(
            unit,
            dict,
        )
        and unit.get(
            "unit_id"
        ) == unit_id
    ]

    if not matches:

        available = [
            unit.get(
                "unit_id"
            )
            for unit in units
            if isinstance(
                unit,
                dict,
            )
            and isinstance(
                unit.get(
                    "unit_id"
                ),
                str,
            )
        ]

        raise PatternFilterError(
            f"optimization unit "
            f"{unit_id!r} not found. "
            "Available units: "
            + (
                ", ".join(
                    available
                )
                if available
                else "(none)"
            )
        )

    if len(matches) != 1:
        raise PatternFilterError(
            f"optimization unit "
            f"{unit_id!r} appears "
            "more than once"
        )

    return dict(
        matches[0]
    )


def require_unit_ready(
    unit: Mapping[str, Any],
) -> None:

    eligibility = (
        unit.get(
            "eligibility"
        )
        or {}
    )

    if not isinstance(
        eligibility,
        dict,
    ):
        raise PatternFilterError(
            "unit eligibility must be an object"
        )

    ready = eligibility.get("request_ready")
    if ready is None:
        ready = eligibility.get("optimization_ready", False)
    if not ready:

        blockers = (
            eligibility.get(
                "blockers"
            )
            or []
        )

        raise PatternFilterError(
            f"unit {unit.get('unit_id')!r} "
            "is not request-ready"
            + (
                ": "
                + "; ".join(
                    str(x)
                    for x in blockers
                )
                if blockers
                else ""
            )
        )


# ============================================================
# Unit-only classification fallback
# ============================================================

def critical_path_index(
    optimizer_input: Mapping[str, Any],
) -> Dict[str, Dict[str, Any]]:

    paths = (
        optimizer_input.get(
            "critical_paths"
        )
        or []
    )

    if not isinstance(
        paths,
        list,
    ):
        raise PatternFilterError(
            "critical_paths must be an array"
        )

    result: Dict[
        str,
        Dict[str, Any]
    ] = {}

    for path in paths:

        if not isinstance(
            path,
            dict,
        ):
            continue

        path_id = path.get(
            "path_id"
        )

        if not isinstance(
            path_id,
            str,
        ):
            continue

        if path_id in result:
            raise PatternFilterError(
                f"duplicate critical path ID: "
                f"{path_id!r}"
            )

        result[path_id] = path

    return result


def unit_path_classification(
    optimizer_input: Mapping[str, Any],
    unit: Mapping[str, Any],
) -> Tuple[
    Set[str],
    Set[str],
]:
    """
    Return classes/patterns derived ONLY from unit.affected_paths.
    """

    affected_paths = (
        unit.get(
            "affected_paths"
        )
        or []
    )

    if not isinstance(
        affected_paths,
        list,
    ):
        raise PatternFilterError(
            "unit affected_paths must be an array"
        )

    index = critical_path_index(
        optimizer_input
    )

    classes: Set[str] = set()

    patterns: Set[str] = set()

    for path_id in affected_paths:

        if not isinstance(
            path_id,
            str,
        ):
            raise PatternFilterError(
                "unit affected_paths contains "
                "a non-string path ID"
            )

        path = index.get(
            path_id
        )

        if path is None:
            raise PatternFilterError(
                f"unit references missing "
                f"critical path {path_id!r}"
            )

        classification = (
            path.get(
                "classification"
            )
            or {}
        )

        if not isinstance(
            classification,
            dict,
        ):
            continue

        class_name = (
            classification.get(
                "class"
            )
        )

        pattern_name = (
            classification.get(
                "pattern"
            )
        )

        if isinstance(
            class_name,
            str,
        ) and class_name:

            classes.add(
                class_name
            )

        if isinstance(
            pattern_name,
            str,
        ) and pattern_name:

            patterns.add(
                pattern_name
            )

    return (
        classes,
        patterns,
    )


# ============================================================
# Unit facets
# ============================================================

def unit_facets(
    optimizer_input: Mapping[str, Any],
    unit_id: str,
) -> Dict[str, Any]:
    """
    Build pattern-filtering facets for EXACTLY one unit.
    """

    unit = select_unit(
        optimizer_input,
        unit_id,
    )

    require_unit_ready(
        unit
    )

    policy = (
        (
            optimizer_input.get(
                "constraints"
            )
            or {}
        ).get(
            "optimization_policy"
        )
        or {}
    )

    if not isinstance(
        policy,
        dict,
    ):
        raise PatternFilterError(
            "optimization_policy must be an object"
        )

    allowed_families = set(
        unit.get(
            "allowed_transformation_families"
        )
        or []
    )

    # Empty recommendation families are legal. They cause the guided layer to
    # exhaust naturally and the closure driver to fall back to exploratory mode.

    structural_summary = (
        unit.get(
            "structural_summary"
        )
        or {}
    )

    if not isinstance(
        structural_summary,
        dict,
    ):
        raise PatternFilterError(
            "unit structural_summary "
            "must be an object"
        )

    # --------------------------------------------------------
    # Prefer unit summary
    # --------------------------------------------------------

    classes = set(
        structural_summary.get(
            "classes"
        )
        or []
    )

    patterns = set(
        structural_summary.get(
            "patterns"
        )
        or []
    )

    # Older/smaller unit summaries may use singular names.
    if not classes:

        singular_class = (
            structural_summary.get(
                "class"
            )
        )

        if isinstance(
            singular_class,
            str,
        ) and singular_class:

            classes.add(
                singular_class
            )

    if not patterns:

        singular_pattern = (
            structural_summary.get(
                "pattern"
            )
        )

        if isinstance(
            singular_pattern,
            str,
        ) and singular_pattern:

            patterns.add(
                singular_pattern
            )

    # --------------------------------------------------------
    # Fallback ONLY to this unit's paths
    # --------------------------------------------------------

    if not classes or not patterns:

        path_classes, path_patterns = (
            unit_path_classification(
                optimizer_input,
                unit,
            )
        )

        if not classes:
            classes.update(
                path_classes
            )

        if not patterns:
            patterns.update(
                path_patterns
            )

    return {
        "unit_id": unit_id,
        "optimizer_mode": unit.get("optimizer_mode") or (unit.get("eligibility") or {}).get("optimizer_mode") or "guided",
        "policy": dict(
            policy
        ),
        "allowed_families": (
            allowed_families
        ),
        "classes": classes,
        "patterns": patterns,
        "affected_paths": list(
            unit.get(
                "affected_paths"
            )
            or []
        ),
        "member_regions": list(
            unit.get(
                "member_regions"
            )
            or []
        ),
    }


# ============================================================
# Eligibility
# ============================================================

def is_eligible(
    pattern: Mapping[str, Any],
    facets: Mapping[str, Any],
) -> Tuple[
    bool,
    List[str],
]:
    """
    Determine whether one optimization pattern may be offered.
    """

    reasons: List[str] = []

    policy = facets[
        "policy"
    ]

    allowed_families = facets[
        "allowed_families"
    ]

    unit_classes = facets[
        "classes"
    ]

    unit_patterns = facets[
        "patterns"
    ]

    family = pattern.get(
        "family"
    )

    # --------------------------------------------------------
    # Family gate
    # --------------------------------------------------------

    if family not in allowed_families:

        reasons.append(
            f"family {family!r} not in "
            "allowed_transformation_families"
        )

    # --------------------------------------------------------
    # Latency gate
    # --------------------------------------------------------

    latency_effect = (
        pattern.get(
            "latency_effect"
        )
    )

    if (
        latency_effect == "changes"
        and not policy.get(
            "allow_latency_change",
            False,
        )
    ):

        reasons.append(
            "latency-changing pattern blocked "
            "because allow_latency_change is false"
        )

    # --------------------------------------------------------
    # applies_when
    # --------------------------------------------------------

    applies_when = (
        pattern.get(
            "applies_when"
        )
        or {}
    )

    if not isinstance(
        applies_when,
        dict,
    ):
        reasons.append(
            "applies_when is not an object"
        )

        return (
            False,
            reasons,
        )

    # --------------------------------------------------------
    # Required policy flags
    # --------------------------------------------------------

    required_flags = (
        applies_when.get(
            "requires_flags"
        )
        or []
    )

    for flag in required_flags:

        if not isinstance(
            flag,
            str,
        ):
            reasons.append(
                "requires_flags contains "
                "a non-string entry"
            )

            continue

        if not policy.get(
            flag,
            False,
        ):

            reasons.append(
                f"required policy flag "
                f"not enabled: {flag}"
            )

    # --------------------------------------------------------
    # Structural class gate
    # --------------------------------------------------------

    wanted_classes = set(
        applies_when.get(
            "class"
        )
        or []
    )

    if (
        wanted_classes
        and not (
            wanted_classes
            & unit_classes
        )
    ):

        reasons.append(
            "class "
            f"{sorted(wanted_classes)} "
            "does not match unit "
            f"{sorted(unit_classes)}"
        )

    # --------------------------------------------------------
    # Structural pattern gate
    # --------------------------------------------------------

    wanted_patterns = set(
        applies_when.get(
            "pattern_in"
        )
        or []
    )

    if (
        wanted_patterns
        and not (
            wanted_patterns
            & unit_patterns
        )
    ):

        reasons.append(
            "pattern "
            f"{sorted(wanted_patterns)} "
            "does not match unit "
            f"{sorted(unit_patterns)}"
        )

    return (
        len(reasons) == 0,
        reasons,
    )


# ============================================================
# Slim LLM view
# ============================================================

def slim_pattern(
    pattern: Mapping[str, Any],
) -> Dict[str, Any]:

    return {
        "id": pattern[
            "id"
        ],
        "name": pattern.get(
            "name"
        ),
        "family": pattern.get(
            "family"
        ),
        "latency_effect": pattern.get(
            "latency_effect"
        ),
        "expected_equivalence_class": (
            pattern.get("expected_equivalence_class")
            or pattern.get("equivalence_class")
        ),
        "sec_recipe": pattern.get(
            "sec_recipe"
        ),
        "template": pattern.get(
            "template"
        ),
        "example": pattern.get(
            "example"
        ),
        "guardrails": list(
            pattern.get(
                "guardrails"
            )
            or []
        ),
        "version": pattern.get(
            "version"
        ),
    }


# ============================================================
# Main filter API
# ============================================================

def filter_patterns(
    optimizer_input: Mapping[str, Any],
    patterns: Sequence[
        Mapping[str, Any]
    ],
    unit_id: str,
    exclude_patterns: Sequence[str] = (),
) -> Dict[str, Any]:

    facets = unit_facets(
        optimizer_input,
        unit_id,
    )

    excluded_ids = {str(x) for x in exclude_patterns if str(x)}

    eligible: List[
        Dict[str, Any]
    ] = []

    rejected: List[
        Dict[str, Any]
    ] = []

    for pattern in patterns:

        if not isinstance(
            pattern,
            Mapping,
        ):
            raise PatternFilterError(
                "pattern library contains "
                "a non-object entry"
            )

        pattern_id = pattern.get(
            "id"
        )

        if not isinstance(
            pattern_id,
            str,
        ):

            raise PatternFilterError(
                "pattern has no valid id"
            )

        if pattern_id in excluded_ids:
            rejected.append({
                "id": pattern_id,
                "reasons": ["excluded_by_history"],
            })
            continue

        ok, reasons = (
            is_eligible(
                pattern,
                facets,
            )
        )

        if ok:

            eligible.append(
                slim_pattern(
                    pattern
                )
            )

        else:

            rejected.append(
                {
                    "id": pattern_id,
                    "reasons": reasons,
                }
            )

    return {
        "schema": "eligible_patterns/2",

        "unit_id": facets[
            "unit_id"
        ],
        "optimizer_mode": facets.get("optimizer_mode", "guided"),

        "affected_path_ids": (
            facets[
                "affected_paths"
            ]
        ),

        "member_region_ids": (
            facets[
                "member_regions"
            ]
        ),

        "unit_classes": sorted(
            facets[
                "classes"
            ]
        ),

        "unit_patterns": sorted(
            facets[
                "patterns"
            ]
        ),

        "allowed_families": sorted(
            facets[
                "allowed_families"
            ]
        ),

        "eligible_patterns": (
            eligible
        ),
        "recommended_patterns": (
            eligible
        ),

        "rejected_patterns": (
            rejected
        ),

        "excluded_pattern_ids": sorted(excluded_ids),
        "exhausted": len(eligible) == 0,
    }


# ============================================================
# Self-test
# ============================================================

def run_self_test() -> None:
    """
    Regression tests proving that unit_001 never inherits classification
    evidence from unit_000.
    """

    optimizer_input = {
        "constraints": {
            "optimization_policy": {
                "allow_latency_change": False,
                "allow_pipeline": False,
                "allow_retiming": False,
            }
        },

        "critical_paths": [
            {
                "path_id": "path_A",
                "classification": {
                    "class": "arithmetic",
                    "pattern": "adder",
                },
            },
            {
                "path_id": "path_B",
                "classification": {
                    "class": "mux_control",
                    "pattern": "priority_mux",
                },
            },
        ],

        "optimization_units": [
            {
                "unit_id": "unit_000",

                "eligibility": {
                    "optimization_ready": True,
                    "blockers": [],
                },

                "affected_paths": [
                    "path_A"
                ],

                "member_regions": [
                    "region_A"
                ],

                "allowed_transformation_families": [
                    "arithmetic"
                ],

                "structural_summary": {
                    "classes": [
                        "arithmetic"
                    ],
                    "patterns": [
                        "adder"
                    ],
                },
            },

            {
                "unit_id": "unit_001",

                "eligibility": {
                    "optimization_ready": True,
                    "blockers": [],
                },

                "affected_paths": [
                    "path_B"
                ],

                "member_regions": [
                    "region_B"
                ],

                "allowed_transformation_families": [
                    "mux"
                ],

                # Deliberately thin summary.
                #
                # Fallback MUST use path_B only.
                "structural_summary": {},
            },
        ],
    }

    patterns = [
        {
            "id": "arith.adder_test",
            "name": "Adder test",
            "family": "arithmetic",
            "latency_effect": "preserves",

            "applies_when": {
                "class": [
                    "arithmetic"
                ],
                "pattern_in": [
                    "adder"
                ],
                "requires_flags": [],
            },
        },

        {
            "id": "mux.priority_test",
            "name": "Mux test",
            "family": "mux",
            "latency_effect": "preserves",

            "applies_when": {
                "class": [
                    "mux_control"
                ],
                "pattern_in": [
                    "priority_mux"
                ],
                "requires_flags": [],
            },
        },

        {
            "id": "mux.pipeline_test",
            "name": "Latency-changing mux",
            "family": "mux",
            "latency_effect": "changes",

            "applies_when": {
                "class": [
                    "mux_control"
                ],
                "requires_flags": [],
            },
        },
    ]

    # --------------------------------------------------------
    # Select unit_001 explicitly
    # --------------------------------------------------------

    result = filter_patterns(
        optimizer_input,
        patterns,
        "unit_001",
    )

    assert (
        result[
            "unit_id"
        ]
        == "unit_001"
    )

    # --------------------------------------------------------
    # Fallback classification must come ONLY from path_B.
    # --------------------------------------------------------

    assert (
        result[
            "unit_classes"
        ]
        == [
            "mux_control"
        ]
    )

    assert (
        result[
            "unit_patterns"
        ]
        == [
            "priority_mux"
        ]
    )

    assert (
        result[
            "affected_path_ids"
        ]
        == [
            "path_B"
        ]
    )

    assert (
        result[
            "member_region_ids"
        ]
        == [
            "region_B"
        ]
    )

    # --------------------------------------------------------
    # Only mux pattern is eligible.
    #
    # Arithmetic belongs to unit_000.
    # Pipeline changes latency and must remain blocked.
    # --------------------------------------------------------

    eligible_ids = [
        pattern[
            "id"
        ]
        for pattern in result[
            "eligible_patterns"
        ]
    ]

    assert eligible_ids == [
        "mux.priority_test"
    ]

    rejected_ids = {
        item[
            "id"
        ]
        for item in result[
            "rejected_patterns"
        ]
    }

    assert (
        "arith.adder_test"
        in rejected_ids
    )

    assert (
        "mux.pipeline_test"
        in rejected_ids
    )

    # --------------------------------------------------------
    # unit_000 should produce the opposite result.
    # --------------------------------------------------------

    result_a = filter_patterns(
        optimizer_input,
        patterns,
        "unit_000",
    )

    eligible_a = [
        pattern[
            "id"
        ]
        for pattern in result_a[
            "eligible_patterns"
        ]
    ]

    assert eligible_a == [
        "arith.adder_test"
    ]

    excluded = filter_patterns(
        optimizer_input,
        patterns,
        "unit_001",
        exclude_patterns=["mux.priority_test"],
    )
    assert excluded["eligible_patterns"] == []
    assert excluded["exhausted"] is True
    assert excluded["excluded_pattern_ids"] == ["mux.priority_test"]

    # --------------------------------------------------------
    # Unknown unit rejected.
    # --------------------------------------------------------

    unknown_rejected = False

    try:

        filter_patterns(
            optimizer_input,
            patterns,
            "unit_999",
        )

    except PatternFilterError:
        unknown_rejected = True

    assert unknown_rejected

    # --------------------------------------------------------
    # Non-ready unit rejected.
    # --------------------------------------------------------

    blocked_input = json.loads(
        json.dumps(
            optimizer_input
        )
    )

    blocked_input[
        "optimization_units"
    ][1][
        "eligibility"
    ] = {
        "optimization_ready": False,
        "blockers": [
            "unsafe source mapping"
        ],
    }

    blocked_rejected = False

    try:

        filter_patterns(
            blocked_input,
            patterns,
            "unit_001",
        )

    except PatternFilterError:
        blocked_rejected = True

    assert blocked_rejected

    print(
        "PASS: filter_patterns self-test"
    )

    print(
        "PASS: --unit-id selects exact unit"
    )

    print(
        "PASS: automatic first-unit selection removed"
    )

    print(
        "PASS: unrelated critical-path classifications excluded"
    )

    print(
        "PASS: unit-only classification fallback works"
    )

    print(
        "PASS: transformation-family gate works"
    )

    print(
        "PASS: latency-changing patterns blocked by policy"
    )

    print(
        "PASS: unknown unit rejected"
    )

    print(
        "PASS: non-ready unit rejected"
    )
    print(
        "PASS: deterministic history exclusions and exhausted flag work"
    )


# ============================================================
# CLI
# ============================================================

def parse_args() -> argparse.Namespace:

    parser = argparse.ArgumentParser(
        description=(
            "Filter the optimization pattern "
            "library for one exact optimization unit."
        )
    )

    parser.add_argument(
        "--optimizer-input",
        help=(
            "optimizer_input.json"
        ),
    )

    parser.add_argument(
        "--unit-id",
        help=(
            "exact optimization unit, "
            "for example unit_000"
        ),
    )

    parser.add_argument(
        "--patterns-dir",
        default=str(
            DEFAULT_PATTERNS_DIR
        ),
    )

    parser.add_argument(
        "--output",
        default=None,
    )

    parser.add_argument(
        "--exclude-patterns",
        default="",
        help="Comma-separated pattern ids to exclude deterministically (e.g. prior valid rejections).",
    )

    parser.add_argument(
        "--verbose",
        action="store_true",
        help=(
            "include rejected-pattern "
            "reasons in terminal output"
        ),
    )

    parser.add_argument(
        "--self-test",
        action="store_true",
    )

    return parser.parse_args()


def main() -> int:

    args = parse_args()

    # --------------------------------------------------------
    # Self-test
    # --------------------------------------------------------

    if args.self_test:

        try:

            run_self_test()

        except Exception as exc:

            print(
                f"SELF-TEST FAILED: {exc}",
                file=sys.stderr,
            )

            return 1

        return 0

    # --------------------------------------------------------
    # Required CLI arguments
    # --------------------------------------------------------

    missing = []

    if not args.optimizer_input:
        missing.append(
            "--optimizer-input"
        )

    if not args.unit_id:
        missing.append(
            "--unit-id"
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

    # --------------------------------------------------------
    # Run filter
    # --------------------------------------------------------

    try:

        optimizer_input = load_json(
            Path(
                args.optimizer_input
            )
        )

        patterns = load_patterns(
            Path(
                args.patterns_dir
            )
        )

        excluded = [x.strip() for x in str(args.exclude_patterns).split(",") if x.strip()]
        result = filter_patterns(
            optimizer_input,
            patterns,
            args.unit_id,
            exclude_patterns=excluded,
        )

        if args.output:

            write_json(
                Path(
                    args.output
                ),
                result,
            )

    except PatternFilterError as exc:

        print(
            f"ERROR: {exc}",
            file=sys.stderr,
        )

        return 2

    # --------------------------------------------------------
    # Terminal summary
    # --------------------------------------------------------

    summary = {
        "unit_id": result[
            "unit_id"
        ],

        "unit_classes": result[
            "unit_classes"
        ],

        "unit_patterns": result[
            "unit_patterns"
        ],

        "allowed_families": result[
            "allowed_families"
        ],

        "affected_path_ids": result[
            "affected_path_ids"
        ],

        "exhausted": result.get("exhausted", False),
        "excluded_pattern_ids": result.get("excluded_pattern_ids", []),

        "eligible": [
            pattern[
                "id"
            ]
            for pattern in result[
                "eligible_patterns"
            ]
        ],

        "rejected": (
            result[
                "rejected_patterns"
            ]
            if args.verbose
            else len(
                result[
                    "rejected_patterns"
                ]
            )
        ),
    }

    print(
        json.dumps(
            summary,
            indent=2,
        )
    )

    if not result[
        "eligible_patterns"
    ]:

        print(
            "WARNING: no eligible patterns "
            f"for {args.unit_id}.",
            file=sys.stderr,
        )

        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(
        main()
    )