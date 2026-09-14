#!/usr/bin/env python3
# new! classify! new! classify!

"""
Classify mapped RTL timing paths by structural bottleneck type.

Pipeline:

    parse_timing.py
        ↓
    build_rtl_graph.py
        ↓
    map_to_rtl.py
        ↓
    [optional]
    narrow_reconvergence.py
        ↓
    classify_path.py

The classifier operates on RTL/Yosys operators, NOT physical
standard-cell names.

Examples:

    {$add, $mux}
        -> structural_class = arithmetic
        -> pattern = adder_followed_by_mux

    {$xor, $mux}
        -> structural_class = bitwise
        -> pattern = bitwise_logic_followed_by_mux

    {$mux, $mux, $mux}
        -> structural_class = muxing
        -> pattern = mux_chain

If map_to_rtl.py reports genuine logical reconvergence, classification
is considered unresolved unless a matching narrow_reconvergence.py
result is supplied.

Important:
    "primary_bottleneck" here means the primary RTL structural class.
    It is NOT a claim that this operator contributes the largest
    physical cell delay.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_FLOW_DIR = SCRIPT_DIR.parent


# ============================================================================
# Operator groups
# ============================================================================

ADD_OPS = {
    "$add",
}

SUB_OPS = {
    "$sub",
}

ARITHMETIC_OPS = (
    ADD_OPS
    | SUB_OPS
)

MULTIPLY_OPS = {
    "$mul",
}

DIVIDE_OPS = {
    "$div",
    "$mod",
    "$divfloor",
    "$modfloor",
}

MUX_OPS = {
    "$mux",
    "$pmux",
}

COMPARE_OPS = {
    "$eq",
    "$ne",
    "$eqx",
    "$nex",
    "$lt",
    "$le",
    "$gt",
    "$ge",
}

SHIFT_OPS = {
    "$shl",
    "$shr",
    "$sshl",
    "$sshr",
    "$shift",
    "$shiftx",
}

BITWISE_OPS = {
    "$and",
    "$or",
    "$xor",
    "$xnor",
    "$not",
    "$pos",
}

LOGICAL_OPS = {
    "$logic_and",
    "$logic_or",
    "$logic_not",
}

REDUCTION_OPS = {
    "$reduce_and",
    "$reduce_or",
    "$reduce_xor",
    "$reduce_xnor",
    "$reduce_bool",
}

OTHER_KNOWN_OPS = {
    "$neg",
    "$slice",
    "$concat",
}


# ============================================================================
# Utility helpers
# ============================================================================

def load_json(path: Path) -> Dict[str, Any]:

    try:
        data = json.loads(
            path.read_text()
        )

    except json.JSONDecodeError as exc:

        raise ValueError(
            f"Invalid JSON:\n  {path}"
        ) from exc

    if not isinstance(
        data,
        dict,
    ):
        raise ValueError(
            f"Expected top-level JSON object:\n  {path}"
        )

    return data


def resolve_file(
    value: str,
    flow_dir: Path,
) -> Path:

    path = Path(
        value
    ).expanduser()

    if path.is_absolute():

        result = path

    else:

        cwd_path = (
            Path.cwd()
            / path
        )

        flow_path = (
            flow_dir
            / path
        )

        if cwd_path.exists():

            result = cwd_path

        else:

            result = flow_path

    result = result.resolve()

    if not result.exists():

        raise FileNotFoundError(
            f"File not found:\n  {result}"
        )

    return result


def normalized_signal(
    value: Any,
) -> Optional[str]:

    if value is None:
        return None

    text = str(
        value
    ).strip()

    if text.startswith("\\"):
        text = text[1:]

    return text or None


def numeric(
    value: Any,
) -> Optional[float]:

    if value is None:
        return None

    try:
        return float(value)

    except (
        TypeError,
        ValueError,
    ):
        return None


def integer(
    value: Any,
) -> Optional[int]:

    if value is None:
        return None

    try:
        return int(value)

    except (
        TypeError,
        ValueError,
    ):
        return None


def clean_operator_counts(
    raw: Any,
) -> Dict[str, int]:

    if not isinstance(
        raw,
        dict,
    ):
        return {}

    result = {}

    for operator, count in raw.items():

        try:
            count = int(
                count
            )

        except (
            TypeError,
            ValueError,
        ):
            continue

        if count > 0:

            result[
                str(operator)
            ] = count

    return result


def operator_total(
    counts: Dict[str, int],
    operators: Sequence[str],
) -> int:

    return sum(
        counts.get(
            operator,
            0,
        )
        for operator
        in operators
    )


def contains_any(
    counts: Dict[str, int],
    operators: Sequence[str],
) -> bool:

    return (
        operator_total(
            counts,
            operators,
        )
        > 0
    )


# ============================================================================
# Narrowing-result lookup
# ============================================================================

def narrowing_path_identity(
    narrowing: Dict[str, Any],
) -> Tuple[
    Optional[str],
    Optional[str],
]:

    path = (
        narrowing.get(
            "ambiguous_path"
        )
        or {}
    )

    return (
        normalized_signal(
            path.get(
                "startpoint"
            )
        ),
        normalized_signal(
            path.get(
                "endpoint"
            )
        ),
    )


def path_identity(
    path: Dict[str, Any],
) -> Tuple[
    Optional[str],
    Optional[str],
]:

    return (
        normalized_signal(
            path.get(
                "startpoint"
            )
        ),
        normalized_signal(
            path.get(
                "endpoint"
            )
        ),
    )


def narrowing_matches_path(
    narrowing: Dict[str, Any],
    path: Dict[str, Any],
) -> bool:

    n_start, n_end = (
        narrowing_path_identity(
            narrowing
        )
    )

    p_start, p_end = (
        path_identity(
            path
        )
    )

    return (
        n_start == p_start
        and n_end == p_end
    )


# ============================================================================
# Resolve effective operator set
# ============================================================================

def effective_operator_counts(
    mapping: Dict[str, Any],
    narrowing: Optional[
        Dict[str, Any]
    ],
) -> Tuple[
    Dict[str, int],
    Dict[str, Any],
]:

    original = clean_operator_counts(
        mapping.get(
            "operator_counts"
        )
    )

    ambiguous = bool(
        mapping.get(
            "remaining_logical_reconvergence"
        )
    )

    if narrowing is None:
        embedded = mapping.get("physical_narrowing")
        if isinstance(embedded, dict):
            narrowing = embedded

    # ------------------------------------------------------------------
    # No ambiguity:
    #
    # map_to_rtl.py already gives the correct bit-precise cone.
    # ------------------------------------------------------------------

    if not ambiguous:

        return (
            original,
            {
                "status": "resolved_logically",
                "method": "bit_precise_rtl_mapping",
                "selected_branch": None,
                "selected_operator": None,
            },
        )

    # ------------------------------------------------------------------
    # Genuine ambiguity but no physical narrowing.
    # ------------------------------------------------------------------

    if narrowing is None:

        return (
            original,
            {
                "status": "unresolved_reconvergence",
                "method": None,
                "selected_branch": None,
                "selected_operator": None,
            },
        )

    if not narrowing.get(
        "resolved"
    ):

        return (
            original,
            {
                "status": "unresolved_reconvergence",
                "method": "physical_witness_narrowing_failed",
                "selected_branch": narrowing.get(
                    "selected_rtl_branch"
                ),
                "selected_operator": narrowing.get(
                    "selected_rtl_operator"
                ),
            },
        )

    # A resolved physical implementation does not necessarily select
    # one logical branch. Synthesis may factor multiple logical alternatives
    # into a shared physical implementation.
    #
    # In that case there is no branch-specific logical operator set to
    # select. Preserve the complete mapper-derived RTL operator cone.
    physical_resolution = (
        narrowing.get("physical_resolution")
        or {}
    )

    if (
        physical_resolution.get("status") == "resolved"
        and physical_resolution.get(
            "branch_selection_required"
        ) is False
    ):
        return (
            dict(original),
            {
                "status": "resolved_reconvergence",
                "method": "physical_shared_implementation",
                "selected_branch": None,
                "selected_operator": None,
                "physical_reason": physical_resolution.get(
                    "reason"
                ),
            },
        )

    selected_branch = narrowing.get(
        "selected_rtl_branch"
    )

    selected_operator = narrowing.get(
        "selected_rtl_operator"
    )

    # A physically selected branch is useful for RTL
    # optimization only when its RTL operator provenance is
    # defensible.  Never guess operator semantics from branch
    # labels such as "mux_a", "mux_b", "add", or "sub".
    #
    # A branch may legitimately have no non-mux operator
    # (for example a direct-wire branch).  In that case mux-only
    # narrowing remains valid.  Otherwise missing/conflicting
    # provenance must fail closed.
    non_mux_original = {
        operator: count
        for operator, count
        in original.items()
        if (
            operator not in MUX_OPS
            and count
        )
    }

    if (
        not selected_operator
        and non_mux_original
    ):
        return (
            original,
            {
                "status": "unresolved_reconvergence",
                "method": (
                    "physical_branch_operator_"
                    "provenance_unresolved"
                ),
                "selected_branch": (
                    selected_branch
                ),
                "selected_operator": None,
                "physical_reason": (
                    physical_resolution.get(
                        "reason"
                    )
                ),
            },
        )

    if (
        selected_operator
        and selected_operator
        not in original
    ):
        return (
            original,
            {
                "status": "unresolved_reconvergence",
                "method": (
                    "physical_branch_operator_"
                    "not_in_logical_cone"
                ),
                "selected_branch": (
                    selected_branch
                ),
                "selected_operator": (
                    selected_operator
                ),
                "physical_reason": (
                    physical_resolution.get(
                        "reason"
                    )
                ),
            },
        )

    # ------------------------------------------------------------------
    # After physical branch resolution:
    #
    # Keep:
    #   - the selected branch operator
    #   - muxes at/after reconvergence
    #
    # We intentionally remove competing branch operators.
    #
    # For the current validated example:
    #
    #     logical candidates:
    #       $add + $xor + $mux
    #
    #     physical result:
    #       ADD
    #
    #     effective structure:
    #       $add + $mux
    # ------------------------------------------------------------------

    effective: Dict[str, int] = {}

    if (
        selected_operator
        and selected_operator
        in original
    ):

        effective[
            selected_operator
        ] = original[
            selected_operator
        ]

    for mux_operator in MUX_OPS:

        if mux_operator in original:

            effective[
                mux_operator
            ] = original[
                mux_operator
            ]

    # Safety fallback.
    #
    # If narrowing selected an operator that does not exist in the
    # original logical cone, do not silently invent a structure.

    if not effective:

        effective = dict(
            original
        )

    return (
        effective,
        {
            "status": "resolved_physically",
            "method": (
                narrowing.get(
                    "resolution_source"
                )
                or "physical_witness_narrowing"
            ),
            "selected_branch": selected_branch,
            "selected_operator": selected_operator,
            "physical_confidence": (
                (
                    narrowing.get(
                        "physical_resolution"
                    )
                    or {}
                ).get(
                    "confidence"
                )
            ),
        },
    )


# ============================================================================
# Structural pattern detection
# ============================================================================

def detect_pattern(
    counts: Dict[str, int],
) -> str:

    add_count = operator_total(
        counts,
        ADD_OPS,
    )

    sub_count = operator_total(
        counts,
        SUB_OPS,
    )

    mul_count = operator_total(
        counts,
        MULTIPLY_OPS,
    )

    div_count = operator_total(
        counts,
        DIVIDE_OPS,
    )

    mux_count = operator_total(
        counts,
        MUX_OPS,
    )

    compare_count = operator_total(
        counts,
        COMPARE_OPS,
    )

    shift_count = operator_total(
        counts,
        SHIFT_OPS,
    )

    bitwise_count = operator_total(
        counts,
        BITWISE_OPS,
    )

    reduction_count = operator_total(
        counts,
        REDUCTION_OPS,
    )

    logical_count = operator_total(
        counts,
        LOGICAL_OPS,
    )

    # ------------------------------------------------------------------
    # Arithmetic patterns
    # ------------------------------------------------------------------

    if add_count >= 2 and mux_count:

        return "adder_chain_followed_by_mux"

    if add_count >= 2:

        return "adder_chain"

    if add_count == 1 and mux_count:

        return "adder_followed_by_mux"

    if add_count == 1:

        return "adder"

    if sub_count >= 2 and mux_count:

        return "subtractor_chain_followed_by_mux"

    if sub_count >= 2:

        return "subtractor_chain"

    if sub_count == 1 and mux_count:

        return "subtractor_followed_by_mux"

    if sub_count == 1:

        return "subtractor"

    # ------------------------------------------------------------------
    # Expensive arithmetic
    # ------------------------------------------------------------------

    if mul_count and mux_count:

        return "multiplier_followed_by_mux"

    if mul_count:

        return "multiplier"

    if div_count and mux_count:

        return "divider_followed_by_mux"

    if div_count:

        return "divider"

    # ------------------------------------------------------------------
    # Comparator
    # ------------------------------------------------------------------

    if compare_count and mux_count:

        return "comparator_driving_mux"

    if compare_count >= 2:

        return "comparison_chain"

    if compare_count:

        return "comparator"

    # ------------------------------------------------------------------
    # Shift
    # ------------------------------------------------------------------

    if shift_count and mux_count:

        return "shifter_followed_by_mux"

    if shift_count:

        return "shifter"

    # ------------------------------------------------------------------
    # Bitwise
    # ------------------------------------------------------------------

    if bitwise_count and mux_count:

        return "bitwise_logic_followed_by_mux"

    if bitwise_count >= 2:

        return "bitwise_logic_chain"

    if bitwise_count:

        return "bitwise_logic"

    # ------------------------------------------------------------------
    # Reduction
    # ------------------------------------------------------------------

    if reduction_count and mux_count:

        return "reduction_followed_by_mux"

    if reduction_count:

        return "reduction_tree"

    # ------------------------------------------------------------------
    # Logical
    # ------------------------------------------------------------------

    if logical_count and mux_count:

        return "logical_expression_followed_by_mux"

    if logical_count:

        return "logical_expression"

    # ------------------------------------------------------------------
    # Pure mux structures
    # ------------------------------------------------------------------

    if mux_count >= 2:

        return "mux_chain"

    if mux_count == 1:

        return "mux_selection"

    if counts:

        return "mixed_or_unclassified_logic"

    return "direct_or_empty_logic"


# ============================================================================
# Structural class
# ============================================================================

def detect_structural_class(
    counts: Dict[str, int],
) -> str:

    if contains_any(
        counts,
        MULTIPLY_OPS,
    ):

        return "multiplication"

    if contains_any(
        counts,
        DIVIDE_OPS,
    ):

        return "division"

    if contains_any(
        counts,
        ARITHMETIC_OPS,
    ):

        return "arithmetic"

    if contains_any(
        counts,
        COMPARE_OPS,
    ):

        return "comparison"

    if contains_any(
        counts,
        SHIFT_OPS,
    ):

        return "shift"

    if contains_any(
        counts,
        BITWISE_OPS,
    ):

        return "bitwise"

    if contains_any(
        counts,
        REDUCTION_OPS,
    ):

        return "reduction"

    if contains_any(
        counts,
        LOGICAL_OPS,
    ):

        return "logical"

    if contains_any(
        counts,
        MUX_OPS,
    ):

        return "muxing"

    if counts:

        return "generic_combinational"

    return "direct_connection"


# ============================================================================
# Complexity information
# ============================================================================

def structural_complexity(
    counts: Dict[str, int],
) -> Dict[str, Any]:

    total = sum(
        counts.values()
    )

    muxes = operator_total(
        counts,
        MUX_OPS,
    )

    arithmetic = operator_total(
        counts,
        ARITHMETIC_OPS,
    )

    expensive_arithmetic = (
        operator_total(
            counts,
            MULTIPLY_OPS,
        )
        +
        operator_total(
            counts,
            DIVIDE_OPS,
        )
    )

    return {
        "rtl_operator_count": total,
        "mux_count": muxes,
        "arithmetic_operator_count": arithmetic,
        "expensive_arithmetic_operator_count": (
            expensive_arithmetic
        ),
    }


# ============================================================================
# Optimization hints
# ============================================================================

def optimization_hints(
    structural_class: str,
    pattern: str,
) -> List[str]:

    if structural_class == "arithmetic":

        hints = [
            "inspect arithmetic carry-chain depth",
            "consider pipelining or retiming if latency changes are allowed",
            "consider algebraic reassociation only with equivalence checking",
        ]

        if "mux" in pattern:

            hints.append(
                "inspect whether mux placement can move before or after arithmetic"
            )

        return hints

    if structural_class == "multiplication":

        return [
            "inspect multiplier width and signedness",
            "consider pipelining or registered multiplier stages",
            "consider constant-coefficient simplification when applicable",
        ]

    if structural_class == "division":

        return [
            "division/modulo is typically timing expensive",
            "inspect whether iterative or pipelined architecture is acceptable",
            "check for constant-divisor simplification opportunities",
        ]

    if structural_class == "muxing":

        return [
            "inspect mux depth and select structure",
            "consider balanced mux trees",
            "consider restructuring nested conditional expressions",
        ]

    if structural_class == "comparison":

        return [
            "inspect comparator width",
            "inspect comparator-to-mux dependency",
            "consider early-decision or staged comparison where architecture permits",
        ]

    if structural_class == "shift":

        return [
            "inspect whether the shift is variable or constant",
            "variable shifts may synthesize into deep mux networks",
            "consider staged or pipelined shifting where appropriate",
        ]

    if structural_class == "bitwise":

        return [
            "inspect logic-chain depth",
            "consider Boolean simplification",
            "check whether operations can execute in parallel before reconvergence",
        ]

    if structural_class == "reduction":

        return [
            "inspect reduction-tree depth",
            "consider balanced-tree implementation",
        ]

    if structural_class == "logical":

        return [
            "inspect Boolean dependency depth",
            "consider logic factoring or balancing",
        ]

    return [
        "inspect the RTL cone manually before proposing a rewrite"
    ]


# ============================================================================
# Confidence
# ============================================================================

def classification_confidence(
    resolution: Dict[str, Any],
    mapping: Dict[str, Any],
) -> str:

    status = resolution.get(
        "status"
    )

    # A resolved physical witness is authoritative evidence for the
    # structural resolution, even when synthesis did not select one
    # logical RTL branch.
    #
    # Do not depend on a particular physical-resolution reason or
    # transformation type. Propagate the confidence supplied by the
    # physical resolver itself.
    physical_narrowing = (
        mapping.get("physical_narrowing")
        or {}
    )

    physical_resolution = (
        physical_narrowing.get("physical_resolution")
        or {}
    )

    if (
        status != "unresolved_reconvergence"
        and physical_narrowing.get("resolved") is True
        and physical_resolution.get("status") == "resolved"
    ):
        physical_confidence = str(
            physical_resolution.get("confidence")
            or ""
        ).lower()

        if physical_confidence in {
            "low",
            "medium",
            "high",
        }:
            return physical_confidence

    if status == "resolved_physically":

        physical = resolution.get(
            "physical_confidence"
        )

        if physical == "high":
            return "high"

        return "medium"

    if status == "resolved_logically":

        if mapping.get(
            "has_user_rtl_source"
        ):
            return "high"

        return "medium"

    return "low"


# ============================================================================
# One-path classification
# ============================================================================

def classify_one_path(
    path: Dict[str, Any],
    narrowing: Optional[
        Dict[str, Any]
    ],
) -> Dict[str, Any]:

    mapping = (
        path.get(
            "rtl_mapping"
        )
        or {}
    )

    mapping_status = mapping.get(
        "status"
    )

    result = dict(
        path
    )

    if mapping_status != "mapped":

        result[
            "classification"
        ] = {
            "status": "not_classified",
            "reason": (
                f"rtl_mapping status is {mapping_status!r}"
            ),
            "primary_bottleneck": None,
            "structural_class": None,
            "structural_pattern": None,
            "confidence": "none",
        }

        return result

    original_counts = clean_operator_counts(
        mapping.get(
            "operator_counts"
        )
    )

    effective_counts, resolution = (
        effective_operator_counts(
            mapping,
            narrowing,
        )
    )

    unresolved = (
        resolution.get(
            "status"
        )
        == "unresolved_reconvergence"
    )

    if unresolved:

        structural_class = (
            "ambiguous_reconvergent"
        )

        pattern = (
            "multiple_logically_valid_branches"
        )

        primary = None

    else:

        structural_class = (
            detect_structural_class(
                effective_counts
            )
        )

        pattern = detect_pattern(
            effective_counts
        )

        primary = structural_class

    confidence = classification_confidence(
        resolution,
        mapping,
    )

    timing_metrics = {

        "slack_ns": numeric(
            path.get(
                "slack_ns"
            )
        ),

        "combinational_delay_ns": numeric(
            path.get(
                "combinational_delay_ns"
            )
        ),

        "logic_depth": integer(
            path.get(
                "logic_depth"
            )
        ),

        "max_fanout": integer(
            path.get(
                "max_fanout"
            )
        ),
    }

    classification = {

        "status": (
            "classified"
            if not unresolved
            else "needs_physical_narrowing"
        ),

        "classification_semantics": (
            "RTL structural classification. "
            "primary_bottleneck identifies the primary structural "
            "operator class; it does not by itself prove which RTL "
            "operator contributes the largest physical delay."
        ),

        "primary_bottleneck": primary,

        "structural_class": structural_class,

        "structural_pattern": pattern,

        "confidence": confidence,

        "original_logical_operator_counts": (
            original_counts
        ),

        "effective_operator_counts": (
            effective_counts
        ),

        "resolution": resolution,

        "remaining_logical_reconvergence": bool(
            mapping.get(
                "remaining_logical_reconvergence"
            )
        ),

        "timing_metrics": timing_metrics,

        "complexity": structural_complexity(
            effective_counts
        ),

        "optimization_hints": (
            optimization_hints(
                structural_class,
                pattern,
            )
            if not unresolved
            else [
                "physical branch attribution remains unresolved; route this path to exploratory optimization over the complete trusted RTL cone"
            ]
        ),
    }

    result[
        "classification"
    ] = classification

    return result


# ============================================================================
# Summary
# ============================================================================

def build_summary(
    paths: List[Dict[str, Any]],
) -> Dict[str, Any]:

    status_counts = Counter()

    class_counts = Counter()

    pattern_counts = Counter()

    confidence_counts = Counter()

    for path in paths:

        classification = (
            path.get(
                "classification"
            )
            or {}
        )

        status = classification.get(
            "status",
            "unknown",
        )

        status_counts[
            status
        ] += 1

        structural_class = (
            classification.get(
                "structural_class"
            )
        )

        if structural_class:

            class_counts[
                structural_class
            ] += 1

        pattern = classification.get(
            "structural_pattern"
        )

        if pattern:

            pattern_counts[
                pattern
            ] += 1

        confidence = classification.get(
            "confidence"
        )

        if confidence:

            confidence_counts[
                confidence
            ] += 1

    return {

        "path_count": len(
            paths
        ),

        "classification_status_counts": dict(
            status_counts
        ),

        "structural_class_counts": dict(
            class_counts
        ),

        "structural_pattern_counts": dict(
            pattern_counts
        ),

        "confidence_counts": dict(
            confidence_counts
        ),
    }


# ============================================================================
# CLI
# ============================================================================

def main() -> None:

    parser = argparse.ArgumentParser(
        description=(
            "Classify bit-precise/resolved RTL timing paths "
            "by structural bottleneck type."
        )
    )

    parser.add_argument(
        "--mapped-json",
        required=True,
        help="Output JSON from map_to_rtl.py",
    )

    parser.add_argument(
        "--narrowed-json",
        default=None,
        help=(
            "Optional output from narrow_reconvergence.py. "
            "Required when the mapped path has genuine reconvergence."
        ),
    )

    parser.add_argument(
        "--flow-dir",
        default=str(
            DEFAULT_FLOW_DIR
        ),
    )

    parser.add_argument(
        "--output",
        default=None,
    )

    args = parser.parse_args()

    flow_dir = Path(
        args.flow_dir
    ).resolve()

    mapped_file = resolve_file(
        args.mapped_json,
        flow_dir,
    )

    mapped_data = load_json(
        mapped_file
    )

    paths = mapped_data.get(
        "paths"
    )

    if not isinstance(
        paths,
        list,
    ):

        raise ValueError(
            "Mapped JSON must contain a top-level 'paths' list."
        )

    narrowing = None
    narrowing_file = None

    if args.narrowed_json:

        narrowing_file = resolve_file(
            args.narrowed_json,
            flow_dir,
        )

        narrowing = load_json(
            narrowing_file
        )

    classified_paths = []

    narrowing_used = False

    for path in paths:

        matching_narrowing = None

        if (
            narrowing is not None
            and narrowing_matches_path(
                narrowing,
                path,
            )
        ):

            matching_narrowing = (
                narrowing
            )

            narrowing_used = True

        classified_paths.append(
            classify_one_path(
                path,
                matching_narrowing,
            )
        )

    summary = build_summary(
        classified_paths
    )

    output_data = {

        "schema_version": 1,

        "classification_semantics": (
            "Classification is based on resolved pre-ABC RTL "
            "structure. Physical witness narrowing is used only "
            "when map_to_rtl.py reports genuine logical reconvergence."
        ),

        "design": mapped_data.get(
            "design"
        ),

        "platform": mapped_data.get(
            "platform"
        ),

        "flow_variant": mapped_data.get(
            "flow_variant"
        ),

        "source_mapped_json": str(
            mapped_file
        ),

        "source_narrowed_json": (
            str(
                narrowing_file
            )
            if narrowing_file
            else None
        ),

        "narrowing_result_used": (
            narrowing_used
        ),

        "summary": summary,

        "paths": classified_paths,
    }

    if args.output:

        output_file = Path(
            args.output
        ).expanduser()

        if not output_file.is_absolute():

            output_file = (
                Path.cwd()
                / output_file
            )

        output_file = (
            output_file.resolve()
        )

    else:

        name = mapped_file.name

        if name.endswith(
            "_rtl_mapped.json"
        ):

            name = name[
                :-len(
                    "_rtl_mapped.json"
                )
            ] + "_classified.json"

        else:

            name = (
                mapped_file.stem
                + "_classified.json"
            )

        output_file = (
            mapped_file.parent
            / name
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

    print()
    print(
        "=" * 68
    )
    print(
        "RTL Path Classification"
    )
    print(
        "=" * 68
    )

    print(
        f"Design       : "
        f"{output_data.get('design')}"
    )

    print(
        f"Flow Variant : "
        f"{output_data.get('flow_variant')}"
    )

    print(
        f"Paths        : "
        f"{summary['path_count']}"
    )

    print()

    for path in classified_paths:

        classification = (
            path.get(
                "classification"
            )
            or {}
        )

        print(
            f"{path.get('startpoint')} "
            f"-> "
            f"{path.get('endpoint')}"
        )

        print(
            f"  status     : "
            f"{classification.get('status')}"
        )

        print(
            f"  class      : "
            f"{classification.get('structural_class')}"
        )

        print(
            f"  pattern    : "
            f"{classification.get('structural_pattern')}"
        )

        print(
            f"  operators  : "
            f"{classification.get('effective_operator_counts')}"
        )

        print(
            f"  confidence : "
            f"{classification.get('confidence')}"
        )

        resolution = (
            classification.get(
                "resolution"
            )
            or {}
        )

        if resolution.get(
            "selected_branch"
        ):

            print(
                f"  branch     : "
                f"{resolution.get('selected_branch')}"
            )

        print()

    print(
        "Summary"
    )

    print(
        "-" * 68
    )

    print(
        "Classes : "
        f"{summary['structural_class_counts']}"
    )

    print(
        "Patterns: "
        f"{summary['structural_pattern_counts']}"
    )

    print()
    print(
        f"JSON written to:\n"
        f"{output_file}"
    )

    print(
        "=" * 68
    )


if __name__ == "__main__":
    main()
