#!/usr/bin/env python3
"""
physical_recipe.py

Freeze and fingerprint the physical-flow recipe used for nEBULA PPA evaluation.

Why this exists
---------------
Baseline and candidate RTL are only comparable when the physical experiment is
the same. The RTL source files and FLOW_VARIANT are expected to differ, so they
must NOT participate in the physical-recipe fingerprint.

The fingerprint instead covers:

    - design identity
    - platform
    - evaluation stage
    - design config file contents
    - timing/physical constraint file contents
    - fixed flow variables
    - stage-specific settings
    - toolchain identity

The resulting SHA-256 is written into every baseline/candidate metric artifact
as:

    evaluation.recipe_fingerprint_sha256

evaluate_candidate.py refuses to compare artifacts whose fingerprints differ.

Input spec
----------
Recommended physical_recipe_spec/1:

{
  "schema": "physical_recipe_spec/1",
  "recipe_id": "mapper_reconvergent_nangate45_global_route",
  "design": {
    "design_id": "mapper_reconvergent_test",
    "top": "mapper_reconvergent_test",
    "platform": "nangate45"
  },
  "stage": "global_route",
  "files": [
    {
      "role": "design_config",
      "path": "designs/nangate45/mapper_reconvergent_test/config.mk"
    },
    {
      "role": "constraints",
      "path": "designs/nangate45/mapper_reconvergent_test/constraint.sdc"
    }
  ],
  "flow_variables": {
    "CORE_UTILIZATION": 40,
    "PLACE_DENSITY": 0.55,
    "ROUTING_LAYER_ADJUSTMENT": 0.5
  },
  "stage_settings": {
    "stop_after": "global_route"
  },
  "toolchain": {
    "orfs_revision": "<git commit>",
    "platform_revision": "nangate45",
    "yosys_revision": "<version>",
    "openroad_revision": "<version>"
  },
  "metadata": {
    "note": "metadata is recorded but excluded from the fingerprint"
  }
}

Normal usage
------------

python3 timing_automation/physical_recipe.py \
    --spec physical_recipe_spec.json \
    --base-dir <path-to-flow> \
    --output ai_runs/mapper_global_route.physical_recipe.json

Verify later that all frozen recipe files are still unchanged:

python3 timing_automation/physical_recipe.py \
    --verify-recipe ai_runs/mapper_global_route.physical_recipe.json

Self-test
---------

python3 timing_automation/physical_recipe.py --self-test
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence, Tuple


# =============================================================================
# Constants
# =============================================================================

SPEC_SCHEMA = "physical_recipe_spec/1"
RECIPE_SCHEMA = "physical_recipe/1"

VALID_STAGES = (
    "synth",
    "global_route",
    "final",
)

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

REQUIRED_FILE_ROLES = (
    "design_config",
    "constraints",
)


# =============================================================================
# Exceptions
# =============================================================================


class PhysicalRecipeError(RuntimeError):
    """Base physical-recipe error."""


class PhysicalRecipeValidationError(PhysicalRecipeError):
    """Recipe/spec validation error."""


class PhysicalRecipeMutationError(PhysicalRecipeError):
    """A frozen file no longer matches the recipe."""


# =============================================================================
# Canonical JSON / hashing
# =============================================================================


def _validate_json_value(value: Any, *, path: str = "$") -> None:
    """
    Reject values that cannot be represented deterministically as strict JSON.
    """

    if value is None or isinstance(value, (str, bool, int)):
        return

    if isinstance(value, float):
        if not math.isfinite(value):
            raise PhysicalRecipeValidationError(
                f"{path} contains non-finite float {value!r}"
            )
        return

    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_value(item, path=f"{path}[{index}]")
        return

    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise PhysicalRecipeValidationError(
                    f"{path} contains non-string JSON object key {key!r}"
                )
            _validate_json_value(item, path=f"{path}.{key}")
        return

    raise PhysicalRecipeValidationError(
        f"{path} contains unsupported JSON value type "
        f"{type(value).__name__}"
    )


def canonical_json_bytes(value: Any) -> bytes:
    _validate_json_value(value)

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(Path(path).read_bytes())


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            value,
            indent=2,
            sort_keys=False,
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
    )


def load_json(path: Path) -> Dict[str, Any]:
    path = Path(path)

    if not path.is_file():
        raise PhysicalRecipeValidationError(
            f"JSON file not found: {path}"
        )

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PhysicalRecipeValidationError(
            f"invalid JSON in {path}: {exc}"
        ) from exc

    if not isinstance(value, dict):
        raise PhysicalRecipeValidationError(
            f"JSON root must be an object: {path}"
        )

    return value


# =============================================================================
# File records
# =============================================================================


def normalize_logical_path(value: str) -> str:
    """
    Normalize a recipe logical path without resolving it to an absolute path.

    Absolute machine paths are allowed, but relative paths are strongly
    preferred because they produce machine-independent recipe fingerprints.
    """

    if not isinstance(value, str) or not value.strip():
        raise PhysicalRecipeValidationError(
            "recipe file path must be a non-empty string"
        )

    raw = value.strip().replace("\\", "/")

    # Preserve an absolute leading slash while collapsing simple duplicate
    # separators and "." components.
    absolute = raw.startswith("/")
    parts = []

    for part in raw.split("/"):
        if part in ("", "."):
            continue
        parts.append(part)

    if not parts:
        raise PhysicalRecipeValidationError(
            f"invalid recipe file path {value!r}"
        )

    normalized = "/".join(parts)

    if absolute:
        normalized = "/" + normalized

    return normalized


@dataclass(frozen=True)
class FrozenRecipeFile:
    role: str
    logical_path: str
    resolved_path: str
    sha256: str

    def validate(self) -> None:
        if not isinstance(self.role, str) or not self.role.strip():
            raise PhysicalRecipeValidationError(
                "frozen file role must be non-empty"
            )

        normalize_logical_path(self.logical_path)

        if not isinstance(self.resolved_path, str) or not self.resolved_path:
            raise PhysicalRecipeValidationError(
                "frozen file resolved_path must be non-empty"
            )

        if not isinstance(self.sha256, str) or not SHA256_RE.fullmatch(
            self.sha256.lower()
        ):
            raise PhysicalRecipeValidationError(
                f"invalid SHA-256 for {self.logical_path!r}"
            )

    def fingerprint_dict(self) -> Dict[str, str]:
        """
        resolved_path is intentionally excluded from the fingerprint.

        Baseline and candidate may be launched from different absolute
        directories while still using byte-identical recipe files.
        """

        return {
            "role": self.role,
            "path": self.logical_path,
            "sha256": self.sha256.lower(),
        }

    def to_dict(self) -> Dict[str, str]:
        return {
            "role": self.role,
            "path": self.logical_path,
            "resolved_path": self.resolved_path,
            "sha256": self.sha256.lower(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "FrozenRecipeFile":
        item = cls(
            role=str(value.get("role", "")),
            logical_path=str(value.get("path", "")),
            resolved_path=str(value.get("resolved_path", "")),
            sha256=str(value.get("sha256", "")).lower(),
        )
        item.validate()
        return item


def frozen_physical_variables(recipe):
    """Require explicit accepted geometry, never candidate-size-dependent utilization."""
    variables = dict(recipe.flow_variables)
    rectangles = {}
    for key in ("DIE_AREA", "CORE_AREA"):
        raw = variables.get(key)
        if not isinstance(raw, str):
            raise PhysicalRecipeValidationError(f"{key} must contain frozen baseline coordinates")
        try:
            coords = tuple(float(v) for v in raw.split())
        except ValueError as exc:
            raise PhysicalRecipeValidationError(f"invalid {key}") from exc
        if len(coords) != 4 or not all(math.isfinite(v) for v in coords) or coords[0] >= coords[2] or coords[1] >= coords[3]:
            raise PhysicalRecipeValidationError(f"invalid {key} rectangle")
        rectangles[key] = coords
    die, core = rectangles["DIE_AREA"], rectangles["CORE_AREA"]
    if not (die[0] <= core[0] < core[2] <= die[2] and die[1] <= core[1] < core[3] <= die[3]):
        raise PhysicalRecipeValidationError("CORE_AREA lies outside DIE_AREA")
    for key in ("CORE_UTILIZATION", "FLOORPLAN_DEF", "FOOTPRINT"):
        if variables.get(key) not in (None, ""):
            raise PhysicalRecipeValidationError(f"candidate recipe cannot use adaptive/alternative floorplan {key}")
        variables[key] = ""
    reserved = {"DESIGN_CONFIG", "FLOW_VARIANT", "VERILOG_FILES", "VERILOG_INCLUDE_DIRS", "VERILOG_DEFINES", "VERILOG_TOP_PARAMS"}
    for key, value in variables.items():
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or key in reserved:
            raise PhysicalRecipeValidationError(f"invalid physical variable {key}")
        if not isinstance(value, (str, int, float)) or isinstance(value, bool) or any(c in str(value) for c in "$\n\r"):
            raise PhysicalRecipeValidationError(f"physical variable {key} must be a literal scalar")
    return variables


# =============================================================================
# Physical recipe
# =============================================================================


@dataclass(frozen=True)
class PhysicalRecipe:
    recipe_id: str
    design_id: str
    top: str
    platform: str
    stage: str
    files: Tuple[FrozenRecipeFile, ...]
    flow_variables: Dict[str, Any]
    stage_settings: Dict[str, Any]
    toolchain: Dict[str, Any]
    source_base_dir: str
    metadata: Dict[str, Any]
    fingerprint_sha256: str

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate(self) -> None:
        for name, value in (
            ("recipe_id", self.recipe_id),
            ("design_id", self.design_id),
            ("top", self.top),
            ("platform", self.platform),
        ):
            if not isinstance(value, str) or not value.strip():
                raise PhysicalRecipeValidationError(
                    f"{name} must be a non-empty string"
                )

        if self.stage not in VALID_STAGES:
            raise PhysicalRecipeValidationError(
                f"stage must be one of {list(VALID_STAGES)}, "
                f"got {self.stage!r}"
            )

        if not self.files:
            raise PhysicalRecipeValidationError(
                "physical recipe must freeze at least one file"
            )

        seen_pairs = set()
        roles = set()

        for item in self.files:
            item.validate()

            pair = (item.role, item.logical_path)
            if pair in seen_pairs:
                raise PhysicalRecipeValidationError(
                    "duplicate frozen recipe file: "
                    f"role={item.role!r}, path={item.logical_path!r}"
                )
            seen_pairs.add(pair)
            roles.add(item.role)

        missing_roles = [
            role
            for role in REQUIRED_FILE_ROLES
            if role not in roles
        ]

        if missing_roles:
            raise PhysicalRecipeValidationError(
                "physical recipe is missing required file role(s): "
                + ", ".join(missing_roles)
            )

        for name, value in (
            ("flow_variables", self.flow_variables),
            ("stage_settings", self.stage_settings),
            ("toolchain", self.toolchain),
            ("metadata", self.metadata),
        ):
            if not isinstance(value, dict):
                raise PhysicalRecipeValidationError(
                    f"{name} must be a JSON object"
                )
            _validate_json_value(value, path=name)

        if not self.flow_variables:
            raise PhysicalRecipeValidationError(
                "flow_variables must not be empty; freeze the ORFS knobs "
                "that affect the physical experiment"
            )

        if not self.stage_settings:
            raise PhysicalRecipeValidationError(
                "stage_settings must not be empty"
            )

        if not self.toolchain:
            raise PhysicalRecipeValidationError(
                "toolchain must not be empty; record the ORFS/tool identity"
            )

        if (
            not isinstance(self.fingerprint_sha256, str)
            or not SHA256_RE.fullmatch(self.fingerprint_sha256.lower())
        ):
            raise PhysicalRecipeValidationError(
                "fingerprint_sha256 must be a 64-character SHA-256"
            )

        computed = self.compute_fingerprint()

        if computed != self.fingerprint_sha256.lower():
            raise PhysicalRecipeValidationError(
                "stored physical-recipe fingerprint is inconsistent: "
                f"stored={self.fingerprint_sha256}, computed={computed}"
            )

    # ------------------------------------------------------------------
    # Fingerprint
    # ------------------------------------------------------------------

    def fingerprint_payload(self) -> Dict[str, Any]:
        """
        Only physical-experiment invariants belong here.

        Excluded on purpose:
            - recipe_id
            - source_base_dir
            - metadata
            - resolved absolute paths
            - candidate/golden RTL files
            - FLOW_VARIANT
            - timestamps
        """

        return {
            "schema": RECIPE_SCHEMA,
            "design": {
                "design_id": self.design_id,
                "top": self.top,
                "platform": self.platform,
            },
            "stage": self.stage,
            "files": [
                item.fingerprint_dict()
                for item in sorted(
                    self.files,
                    key=lambda entry: (
                        entry.role,
                        entry.logical_path,
                    ),
                )
            ],
            "flow_variables": self.flow_variables,
            "stage_settings": self.stage_settings,
            "toolchain": self.toolchain,
        }

    def compute_fingerprint(self) -> str:
        return sha256_bytes(
            canonical_json_bytes(
                self.fingerprint_payload()
            )
        )

    # ------------------------------------------------------------------
    # Disk verification
    # ------------------------------------------------------------------

    def verify_frozen_files(
        self,
        *,
        base_dir: Path | None = None,
    ) -> Dict[str, Any]:
        """
        Verify current recipe files against the frozen hashes.

        If base_dir is omitted, source_base_dir from the recipe artifact is used.
        """

        root = (
            Path(base_dir).expanduser().resolve()
            if base_dir is not None
            else Path(self.source_base_dir).expanduser().resolve()
        )

        results = []
        ok = True

        for item in self.files:
            logical = Path(item.logical_path)

            path = (
                logical.expanduser().resolve()
                if logical.is_absolute()
                else (root / logical).resolve()
            )

            exists = path.is_file()
            actual_sha = sha256_file(path) if exists else None
            matches = exists and actual_sha == item.sha256.lower()

            results.append(
                {
                    "role": item.role,
                    "path": item.logical_path,
                    "resolved_path": str(path),
                    "exists": exists,
                    "expected_sha256": item.sha256.lower(),
                    "actual_sha256": actual_sha,
                    "matches": matches,
                }
            )

            if not matches:
                ok = False

        return {
            "ok": ok,
            "fingerprint_sha256": self.fingerprint_sha256.lower(),
            "base_dir": str(root),
            "files": results,
        }

    # ------------------------------------------------------------------
    # JSON
    # ------------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": RECIPE_SCHEMA,
            "recipe_id": self.recipe_id,
            "fingerprint_sha256": self.fingerprint_sha256.lower(),
            "design": {
                "design_id": self.design_id,
                "top": self.top,
                "platform": self.platform,
            },
            "stage": self.stage,
            "files": [
                item.to_dict()
                for item in self.files
            ],
            "flow_variables": self.flow_variables,
            "stage_settings": self.stage_settings,
            "toolchain": self.toolchain,
            "source_base_dir": self.source_base_dir,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PhysicalRecipe":
        if value.get("schema") != RECIPE_SCHEMA:
            raise PhysicalRecipeValidationError(
                f"recipe schema must be {RECIPE_SCHEMA!r}"
            )

        design = value.get("design")
        if not isinstance(design, Mapping):
            raise PhysicalRecipeValidationError(
                "recipe design must be an object"
            )

        raw_files = value.get("files")
        if not isinstance(raw_files, list):
            raise PhysicalRecipeValidationError(
                "recipe files must be an array"
            )

        files = tuple(
            FrozenRecipeFile.from_dict(item)
            for item in raw_files
            if isinstance(item, Mapping)
        )

        if len(files) != len(raw_files):
            raise PhysicalRecipeValidationError(
                "every recipe files[] item must be an object"
            )

        recipe = cls(
            recipe_id=str(value.get("recipe_id", "")),
            design_id=str(design.get("design_id", "")),
            top=str(design.get("top", "")),
            platform=str(design.get("platform", "")),
            stage=str(value.get("stage", "")),
            files=files,
            flow_variables=dict(value.get("flow_variables") or {}),
            stage_settings=dict(value.get("stage_settings") or {}),
            toolchain=dict(value.get("toolchain") or {}),
            source_base_dir=str(value.get("source_base_dir", "")),
            metadata=dict(value.get("metadata") or {}),
            fingerprint_sha256=str(
                value.get("fingerprint_sha256", "")
            ).lower(),
        )

        recipe.validate()
        return recipe

    @classmethod
    def read_json(cls, path: Path) -> "PhysicalRecipe":
        return cls.from_dict(load_json(path))

    def write_json(self, path: Path) -> None:
        self.validate()
        write_json(path, self.to_dict())


# =============================================================================
# Spec -> frozen recipe
# =============================================================================


def _required_nonempty_string(
    mapping: Mapping[str, Any],
    key: str,
    *,
    context: str,
) -> str:
    value = mapping.get(key)

    if not isinstance(value, str) or not value.strip():
        raise PhysicalRecipeValidationError(
            f"{context}.{key} must be a non-empty string"
        )

    return value.strip()


def _resolve_spec_file(
    *,
    base_dir: Path,
    item: Mapping[str, Any],
) -> FrozenRecipeFile:
    role = _required_nonempty_string(
        item,
        "role",
        context="files[]",
    )

    path_value = _required_nonempty_string(
        item,
        "path",
        context="files[]",
    )

    logical_path = normalize_logical_path(
        str(item.get("logical_path") or path_value)
    )

    source_path = Path(path_value).expanduser()

    resolved = (
        source_path.resolve()
        if source_path.is_absolute()
        else (base_dir / source_path).resolve()
    )

    if not resolved.is_file():
        raise PhysicalRecipeValidationError(
            f"recipe file not found: role={role!r}, path={resolved}"
        )

    return FrozenRecipeFile(
        role=role,
        logical_path=logical_path,
        resolved_path=str(resolved),
        sha256=sha256_file(resolved),
    )


def build_physical_recipe(
    spec: Mapping[str, Any],
    *,
    base_dir: Path,
) -> PhysicalRecipe:
    if spec.get("schema") != SPEC_SCHEMA:
        raise PhysicalRecipeValidationError(
            f"spec schema must be {SPEC_SCHEMA!r}"
        )

    base_dir = Path(base_dir).expanduser().resolve()

    design = spec.get("design")
    if not isinstance(design, Mapping):
        raise PhysicalRecipeValidationError(
            "spec.design must be an object"
        )

    recipe_id = _required_nonempty_string(
        spec,
        "recipe_id",
        context="spec",
    )

    design_id = _required_nonempty_string(
        design,
        "design_id",
        context="spec.design",
    )

    top = _required_nonempty_string(
        design,
        "top",
        context="spec.design",
    )

    platform = _required_nonempty_string(
        design,
        "platform",
        context="spec.design",
    )

    stage = _required_nonempty_string(
        spec,
        "stage",
        context="spec",
    )

    if stage not in VALID_STAGES:
        raise PhysicalRecipeValidationError(
            f"spec.stage must be one of {list(VALID_STAGES)}"
        )

    raw_files = spec.get("files")

    if not isinstance(raw_files, list) or not raw_files:
        raise PhysicalRecipeValidationError(
            "spec.files must be a non-empty array"
        )

    files_list = []

    for index, item in enumerate(raw_files):
        if not isinstance(item, Mapping):
            raise PhysicalRecipeValidationError(
                f"spec.files[{index}] must be an object"
            )

        files_list.append(
            _resolve_spec_file(
                base_dir=base_dir,
                item=item,
            )
        )

    flow_variables = spec.get("flow_variables")
    stage_settings = spec.get("stage_settings")
    toolchain = spec.get("toolchain")
    metadata = spec.get("metadata", {})

    for name, value in (
        ("flow_variables", flow_variables),
        ("stage_settings", stage_settings),
        ("toolchain", toolchain),
        ("metadata", metadata),
    ):
        if not isinstance(value, dict):
            raise PhysicalRecipeValidationError(
                f"spec.{name} must be an object"
            )
        _validate_json_value(value, path=f"spec.{name}")

    # Temporary placeholder. Fingerprint is computed from the immutable payload
    # immediately below.
    provisional = PhysicalRecipe(
        recipe_id=recipe_id,
        design_id=design_id,
        top=top,
        platform=platform,
        stage=stage,
        files=tuple(files_list),
        flow_variables=dict(flow_variables),
        stage_settings=dict(stage_settings),
        toolchain=dict(toolchain),
        source_base_dir=str(base_dir),
        metadata=dict(metadata),
        fingerprint_sha256="0" * 64,
    )

    fingerprint = provisional.compute_fingerprint()

    recipe = PhysicalRecipe(
        recipe_id=provisional.recipe_id,
        design_id=provisional.design_id,
        top=provisional.top,
        platform=provisional.platform,
        stage=provisional.stage,
        files=provisional.files,
        flow_variables=provisional.flow_variables,
        stage_settings=provisional.stage_settings,
        toolchain=provisional.toolchain,
        source_base_dir=provisional.source_base_dir,
        metadata=provisional.metadata,
        fingerprint_sha256=fingerprint,
    )

    recipe.validate()
    return recipe


# =============================================================================
# Recipe comparison
# =============================================================================


def compare_recipe_fingerprints(
    left: PhysicalRecipe,
    right: PhysicalRecipe,
) -> Dict[str, Any]:
    left.validate()
    right.validate()

    same = (
        left.fingerprint_sha256.lower()
        == right.fingerprint_sha256.lower()
    )

    return {
        "same_recipe": same,
        "left_fingerprint_sha256": left.fingerprint_sha256.lower(),
        "right_fingerprint_sha256": right.fingerprint_sha256.lower(),
        "left_stage": left.stage,
        "right_stage": right.stage,
        "left_design_id": left.design_id,
        "right_design_id": right.design_id,
        "left_platform": left.platform,
        "right_platform": right.platform,
    }


# =============================================================================
# Template
# =============================================================================


def recipe_spec_template() -> Dict[str, Any]:
    return {
        "schema": SPEC_SCHEMA,
        "recipe_id": "design_platform_stage_v1",
        "design": {
            "design_id": "YOUR_DESIGN_ID",
            "top": "YOUR_RTL_TOP",
            "platform": "nangate45",
        },
        "stage": "global_route",
        "files": [
            {
                "role": "design_config",
                "path": "designs/nangate45/YOUR_DESIGN/config.mk",
            },
            {
                "role": "constraints",
                "path": "designs/nangate45/YOUR_DESIGN/constraint.sdc",
            },
        ],
        "flow_variables": {
            "CORE_UTILIZATION": 40,
            "PLACE_DENSITY": 0.55,
        },
        "stage_settings": {
            "stop_after": "global_route",
        },
        "toolchain": {
            "orfs_revision": "REPLACE_WITH_GIT_COMMIT",
            "openroad_revision": "REPLACE_WITH_VERSION_OR_COMMIT",
            "yosys_revision": "REPLACE_WITH_VERSION_OR_COMMIT",
        },
        "metadata": {
            "note": (
                "Recorded for humans; metadata does not affect the "
                "recipe fingerprint."
            ),
        },
    }


# =============================================================================
# Self-test
# =============================================================================


def run_self_test() -> None:
    with tempfile.TemporaryDirectory(
        prefix="nebula_physical_recipe_"
    ) as tmp:
        root = Path(tmp).resolve()

        config = root / "designs" / "nangate45" / "demo" / "config.mk"
        sdc = root / "designs" / "nangate45" / "demo" / "constraint.sdc"

        config.parent.mkdir(parents=True, exist_ok=True)

        config.write_text(
            "export DESIGN_NAME = demo\n"
            "export PLATFORM = nangate45\n",
            encoding="utf-8",
        )

        sdc.write_text(
            "create_clock -name clk -period 10 [get_ports clk]\n",
            encoding="utf-8",
        )

        spec = {
            "schema": SPEC_SCHEMA,
            "recipe_id": "demo_nangate45_global_route_v1",
            "design": {
                "design_id": "demo",
                "top": "demo",
                "platform": "nangate45",
            },
            "stage": "global_route",
            "files": [
                {
                    "role": "design_config",
                    "path": "designs/nangate45/demo/config.mk",
                },
                {
                    "role": "constraints",
                    "path": "designs/nangate45/demo/constraint.sdc",
                },
            ],
            "flow_variables": {
                "CORE_UTILIZATION": 40,
                "PLACE_DENSITY": 0.55,
                "ROUTING_LAYER_ADJUSTMENT": 0.5,
            },
            "stage_settings": {
                "stop_after": "global_route",
            },
            "toolchain": {
                "orfs_revision": "abc123",
                "openroad_revision": "def456",
                "yosys_revision": "ghi789",
            },
            "metadata": {
                "created_by": "self-test",
            },
        }

        # ------------------------------------------------------------------
        # Deterministic creation.
        # ------------------------------------------------------------------

        first = build_physical_recipe(
            spec,
            base_dir=root,
        )

        second = build_physical_recipe(
            dict(reversed(list(spec.items()))),
            base_dir=root,
        )

        assert (
            first.fingerprint_sha256
            == second.fingerprint_sha256
        )

        # ------------------------------------------------------------------
        # JSON round-trip.
        # ------------------------------------------------------------------

        recipe_path = root / "recipe.json"
        first.write_json(recipe_path)

        round_trip = PhysicalRecipe.read_json(recipe_path)

        assert (
            round_trip.fingerprint_sha256
            == first.fingerprint_sha256
        )

        # ------------------------------------------------------------------
        # Disk verification.
        # ------------------------------------------------------------------

        verified = round_trip.verify_frozen_files()
        assert verified["ok"] is True

        # ------------------------------------------------------------------
        # Metadata does NOT change the fingerprint.
        # ------------------------------------------------------------------

        metadata_changed = dict(spec)
        metadata_changed["metadata"] = {
            "created_by": "different-human-note",
            "timestamp": "2099-01-01",
        }

        metadata_recipe = build_physical_recipe(
            metadata_changed,
            base_dir=root,
        )

        assert (
            metadata_recipe.fingerprint_sha256
            == first.fingerprint_sha256
        )

        # ------------------------------------------------------------------
        # recipe_id is a human label and MUST NOT change the fingerprint.
        # ------------------------------------------------------------------

        renamed_spec = json.loads(json.dumps(spec))
        renamed_spec["recipe_id"] = "same_physics_different_label"

        renamed_recipe = build_physical_recipe(
            renamed_spec,
            base_dir=root,
        )

        assert (
            renamed_recipe.fingerprint_sha256
            == first.fingerprint_sha256
        )

        # ------------------------------------------------------------------
        # FLOW_VARIANT is not part of the spec/fingerprint.
        # ------------------------------------------------------------------

        assert "flow_variant" not in first.fingerprint_payload()

        # ------------------------------------------------------------------
        # A physical variable MUST change the fingerprint.
        # ------------------------------------------------------------------

        variable_changed = json.loads(json.dumps(spec))
        variable_changed["flow_variables"]["PLACE_DENSITY"] = 0.60

        variable_recipe = build_physical_recipe(
            variable_changed,
            base_dir=root,
        )

        assert (
            variable_recipe.fingerprint_sha256
            != first.fingerprint_sha256
        )

        # ------------------------------------------------------------------
        # Stage MUST change the fingerprint.
        # ------------------------------------------------------------------

        stage_changed = json.loads(json.dumps(spec))
        stage_changed["stage"] = "final"
        stage_changed["stage_settings"]["stop_after"] = "final"

        stage_recipe = build_physical_recipe(
            stage_changed,
            base_dir=root,
        )

        assert (
            stage_recipe.fingerprint_sha256
            != first.fingerprint_sha256
        )

        # ------------------------------------------------------------------
        # Toolchain MUST change the fingerprint.
        # ------------------------------------------------------------------

        tool_changed = json.loads(json.dumps(spec))
        tool_changed["toolchain"]["openroad_revision"] = "new-openroad"

        tool_recipe = build_physical_recipe(
            tool_changed,
            base_dir=root,
        )

        assert (
            tool_recipe.fingerprint_sha256
            != first.fingerprint_sha256
        )

        # ------------------------------------------------------------------
        # Frozen config/constraint bytes MUST change the fingerprint.
        # ------------------------------------------------------------------

        old_fingerprint = first.fingerprint_sha256

        sdc.write_text(
            "create_clock -name clk -period 9 [get_ports clk]\n",
            encoding="utf-8",
        )

        mutated_disk = first.verify_frozen_files()
        assert mutated_disk["ok"] is False

        rebuilt = build_physical_recipe(
            spec,
            base_dir=root,
        )

        assert rebuilt.fingerprint_sha256 != old_fingerprint

        # ------------------------------------------------------------------
        # Missing required role rejected.
        # ------------------------------------------------------------------

        missing_constraint = json.loads(json.dumps(spec))
        missing_constraint["files"] = [
            missing_constraint["files"][0]
        ]

        try:
            build_physical_recipe(
                missing_constraint,
                base_dir=root,
            )
        except PhysicalRecipeValidationError as exc:
            assert "constraints" in str(exc)
        else:
            raise AssertionError(
                "missing constraints role should be rejected"
            )

        print("PASS: physical_recipe self-test")
        print("PASS: canonical recipe fingerprint is deterministic")
        print("PASS: config and constraint file bytes are frozen")
        print("PASS: flow variables participate in the fingerprint")
        print("PASS: evaluation stage participates in the fingerprint")
        print("PASS: toolchain identity participates in the fingerprint")
        print("PASS: metadata is excluded from the fingerprint")
        print("PASS: recipe_id is excluded from the fingerprint")
        print("PASS: FLOW_VARIANT is excluded from the fingerprint")
        print("PASS: frozen-file mutation is detected")
        print("PASS: JSON round-trip preserves the fingerprint")
        print("PASS: missing required physical recipe files rejected")


# =============================================================================
# CLI
# =============================================================================


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Freeze and fingerprint a nEBULA physical-flow recipe."
        )
    )

    mode = parser.add_mutually_exclusive_group()

    mode.add_argument(
        "--spec",
        default=None,
        help="physical_recipe_spec/1 JSON to freeze.",
    )

    mode.add_argument(
        "--verify-recipe",
        default=None,
        help=(
            "Existing physical_recipe/1 JSON whose frozen files should "
            "be re-hashed and verified."
        ),
    )

    mode.add_argument(
        "--write-template",
        default=None,
        help="Write an example physical_recipe_spec/1 JSON.",
    )

    mode.add_argument(
        "--self-test",
        action="store_true",
        help="Run standalone regression tests.",
    )

    parser.add_argument(
        "--base-dir",
        default=".",
        help=(
            "Base directory used to resolve relative spec file paths. "
            "For --verify-recipe, omit this to use the recipe's recorded "
            "source_base_dir."
        ),
    )

    parser.add_argument(
        "--output",
        default=None,
        help="Output physical_recipe/1 JSON when using --spec.",
    )

    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.self_test:
        try:
            run_self_test()
        except Exception as exc:
            print(
                f"SELF-TEST FAILED: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return 1
        return 0

    if args.write_template:
        try:
            write_json(
                Path(args.write_template),
                recipe_spec_template(),
            )
        except Exception as exc:
            print(
                f"ERROR: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return 2

        print(
            json.dumps(
                recipe_spec_template(),
                indent=2,
            )
        )
        return 0

    if args.spec:
        if not args.output:
            print(
                "ERROR: --output is required with --spec",
                file=sys.stderr,
            )
            return 2

        try:
            spec = load_json(Path(args.spec))

            recipe = build_physical_recipe(
                spec,
                base_dir=Path(args.base_dir),
            )

            recipe.write_json(Path(args.output))

            print(
                json.dumps(
                    recipe.to_dict(),
                    indent=2,
                )
            )

            return 0

        except PhysicalRecipeError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        except Exception as exc:
            print(
                f"ERROR: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return 2

    if args.verify_recipe:
        try:
            recipe = PhysicalRecipe.read_json(
                Path(args.verify_recipe)
            )

            # argparse always supplies a default ".", so only use the CLI
            # base-dir when the caller explicitly changed it from ".".
            verify_base = (
                Path(args.base_dir)
                if args.base_dir != "."
                else None
            )

            result = recipe.verify_frozen_files(
                base_dir=verify_base,
            )

            print(
                json.dumps(
                    result,
                    indent=2,
                )
            )

            return 0 if result["ok"] else 1

        except PhysicalRecipeError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        except Exception as exc:
            print(
                f"ERROR: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return 2

    print(
        "ERROR: choose one of --spec, --verify-recipe, "
        "--write-template, or --self-test",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
