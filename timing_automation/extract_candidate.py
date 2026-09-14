#!/usr/bin/env python3
"""Extract complete Verilog optimizer responses (schema 3.1).

The default model contract returns every SourceBundle file in source_files[].
Complete-source responses require --manifest and --base-dir for identity checks.
Legacy patch responses remain readable for historical replay and self-tests.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence, Tuple


# ============================================================
# Paths
# ============================================================

SCRIPT_DIR = Path(__file__).resolve().parent
SCHEMA_PATH = SCRIPT_DIR / "response_schema_patches_v2.json"


# ============================================================
# Local imports
# ============================================================

try:
    from design_manifest import (
        ClockSpec,
        DesignManifest,
        PhysicalPolicy,
        VerificationPolicy,
    )

    from source_bundle import (
        SourceBundle,
    )

    from patch_splicer import (
        OverlappingPatchError,
        PatchError,
        PatchTargetError,
        SourcePatch,
        StaleSourceError,
        find_module_line_spans,
        validate_no_overlaps,
        validate_patch_target,
    )

except ImportError:
    from timing_automation.design_manifest import (
        ClockSpec,
        DesignManifest,
        PhysicalPolicy,
        VerificationPolicy,
    )

    from timing_automation.source_bundle import (
        SourceBundle,
    )

    from timing_automation.patch_splicer import (
        OverlappingPatchError,
        PatchError,
        PatchTargetError,
        SourcePatch,
        StaleSourceError,
        find_module_line_spans,
        validate_no_overlaps,
        validate_patch_target,
    )


# ============================================================
# Constants
# ============================================================

EXPECTED_SCHEMA_VERSION = "2.0"

ALLOWED_TOP_LEVEL_KEYS = {
    "schema_version",
    "chosen_pattern_id",
    "target_unit_id",
    "patches",
    "rationale",
    "predicted_wns_gain_ns",
    "predicted_area_change_pct",
    "predicted_power_change_pct",
    "self_checks",
}

REQUIRED_TOP_LEVEL_KEYS = {
    "schema_version",
    "chosen_pattern_id",
    "patches",
    "rationale",
    "predicted_wns_gain_ns",
    "self_checks",
}

ALLOWED_PATCH_KEYS = {
    "patch_id",
    "patch_type",
    "target_file",
    "target_module",
    "original_sha256",
    "changed_source_range",
    "replacement_text",
    "rationale",
}

REQUIRED_PATCH_KEYS = {
    "patch_id",
    "patch_type",
    "target_file",
    "target_module",
    "original_sha256",
    "changed_source_range",
    "replacement_text",
}

ALLOWED_PATCH_TYPES = {
    "line_range",
    "whole_module",
}

REQUIRED_SELF_CHECKS = {
    "interface_unchanged",
    "parameters_unchanged",
    "latency_unchanged",
    "clock_reset_behavior_unchanged",
    "source_bundle_targets_valid",
    "no_new_latches",
    "no_new_clock_domains",
}

ALLOWED_SELF_CHECK_KEYS = (
    REQUIRED_SELF_CHECKS
    | {
        "notes",
    }
)

MODULE_NAME_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_$]*$"
)


# ============================================================
# Exceptions
# ============================================================

class CandidateExtractionError(RuntimeError):
    """Raised when an LLM candidate cannot be accepted."""


class CandidateSchemaError(CandidateExtractionError):
    """Raised when candidate JSON violates the active response contract."""


class CandidateSourceBundleFileSetError(CandidateSchemaError):
    """Raised when a complete-source candidate changes the selected base file set.

    The pre-freeze optimizer deliberately requires the model to return exactly the
    existing selected-base source files.  This is a candidate-shape rejection, not
    an infrastructure failure.
    """

    def __init__(self, *, expected_files, returned_files):
        expected = tuple(sorted(str(x) for x in expected_files))
        returned = tuple(sorted(str(x) for x in returned_files))
        self.expected_files = expected
        self.returned_files = returned
        self.missing_files = tuple(sorted(set(expected) - set(returned)))
        self.unexpected_files = tuple(sorted(set(returned) - set(expected)))
        details = []
        if self.missing_files:
            details.append("missing=" + ",".join(self.missing_files))
        if self.unexpected_files:
            details.append("unexpected=" + ",".join(self.unexpected_files))
        suffix = "; " + "; ".join(details) if details else ""
        super().__init__(
            "complete-source file set must exactly match selected editing base" + suffix
        )


# ============================================================
# Canonical candidate
# ============================================================

@dataclass(frozen=True)
class ExtractedCandidate:
    """
    Parsed and validated optimizer response.

    `payload` remains JSON-serializable.

    `patches` contains executable SourcePatch objects.
    """

    payload: Dict[str, Any]

    patches: Tuple[SourcePatch, ...]

    patch_types: Tuple[str, ...]

    bundle_validated: bool = False
    complete_sources: Dict[str, bytes] | None = None
    original_sources: Dict[str, bytes] | None = None

    @property
    def chosen_pattern_id(self) -> str | None:
        return self.payload.get(
            "chosen_pattern_id"
        )

    @property
    def target_unit_id(self) -> str | None:
        return self.payload.get(
            "target_unit_id"
        )

    @property
    def decision(self) -> str:
        return str(self.payload.get("decision", "propose_candidate"))

    @property
    def editing_base(self) -> str | None:
        return self.payload.get("editing_base")


@dataclass(frozen=True)
class StopRecommendation:
    payload: Dict[str, Any]

    @property
    def decision(self) -> str:
        return "no_further_optimization"

    @property
    def rationale(self) -> str:
        return str(self.payload.get("rationale", ""))


# ============================================================
# JSON extraction
# ============================================================

def strip_markdown_fences(
    text: str,
) -> str:
    """
    If a Markdown code fence exists, return its content.

    Otherwise return the original text.

    We still support prose outside the fence.
    """

    match = re.search(
        r"```(?:json)?\s*(.*?)```",
        text,
        flags=(
            re.DOTALL
            | re.IGNORECASE
        ),
    )

    if match:
        return match.group(1)

    return text


def find_json_objects(
    text: str,
) -> List[Dict[str, Any]]:
    """
    Find JSON objects embedded inside arbitrary text.

    json.JSONDecoder.raw_decode() is used instead of trying to parse
    braces with regex.
    """

    decoder = json.JSONDecoder()

    objects: List[
        Dict[str, Any]
    ] = []

    for index, char in enumerate(
        text
    ):

        if char != "{":
            continue

        try:
            value, _end = (
                decoder.raw_decode(
                    text[index:]
                )
            )

        except json.JSONDecodeError:
            continue

        if isinstance(
            value,
            dict,
        ):
            objects.append(
                value
            )

    return objects


def candidate_score(
    obj: Mapping[str, Any],
) -> int:
    """
    Prefer objects that look like canonical candidate responses.

    This avoids accidentally selecting a small JSON object from prose
    before the real response.
    """

    score = 0

    if "schema_version" in obj:
        score += 10

    if "source_files" in obj:
        score += 20

    if "patches" in obj:
        score += 20

    if "chosen_pattern_id" in obj:
        score += 5

    if "self_checks" in obj:
        score += 5

    # Legacy candidate should still be selected so we can give a
    # meaningful rejection message.
    if "rewritten_module" in obj:
        score += 15

    return score


def extract_json_response(
    raw_text: str,
) -> Dict[str, Any]:
    """
    Extract the most likely optimizer JSON object from raw model output.
    """

    if not isinstance(
        raw_text,
        str,
    ):
        raise CandidateExtractionError(
            "raw LLM response must be text"
        )

    if not raw_text.strip():
        raise CandidateExtractionError(
            "LLM response is empty"
        )

    candidates: List[
        Dict[str, Any]
    ] = []

    # --------------------------------------------------------
    # First inspect fenced content
    # --------------------------------------------------------

    fenced = strip_markdown_fences(
        raw_text
    )

    candidates.extend(
        find_json_objects(
            fenced
        )
    )

    # --------------------------------------------------------
    # Then inspect entire response if different
    # --------------------------------------------------------

    if fenced != raw_text:

        candidates.extend(
            find_json_objects(
                raw_text
            )
        )

    if not candidates:
        raise CandidateExtractionError(
            "no valid JSON object found in LLM response"
        )

    # Highest score wins.
    #
    # Python sort is stable, so first equally-scored response remains
    # preferred.
    candidates = sorted(
        candidates,
        key=candidate_score,
        reverse=True,
    )

    return candidates[0]


# ============================================================
# Primitive validators
# ============================================================

def is_number(
    value: Any,
) -> bool:
    """
    bool is intentionally not accepted even though bool subclasses int.
    """

    return (
        isinstance(
            value,
            (
                int,
                float,
            ),
        )
        and not isinstance(
            value,
            bool,
        )
    )


def validate_optional_number(
    obj: Mapping[str, Any],
    key: str,
    problems: List[str],
) -> None:

    if key not in obj:
        return

    value = obj[
        key
    ]

    if (
        value is not None
        and not is_number(value)
    ):
        problems.append(
            f"{key} must be a number or null"
        )


# ============================================================
# Schema validation
# ============================================================

def validate_response_schema_file() -> None:
    """
    Ensure the response_schema.json beside this script is the v2 schema
    expected by this extractor.

    This catches accidentally running the new extractor with the old
    rewritten_module schema.
    """

    if not SCHEMA_PATH.is_file():
        raise CandidateSchemaError(
            f"response schema not found: "
            f"{SCHEMA_PATH}"
        )

    try:

        schema = json.loads(
            SCHEMA_PATH.read_text(
                encoding="utf-8"
            )
        )

    except json.JSONDecodeError as exc:

        raise CandidateSchemaError(
            f"invalid response_schema.json: "
            f"{exc}"
        ) from exc

    properties = schema.get(
        "properties",
        {},
    )

    if "patches" not in properties:
        raise CandidateSchemaError(
            "response_schema.json is not v2: "
            "missing patches"
        )

    if "rewritten_module" in properties:
        raise CandidateSchemaError(
            "response_schema.json is legacy: "
            "rewritten_module still exists"
        )

    schema_version = (
        properties.get(
            "schema_version",
            {},
        ).get(
            "const"
        )
    )

    if (
        schema_version
        != EXPECTED_SCHEMA_VERSION
    ):
        raise CandidateSchemaError(
            "response_schema.json version mismatch: "
            f"expected {EXPECTED_SCHEMA_VERSION!r}, "
            f"got {schema_version!r}"
        )


def validate_self_checks(
    value: Any,
    problems: List[str],
) -> None:

    if not isinstance(
        value,
        dict,
    ):

        problems.append(
            "self_checks must be an object"
        )

        return

    actual_keys = set(
        value
    )

    missing = (
        REQUIRED_SELF_CHECKS
        - actual_keys
    )

    extra = (
        actual_keys
        - ALLOWED_SELF_CHECK_KEYS
    )

    if missing:

        problems.append(
            "self_checks missing required keys: "
            + ", ".join(
                sorted(missing)
            )
        )

    if extra:

        problems.append(
            "self_checks has unexpected keys: "
            + ", ".join(
                sorted(extra)
            )
        )

    for key in REQUIRED_SELF_CHECKS:

        if key not in value:
            continue

        if not isinstance(
            value[key],
            bool,
        ):
            problems.append(
                f"self_checks.{key} "
                "must be boolean"
            )

    if "notes" in value:

        notes = value[
            "notes"
        ]

        if not isinstance(
            notes,
            list,
        ):

            problems.append(
                "self_checks.notes "
                "must be an array"
            )

        else:

            for index, note in enumerate(
                notes
            ):

                if not isinstance(
                    note,
                    str,
                ):
                    problems.append(
                        f"self_checks.notes[{index}] "
                        "must be a string"
                    )


def validate_patch_record(
    value: Any,
    index: int,
    problems: List[str],
) -> SourcePatch | None:
    """
    Validate one patch record and convert it to SourcePatch.
    """

    label = (
        f"patches[{index}]"
    )

    if not isinstance(
        value,
        dict,
    ):

        problems.append(
            f"{label} must be an object"
        )

        return None

    actual_keys = set(
        value
    )

    missing = (
        REQUIRED_PATCH_KEYS
        - actual_keys
    )

    extra = (
        actual_keys
        - ALLOWED_PATCH_KEYS
    )

    if missing:

        problems.append(
            f"{label} missing required keys: "
            + ", ".join(
                sorted(missing)
            )
        )

    if extra:

        problems.append(
            f"{label} has unexpected keys: "
            + ", ".join(
                sorted(extra)
            )
        )

    # --------------------------------------------------------
    # patch_id
    # --------------------------------------------------------

    patch_id = value.get(
        "patch_id"
    )

    if (
        not isinstance(
            patch_id,
            str,
        )
        or not patch_id.strip()
    ):
        problems.append(
            f"{label}.patch_id "
            "must be a non-empty string"
        )

    # --------------------------------------------------------
    # patch_type
    # --------------------------------------------------------

    patch_type = value.get(
        "patch_type"
    )

    if (
        not isinstance(
            patch_type,
            str,
        )
        or patch_type
        not in ALLOWED_PATCH_TYPES
    ):
        problems.append(
            f"{label}.patch_type "
            "must be one of: "
            + ", ".join(
                sorted(
                    ALLOWED_PATCH_TYPES
                )
            )
        )

    # --------------------------------------------------------
    # target file
    # --------------------------------------------------------

    target_file = value.get(
        "target_file"
    )

    if (
        not isinstance(
            target_file,
            str,
        )
        or not target_file.strip()
    ):
        problems.append(
            f"{label}.target_file "
            "must be a non-empty string"
        )

    # --------------------------------------------------------
    # target module
    # --------------------------------------------------------

    target_module = value.get(
        "target_module"
    )

    if (
        not isinstance(
            target_module,
            str,
        )
        or not MODULE_NAME_RE.fullmatch(
            target_module
        )
    ):
        problems.append(
            f"{label}.target_module "
            "is not a valid RTL module identifier"
        )

    # --------------------------------------------------------
    # replacement
    #
    # Empty string is legal because deletion may be represented by a
    # patch.
    # --------------------------------------------------------

    replacement = value.get(
        "replacement_text"
    )

    if not isinstance(
        replacement,
        str,
    ):
        problems.append(
            f"{label}.replacement_text "
            "must be a string"
        )

    # --------------------------------------------------------
    # optional rationale
    # --------------------------------------------------------

    if "rationale" in value:

        if not isinstance(
            value["rationale"],
            str,
        ):
            problems.append(
                f"{label}.rationale "
                "must be a string"
            )

    # --------------------------------------------------------
    # SourcePatch performs SHA/range validation.
    # --------------------------------------------------------

    try:

        patch = (
            SourcePatch.from_dict(
                value
            )
        )

    except (
        PatchError,
        KeyError,
        TypeError,
        ValueError,
    ) as exc:

        problems.append(
            f"{label}: {exc}"
        )

        return None

    return patch


def validate_candidate_shape(
    obj: Mapping[str, Any],
) -> Tuple[
    List[SourcePatch],
    List[str],
]:
    """
    Strict dependency-free validator for response schema v2.

    Returns:
        patches,
        patch_types

    Raises:
        CandidateSchemaError
    """

    if not isinstance(
        obj,
        Mapping,
    ):
        raise CandidateSchemaError(
            "response must be a JSON object"
        )

    # --------------------------------------------------------
    # Explicit legacy rejection
    # --------------------------------------------------------

    if "rewritten_module" in obj:

        raise CandidateSchemaError(
            "legacy response rejected: "
            "rewritten_module is no longer a canonical artifact; "
            "return patches[] instead"
        )

    if "changed_line_ranges" in obj:

        raise CandidateSchemaError(
            "legacy response rejected: "
            "changed_line_ranges has been replaced by "
            "patches[].changed_source_range"
        )

    problems: List[str] = []

    keys = set(
        obj
    )

    missing = (
        REQUIRED_TOP_LEVEL_KEYS
        - keys
    )

    extra = (
        keys
        - ALLOWED_TOP_LEVEL_KEYS
    )

    if missing:

        problems.append(
            "missing required keys: "
            + ", ".join(
                sorted(missing)
            )
        )

    if extra:

        problems.append(
            "unexpected top-level keys: "
            + ", ".join(
                sorted(extra)
            )
        )

    # --------------------------------------------------------
    # Version
    # --------------------------------------------------------

    if obj.get(
        "schema_version"
    ) != EXPECTED_SCHEMA_VERSION:

        problems.append(
            "schema_version must be "
            f"{EXPECTED_SCHEMA_VERSION!r}"
        )

    # --------------------------------------------------------
    # chosen_pattern_id
    # --------------------------------------------------------

    chosen_pattern = obj.get(
        "chosen_pattern_id"
    )

    if (
        chosen_pattern is not None
        and (
            not isinstance(
                chosen_pattern,
                str,
            )
            or not chosen_pattern.strip()
        )
    ):
        problems.append(
            "chosen_pattern_id must be "
            "a non-empty string or null"
        )

    # --------------------------------------------------------
    # target_unit_id
    # --------------------------------------------------------

    if "target_unit_id" in obj:

        target_unit = obj[
            "target_unit_id"
        ]

        if (
            target_unit is not None
            and (
                not isinstance(
                    target_unit,
                    str,
                )
                or not target_unit.strip()
            )
        ):
            problems.append(
                "target_unit_id must be "
                "a non-empty string or null"
            )

    # --------------------------------------------------------
    # rationale
    # --------------------------------------------------------

    rationale = obj.get(
        "rationale"
    )

    if (
        not isinstance(
            rationale,
            str,
        )
        or not rationale.strip()
    ):
        problems.append(
            "rationale must be a non-empty string"
        )

    # --------------------------------------------------------
    # predictions
    # --------------------------------------------------------

    validate_optional_number(
        obj,
        "predicted_wns_gain_ns",
        problems,
    )

    validate_optional_number(
        obj,
        "predicted_area_change_pct",
        problems,
    )

    validate_optional_number(
        obj,
        "predicted_power_change_pct",
        problems,
    )

    # --------------------------------------------------------
    # self checks
    # --------------------------------------------------------

    if "self_checks" in obj:

        validate_self_checks(
            obj["self_checks"],
            problems,
        )

    # --------------------------------------------------------
    # patches
    # --------------------------------------------------------

    raw_patches = obj.get(
        "patches"
    )

    patches: List[
        SourcePatch
    ] = []

    patch_types: List[
        str
    ] = []

    patch_ids: List[
        str
    ] = []

    if not isinstance(
        raw_patches,
        list,
    ):

        problems.append(
            "patches must be an array"
        )

    elif not raw_patches:

        problems.append(
            "patches must contain at least one patch"
        )

    else:

        for index, value in enumerate(
            raw_patches
        ):

            patch = (
                validate_patch_record(
                    value,
                    index,
                    problems,
                )
            )

            if patch is None:
                continue

            patches.append(
                patch
            )

            patch_type = value.get(
                "patch_type"
            )

            if isinstance(
                patch_type,
                str,
            ):
                patch_types.append(
                    patch_type
                )

            patch_ids.append(
                patch.patch_id
            )

    # --------------------------------------------------------
    # Unique patch IDs
    # --------------------------------------------------------

    seen = set()

    duplicates = set()

    for patch_id in patch_ids:

        if patch_id in seen:
            duplicates.add(
                patch_id
            )

        seen.add(
            patch_id
        )

    if duplicates:

        problems.append(
            "duplicate patch_id values: "
            + ", ".join(
                sorted(duplicates)
            )
        )

    # --------------------------------------------------------
    # Overlap validation
    # --------------------------------------------------------

    if patches:

        try:

            validate_no_overlaps(
                patches
            )

        except OverlappingPatchError as exc:

            problems.append(
                str(exc)
            )

    if problems:

        raise CandidateSchemaError(
            "\n".join(
                f"- {problem}"
                for problem in problems
            )
        )

    return (
        patches,
        patch_types,
    )


# ============================================================
# Bundle-aware semantic validation
# ============================================================

def validate_against_bundle(
    payload: Mapping[str, Any],
    patches: Sequence[SourcePatch],
    bundle: SourceBundle,
) -> None:
    """
    Validate patch targets against the exact source snapshot.

    This is stronger than JSON shape validation.

    Checks:
        - target file exists
        - target module exists
        - module belongs to target file
        - original SHA matches
        - source range lies inside target module
        - whole_module exactly spans the module
    """

    raw_patches = payload[
        "patches"
    ]

    for index, patch in enumerate(
        patches
    ):

        try:

            validate_patch_target(
                bundle,
                patch,
            )

        except (
            PatchTargetError,
            StaleSourceError,
            PatchError,
        ) as exc:

            raise CandidateSchemaError(
                f"patches[{index}] failed "
                f"SourceBundle validation: {exc}"
            ) from exc

        patch_type = raw_patches[
            index
        ][
            "patch_type"
        ]

        # ----------------------------------------------------
        # whole_module must exactly replace the module span
        # ----------------------------------------------------

        if patch_type == "whole_module":

            source = (
                bundle.get_source(
                    patch.target_file
                )
            )

            spans = (
                find_module_line_spans(
                    source.text()
                )
            )

            expected = spans.get(
                patch.target_module
            )

            if expected is None:

                raise CandidateSchemaError(
                    f"patches[{index}] cannot locate "
                    f"module {patch.target_module!r}"
                )

            actual = (
                patch.start_line,
                patch.end_line,
            )

            if actual != expected:

                raise CandidateSchemaError(
                    f"patches[{index}] is whole_module but "
                    f"range is {actual[0]}-{actual[1]}; "
                    f"module {patch.target_module!r} "
                    f"actually spans "
                    f"{expected[0]}-{expected[1]}"
                )


# ============================================================
# Main extraction API
# ============================================================

def validate_prefreeze_payload(payload: Mapping[str, Any]) -> None:
    from jsonschema import Draft202012Validator
    version = payload.get("schema_version")
    schema_name = "response_schema_latency.json" if version in {"3.2", "3.3"} else "response_schema_prefreeze.json"
    schema = json.loads((SCRIPT_DIR / schema_name).read_text())
    errors = sorted(Draft202012Validator(schema).iter_errors(payload), key=lambda e: list(e.path))
    if errors:
        error = errors[0]
        location = ".".join(str(part) for part in error.path)
        prefix = f" at {location}" if location else ""
        raise CandidateSchemaError(f"schema-{payload.get('schema_version')} response" + prefix + ": " + error.message)


def extract_complete_sources(payload, bundle):
    validate_prefreeze_payload(payload)
    if payload.get("decision") != "propose_candidate":
        raise CandidateSchemaError("complete-source extraction requires decision=propose_candidate")
    if bundle is None:
        raise CandidateSchemaError("complete-source validation requires the selected editing-base SourceBundle")
    items = payload["source_files"]
    names = [item["target_file"] for item in items]
    if len(names) != len(set(names)):
        raise CandidateSchemaError("duplicate complete-source file")
    if set(names) != set(bundle.source_names()):
        raise CandidateSourceBundleFileSetError(
            expected_files=bundle.source_names(), returned_files=names
        )
    files = {}
    originals = {}
    for item in items:
        name = item["target_file"]
        source = bundle.get_source(name)
        # The backend, not the LLM, owns selected-base identity/hash truth.
        # The complete returned file set is interpreted against ``bundle`` chosen
        # from the model's editing_base decision.
        files[name] = item["verilog"].encode("utf-8")
        originals[name] = source.content
    return ExtractedCandidate(
        payload=dict(payload), patches=(), patch_types=(),
        bundle_validated=True, complete_sources=files, original_sources=originals
    )


def extract_optimizer_response(
    raw_text: str,
    *,
    bundle: SourceBundle | None = None,
) -> ExtractedCandidate | StopRecommendation:
    """Parse one schema-3.1/3.2/3.3 optimizer decision.

    Candidate responses are validated against the selected editing-base bundle.
    Stop recommendations contain no RTL and are intentionally advisory.
    """
    payload = extract_json_response(raw_text)
    if payload.get("schema_version") in {"3.1", "3.2", "3.3"}:
        validate_prefreeze_payload(payload)
        if payload.get("decision") == "no_further_optimization":
            return StopRecommendation(payload=dict(payload))
        return extract_complete_sources(payload, bundle)
    # Historical responses remain readable for replay.
    return extract_candidate(raw_text, bundle=bundle)


def extract_candidate(
    raw_text: str,
    *,
    bundle: SourceBundle | None = None,
) -> ExtractedCandidate:
    """Extract and validate one materializable candidate response."""
    validate_response_schema_file()
    payload = extract_json_response(raw_text)
    if payload.get("schema_version") in {"3.1", "3.2", "3.3"}:
        validate_prefreeze_payload(payload)
        if payload.get("decision") != "propose_candidate":
            raise CandidateSchemaError("no_further_optimization is not a materializable candidate")
        return extract_complete_sources(payload, bundle)
    if payload.get("schema_version") == "3.0" or "source_files" in payload:
        # Historical schema-3.0 complete-source replay.  Validate true 3.0
        # responses strictly so unexpected patch-shaped fields cannot slip
        # through the compatibility extractor.
        if payload.get("schema_version") == "3.0":
            from jsonschema import Draft202012Validator
            legacy_schema = json.loads((SCRIPT_DIR / "response_schema.json").read_text())
            errors = sorted(Draft202012Validator(legacy_schema).iter_errors(payload), key=lambda e: list(e.path))
            if errors:
                error = errors[0]
                location = ".".join(str(part) for part in error.path)
                prefix = f" at {location}" if location else ""
                raise CandidateSchemaError("schema-3.0 response" + prefix + ": " + error.message)
        # Historical schema-3.0 complete-source replay.
        if bundle is None:
            raise CandidateSchemaError("complete-source validation requires a manifest and SourceBundle")
        items = payload["source_files"]
        names = [item["target_file"] for item in items]
        if len(names) != len(set(names)) or set(names) != set(bundle.source_names()):
            raise CandidateSchemaError("complete-source file set must exactly match SourceBundle")
        files, originals = {}, {}
        for item in items:
            name = item["target_file"]
            source = bundle.get_source(name)
            declared = item.get("original_sha256")
            if not isinstance(declared, str) or declared.lower() != source.sha256.lower():
                raise CandidateSchemaError("stale complete-source SHA-256: " + name)
            files[name] = item["verilog"].encode("utf-8")
            originals[name] = source.content
        return ExtractedCandidate(payload=dict(payload), patches=(), patch_types=(),
                                  bundle_validated=True, complete_sources=files, original_sources=originals)

    patches, patch_types = validate_candidate_shape(payload)
    bundle_validated = False
    if bundle is not None:
        validate_against_bundle(payload, patches, bundle)
        bundle_validated = True
    canonical_payload = json.loads(json.dumps(payload))
    return ExtractedCandidate(payload=canonical_payload, patches=tuple(patches),
                              patch_types=tuple(patch_types), bundle_validated=bundle_validated)


# ============================================================
# Output
# ============================================================

def candidate_paths(
    out_stem: Path,
) -> Tuple[
    Path,
    Path,
]:
    """
    Preserve the old --out STEM interface.

    Example:

        --out ai_runs/cand_001

    creates:

        ai_runs/cand_001.candidate.json
        ai_runs/cand_001.meta.json
    """

    out_stem = Path(
        out_stem
    )

    candidate_path = (
        out_stem.parent
        / (
            out_stem.name
            + ".candidate.json"
        )
    )

    meta_path = (
        out_stem.parent
        / (
            out_stem.name
            + ".meta.json"
        )
    )

    return (
        candidate_path,
        meta_path,
    )


def write_candidate(
    candidate: ExtractedCandidate,
    out_stem: Path,
) -> Tuple[
    Path,
    Path,
]:
    """
    Write canonical extracted candidate + metadata.
    """

    candidate_path, meta_path = (
        candidate_paths(
            out_stem
        )
    )

    candidate_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    candidate_path.write_text(
        json.dumps(
            candidate.payload,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    target_files = sorted(
        {
            patch.target_file
            for patch in candidate.patches
        }
    )

    target_modules = sorted(
        {
            patch.target_module
            for patch in candidate.patches
        }
    )

    meta = {
        "ok": True,
        "schema_version": (
            candidate.payload[
                "schema_version"
            ]
        ),
        "chosen_pattern_id": (
            candidate.chosen_pattern_id
        ),
        "target_unit_id": (
            candidate.target_unit_id
        ),
        "patch_count": len(
            candidate.patches
        ),
        "patch_ids": [
            patch.patch_id
            for patch in candidate.patches
        ],
        "patch_types": list(
            candidate.patch_types
        ),
        "target_files": target_files,
        "target_modules": target_modules,
        "bundle_validated": (
            candidate.bundle_validated
        ),
        "candidate_json_path": str(
            candidate_path
        ),
    }

    meta_path.write_text(
        json.dumps(
            meta,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    return (
        candidate_path,
        meta_path,
    )


# ============================================================
# Self-test helpers
# ============================================================

def make_self_checks() -> Dict[str, Any]:

    return {
        "interface_unchanged": True,
        "parameters_unchanged": True,
        "latency_unchanged": True,
        "clock_reset_behavior_unchanged": True,
        "source_bundle_targets_valid": True,
        "no_new_latches": True,
        "no_new_clock_domains": True,
        "notes": [],
    }


def make_valid_response(
    *,
    sha256: str,
) -> Dict[str, Any]:

    return {
        "schema_version": "2.0",
        "chosen_pattern_id": (
            "bool.factor_common_subexpr"
        ),
        "target_unit_id": "unit_top",
        "patches": [
            {
                "patch_id": "patch_001",
                "patch_type": "line_range",
                "target_file": "rtl/top.v",
                "target_module": "top",
                "original_sha256": sha256,
                "changed_source_range": {
                    "start_line": 6,
                    "end_line": 6,
                },
                "replacement_text": (
                    "assign y = a | b;\n"
                ),
                "rationale": (
                    "Simplify the local logic cone."
                ),
            }
        ],
        "rationale": (
            "Reduce logic depth in the target cone."
        ),
        "predicted_wns_gain_ns": 0.02,
        "predicted_area_change_pct": -1.0,
        "predicted_power_change_pct": None,
        "self_checks": (
            make_self_checks()
        ),
    }


# ============================================================
# Self-test
# ============================================================

def run_self_test() -> None:

    validate_response_schema_file()

    with tempfile.TemporaryDirectory() as tmp:

        root = Path(
            tmp
        )

        rtl_dir = (
            root
            / "rtl"
        )

        rtl_dir.mkdir()

        top_file = (
            rtl_dir
            / "top.v"
        )

        helper_file = (
            rtl_dir
            / "helper.v"
        )

        top_file.write_text(
            (
                "module top (\n"
                "    input wire a,\n"
                "    input wire b,\n"
                "    output wire y\n"
                ");\n"
                "assign y = a & b;\n"
                "endmodule\n"
            ),
            encoding="utf-8",
        )

        helper_file.write_text(
            (
                "module helper (\n"
                "    input wire a,\n"
                "    output wire y\n"
                ");\n"
                "assign y = a;\n"
                "endmodule\n"
            ),
            encoding="utf-8",
        )

        manifest = DesignManifest(
            dataset=(
                "extract_candidate_self_test"
            ),
            design_id="extract_test",
            top="top",
            source_files=[
                "rtl/top.v",
                "rtl/helper.v",
            ],
            language="verilog",
            clocks=[],
            resets=[],
            constraints={},
            verification=(
                VerificationPolicy(
                    backend="yosys",
                    mode=(
                        "sequential_equivalence"
                    ),
                )
            ),
            physical=(
                PhysicalPolicy(
                    platform="nangate45",
                    evaluation_stage="synth",
                )
            ),
        )

        manifest.refresh_source_hashes(
            root
        )

        bundle = (
            SourceBundle.from_manifest(
                manifest,
                root,
            )
        )

        top_sha = (
            bundle.get_source(
                "rtl/top.v"
            ).sha256
        )

        # ----------------------------------------------------
        # 1. Fenced response extraction
        # ----------------------------------------------------

        valid_payload = (
            make_valid_response(
                sha256=top_sha
            )
        )

        raw = (
            "Here is the candidate:\n"
            "```json\n"
            + json.dumps(
                valid_payload
            )
            + "\n```\n"
            "This should improve timing.\n"
        )

        candidate = extract_candidate(
            raw,
            bundle=bundle,
        )

        assert len(
            candidate.patches
        ) == 1

        assert (
            candidate.patches[
                0
            ].target_file
            == "rtl/top.v"
        )

        assert (
            candidate.bundle_validated
            is True
        )

        # ----------------------------------------------------
        # 2. Plain prose + JSON extraction
        # ----------------------------------------------------

        raw_prose = (
            "Candidate follows:\n"
            + json.dumps(
                valid_payload
            )
            + "\nEnd."
        )

        candidate_2 = extract_candidate(
            raw_prose,
            bundle=bundle,
        )

        assert len(
            candidate_2.patches
        ) == 1

        # ----------------------------------------------------
        # 3. Legacy rewritten_module must be rejected
        # ----------------------------------------------------

        legacy = {
            "chosen_pattern_id": "legacy",
            "changed_line_ranges": [
                [
                    1,
                    7,
                ]
            ],
            "rewritten_module": (
                "module top; endmodule"
            ),
            "rationale": "old",
            "self_checks": {},
        }

        legacy_rejected = False

        try:

            extract_candidate(
                json.dumps(
                    legacy
                )
            )

        except CandidateSchemaError as exc:

            legacy_rejected = (
                "rewritten_module"
                in str(exc)
            )

        assert legacy_rejected

        # ----------------------------------------------------
        # 4. Duplicate patch IDs rejected
        # ----------------------------------------------------

        duplicate = (
            make_valid_response(
                sha256=top_sha
            )
        )

        second_patch = dict(
            duplicate[
                "patches"
            ][0]
        )

        second_patch[
            "changed_source_range"
        ] = {
            "start_line": 5,
            "end_line": 5,
        }

        # Same patch_id intentionally.
        duplicate[
            "patches"
        ].append(
            second_patch
        )

        duplicate_rejected = False

        try:

            extract_candidate(
                json.dumps(
                    duplicate
                )
            )

        except CandidateSchemaError as exc:

            duplicate_rejected = (
                "duplicate patch_id"
                in str(exc)
            )

        assert duplicate_rejected

        # ----------------------------------------------------
        # 5. Overlapping ranges rejected
        # ----------------------------------------------------

        overlap = (
            make_valid_response(
                sha256=top_sha
            )
        )

        overlap[
            "patches"
        ][0][
            "changed_source_range"
        ] = {
            "start_line": 5,
            "end_line": 6,
        }

        overlap[
            "patches"
        ].append(
            {
                "patch_id": "patch_002",
                "patch_type": "line_range",
                "target_file": "rtl/top.v",
                "target_module": "top",
                "original_sha256": top_sha,
                "changed_source_range": {
                    "start_line": 6,
                    "end_line": 6,
                },
                "replacement_text": (
                    "assign y = a;\n"
                ),
            }
        )

        overlap_rejected = False

        try:

            extract_candidate(
                json.dumps(
                    overlap
                )
            )

        except CandidateSchemaError as exc:

            overlap_rejected = (
                "overlapping"
                in str(exc)
            )

        assert overlap_rejected

        # ----------------------------------------------------
        # 6. Stale source SHA rejected when bundle supplied
        # ----------------------------------------------------

        stale = (
            make_valid_response(
                sha256=(
                    "0" * 64
                )
            )
        )

        stale_rejected = False

        try:

            extract_candidate(
                json.dumps(
                    stale
                ),
                bundle=bundle,
            )

        except CandidateSchemaError as exc:

            stale_rejected = (
                "SHA"
                in str(exc)
                or "different source"
                in str(exc)
            )

        assert stale_rejected

        # ----------------------------------------------------
        # 7. Wrong module/file ownership rejected
        # ----------------------------------------------------

        wrong_file = (
            make_valid_response(
                sha256=(
                    bundle.get_source(
                        "rtl/helper.v"
                    ).sha256
                )
            )
        )

        wrong_file[
            "patches"
        ][0][
            "target_file"
        ] = "rtl/helper.v"

        wrong_file_rejected = False

        try:

            extract_candidate(
                json.dumps(
                    wrong_file
                ),
                bundle=bundle,
            )

        except CandidateSchemaError:

            wrong_file_rejected = True

        assert wrong_file_rejected

        # ----------------------------------------------------
        # 8. whole_module exact span accepted
        # ----------------------------------------------------

        whole = (
            make_valid_response(
                sha256=top_sha
            )
        )

        whole[
            "patches"
        ][0][
            "patch_type"
        ] = "whole_module"

        whole[
            "patches"
        ][0][
            "changed_source_range"
        ] = {
            "start_line": 1,
            "end_line": 7,
        }

        whole[
            "patches"
        ][0][
            "replacement_text"
        ] = (
            "module top (\n"
            "    input wire a,\n"
            "    input wire b,\n"
            "    output wire y\n"
            ");\n"
            "assign y = a | b;\n"
            "endmodule\n"
        )

        whole_candidate = (
            extract_candidate(
                json.dumps(
                    whole
                ),
                bundle=bundle,
            )
        )

        assert (
            whole_candidate.patch_types
            == (
                "whole_module",
            )
        )

        # ----------------------------------------------------
        # 9. Incorrect whole_module span rejected
        # ----------------------------------------------------

        bad_whole = (
            make_valid_response(
                sha256=top_sha
            )
        )

        bad_whole[
            "patches"
        ][0][
            "patch_type"
        ] = "whole_module"

        bad_whole[
            "patches"
        ][0][
            "changed_source_range"
        ] = {
            "start_line": 2,
            "end_line": 6,
        }

        bad_whole_rejected = False

        try:

            extract_candidate(
                json.dumps(
                    bad_whole
                ),
                bundle=bundle,
            )

        except CandidateSchemaError as exc:

            bad_whole_rejected = (
                "whole_module"
                in str(exc)
            )

        assert bad_whole_rejected

        # ----------------------------------------------------
        # 10. Output files
        # ----------------------------------------------------

        out_stem = (
            root
            / "candidate_001"
        )

        candidate_path, meta_path = (
            write_candidate(
                candidate,
                out_stem,
            )
        )

        assert (
            candidate_path.is_file()
        )

        assert (
            meta_path.is_file()
        )

        written = json.loads(
            candidate_path.read_text(
                encoding="utf-8"
            )
        )

        assert (
            written[
                "schema_version"
            ]
            == "2.0"
        )

        assert "patches" in written

        assert (
            "rewritten_module"
            not in written
        )

        print(
            "PASS: extract_candidate self-test"
        )

        print(
            "PASS: fenced/prose JSON extraction"
        )

        print(
            "PASS: response schema v2 validation"
        )

        print(
            "PASS: legacy rewritten_module rejected"
        )

        print(
            "PASS: patches converted to SourcePatch objects"
        )

        print(
            "PASS: duplicate patch IDs rejected"
        )

        print(
            "PASS: overlapping patches rejected"
        )

        print(
            "PASS: stale source SHA rejected"
        )

        print(
            "PASS: module/file ownership validated"
        )

        print(
            "PASS: whole_module exact-span validation"
        )

        print(
            "PASS: canonical candidate/meta JSON output"
        )


# ============================================================
# CLI
# ============================================================

def main() -> int:

    parser = argparse.ArgumentParser(
        description=(
            "Extract complete Verilog sources from "
            "an LLM optimizer response"
        )
    )

    parser.add_argument(
        "--response",
        help=(
            "file containing raw LLM response"
        ),
    )

    parser.add_argument(
        "--out",
        help=(
            "output stem, for example "
            "ai_runs/cand_001"
        ),
    )

    parser.add_argument(
        "--manifest",
        help=(
            "optional DesignManifest JSON for "
            "source-aware validation"
        ),
    )

    parser.add_argument(
        "--base-dir",
        default=".",
        help=(
            "base directory used for relative "
            "manifest source paths"
        ),
    )

    parser.add_argument(
        "--self-test",
        action="store_true",
        help=(
            "run standalone extractor regression tests"
        ),
    )

    args = parser.parse_args()

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
    # Normal CLI requires response/out
    # --------------------------------------------------------

    if not args.response:

        parser.error(
            "--response is required "
            "unless --self-test is used"
        )

    if not args.out:

        parser.error(
            "--out is required "
            "unless --self-test is used"
        )

    response_path = Path(
        args.response
    )

    if not response_path.is_file():

        print(
            f"ERROR: response file not found: "
            f"{response_path}",
            file=sys.stderr,
        )

        return 1

    try:

        raw_text = (
            response_path.read_text(
                encoding="utf-8"
            )
        )

    except OSError as exc:

        print(
            f"ERROR: failed to read "
            f"{response_path}: {exc}",
            file=sys.stderr,
        )

        return 1

    bundle: SourceBundle | None = None

    # --------------------------------------------------------
    # Optional bundle validation
    # --------------------------------------------------------

    if args.manifest:

        try:

            manifest = (
                DesignManifest.read_json(
                    Path(
                        args.manifest
                    )
                )
            )

            bundle = (
                SourceBundle.from_manifest(
                    manifest,
                    Path(
                        args.base_dir
                    ),
                )
            )

        except Exception as exc:

            print(
                "ERROR: failed to construct "
                f"SourceBundle: {exc}",
                file=sys.stderr,
            )

            return 1

    # --------------------------------------------------------
    # Extract
    # --------------------------------------------------------

    try:

        candidate = extract_candidate(
            raw_text,
            bundle=bundle,
        )

    except CandidateExtractionError as exc:

        print(
            "ERROR: candidate extraction failed:",
            file=sys.stderr,
        )

        print(
            str(exc),
            file=sys.stderr,
        )

        return 1

    # --------------------------------------------------------
    # Write
    # --------------------------------------------------------

    try:

        candidate_path, meta_path = (
            write_candidate(
                candidate,
                Path(
                    args.out
                ),
            )
        )

    except OSError as exc:

        print(
            f"ERROR: failed writing candidate: "
            f"{exc}",
            file=sys.stderr,
        )

        return 1

    summary = {
        "ok": True,
        "candidate_json": str(
            candidate_path
        ),
        "meta": str(
            meta_path
        ),
        "chosen_pattern_id": (
            candidate.chosen_pattern_id
        ),
        "target_unit_id": (
            candidate.target_unit_id
        ),
        "patch_count": len(
            candidate.patches
        ),
        "target_files": sorted(
            {
                patch.target_file
                for patch in candidate.patches
            }
        ),
        "target_modules": sorted(
            {
                patch.target_module
                for patch in candidate.patches
            }
        ),
        "bundle_validated": (
            candidate.bundle_validated
        ),
    }

    print(
        json.dumps(
            summary,
            indent=2,
        )
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(
        main()
    )