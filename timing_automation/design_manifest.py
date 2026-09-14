#!/usr/bin/env python3
"""
Canonical dataset-independent design manifest for nEBULA.

The purpose of DesignManifest is to prevent downstream pipeline stages
from understanding dataset-specific formats such as DR.RTL's:

    [top, clk, rst, ext]

Dataset adapters should convert their native metadata into this schema.
Everything downstream should eventually consume DesignManifest.

This file intentionally has no dependency on DR.RTL, RTLLM, ORFS,
Yosys, or OpenROAD.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping


# ============================================================
# Constants
# ============================================================

SCHEMA_VERSION = "1.0"

SUPPORTED_LANGUAGES = {
    "verilog",
    "systemverilog",
    "vhdl",
    "mixed",
}

SUPPORTED_EVALUATION_STAGES = {
    "synth",
    "global_route",
    "final",
}

SUPPORTED_VERIFICATION_MODES = {
    "combinational_cec",
    "sequential_equivalence",
    "backend_unsupported",
}


# ============================================================
# Exceptions
# ============================================================

class ManifestError(ValueError):
    """Raised when a DesignManifest is invalid."""


# ============================================================
# Hash helpers
# ============================================================

def sha256_file(path: Path) -> str:
    """
    Return SHA-256 for one source file.

    Files are hashed independently. This is important because future
    SourceBundle logic must be able to detect exactly which source file
    changed.
    """

    path = Path(path)

    if not path.is_file():
        raise FileNotFoundError(
            f"cannot hash missing source file: {path}"
        )

    digest = hashlib.sha256()

    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)

            if not chunk:
                break

            digest.update(chunk)

    return digest.hexdigest()


def is_sha256(value: str) -> bool:
    """
    Return True when value looks like a lowercase/uppercase SHA-256 hex
    digest.
    """

    if not isinstance(value, str):
        return False

    if len(value) != 64:
        return False

    try:
        int(value, 16)
    except ValueError:
        return False

    return True


# ============================================================
# Clock specification
# ============================================================

@dataclass
class ClockSpec:
    """
    Canonical clock description.

    More advanced generated-clock/domain relationships can later be
    added without changing dataset adapters.
    """

    name: str
    port: str
    period_ns: float

    uncertainty_ns: float = 0.0
    virtual: bool = False
    waveform_ns: List[float] = field(default_factory=list)

    metadata: Dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:

        if not self.name:
            raise ManifestError(
                "clock name must not be empty"
            )

        if not self.port and not self.virtual:
            raise ManifestError(
                f"clock {self.name!r} has empty port"
            )

        import math
        if not math.isfinite(self.uncertainty_ns) or self.uncertainty_ns < 0:
            raise ManifestError("clock uncertainty must be finite and nonnegative")
        if self.virtual and self.port:
            raise ManifestError("virtual clock cannot have a port")
        if not math.isfinite(self.period_ns) or self.period_ns <= 0:
            raise ManifestError(
                f"clock {self.name!r} period must be > 0"
            )

        if self.waveform_ns:

            if len(self.waveform_ns) != 2:
                raise ManifestError(
                    f"clock {self.name!r} waveform must contain "
                    "exactly two values"
                )

            rise, fall = self.waveform_ns

            if rise < 0 or fall < 0:
                raise ManifestError(
                    f"clock {self.name!r} waveform cannot be negative"
                )

    def to_dict(self) -> Dict[str, Any]:

        result: Dict[str, Any] = {
            "name": self.name,
            "port": self.port,
            "period_ns": self.period_ns,
        }

        if self.waveform_ns:
            result["waveform_ns"] = list(
                self.waveform_ns
            )

        if self.metadata:
            result["metadata"] = dict(
                self.metadata
            )

        return result

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
    ) -> "ClockSpec":

        clock = cls(
            name=str(data["name"]),
            port=str(data.get("port", "")),
            uncertainty_ns=float(data.get("uncertainty_ns", 0.0)),
            virtual=data.get("virtual", False),
            period_ns=float(
                data["period_ns"]
            ),
            waveform_ns=[
                float(value)
                for value in data.get(
                    "waveform_ns",
                    [],
                )
            ],
            metadata=dict(
                data.get(
                    "metadata",
                    {},
                )
            ),
        )

        clock.validate()

        return clock


# ============================================================
# Reset specification
# ============================================================

@dataclass
class ResetSpec:
    """
    Canonical reset description.
    """

    name: str
    port: str

    active_level: int = 1
    asynchronous: bool = False

    metadata: Dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:

        if not self.name:
            raise ManifestError(
                "reset name must not be empty"
            )

        if not self.port:
            raise ManifestError(
                f"reset {self.name!r} has empty port"
            )

        if self.active_level not in {0, 1}:
            raise ManifestError(
                f"reset {self.name!r} active_level "
                "must be 0 or 1"
            )

    def to_dict(self) -> Dict[str, Any]:

        result: Dict[str, Any] = {
            "name": self.name,
            "port": self.port,
            "active_level": self.active_level,
            "asynchronous": self.asynchronous,
        }

        if self.metadata:
            result["metadata"] = dict(
                self.metadata
            )

        return result

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
    ) -> "ResetSpec":

        reset = cls(
            name=str(data["name"]),
            port=str(data["port"]),
            active_level=int(
                data.get(
                    "active_level",
                    1,
                )
            ),
            asynchronous=bool(
                data.get(
                    "asynchronous",
                    False,
                )
            ),
            metadata=dict(
                data.get(
                    "metadata",
                    {},
                )
            ),
        )

        reset.validate()

        return reset


# ============================================================
# Verification policy
# ============================================================

@dataclass
class VerificationPolicy:
    """
    Formal/equivalence verification configuration.

    Example:

        backend = "yosys"
        mode    = "sequential_equivalence"
    """

    backend: str = "yosys"

    mode: str = "sequential_equivalence"

    options: Dict[str, Any] = field(
        default_factory=dict
    )

    def validate(self) -> None:

        if not self.backend:
            raise ManifestError(
                "verification backend must not be empty"
            )

        if self.mode not in SUPPORTED_VERIFICATION_MODES:
            raise ManifestError(
                "unsupported verification mode "
                f"{self.mode!r}"
            )

    def to_dict(self) -> Dict[str, Any]:

        return {
            "backend": self.backend,
            "mode": self.mode,
            "options": dict(self.options),
        }

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
    ) -> "VerificationPolicy":

        policy = cls(
            backend=str(
                data.get(
                    "backend",
                    "yosys",
                )
            ),
            mode=str(
                data.get(
                    "mode",
                    "sequential_equivalence",
                )
            ),
            options=dict(
                data.get(
                    "options",
                    {},
                )
            ),
        )

        policy.validate()

        return policy


# ============================================================
# Physical policy
# ============================================================

@dataclass
class PhysicalPolicy:
    """
    Physical evaluation configuration.

    Detailed baseline floorplan freezing will be implemented later.
    This object establishes the canonical place where that information
    belongs.
    """

    platform: str = "nangate45"

    evaluation_stage: str = "final"

    freeze_after_baseline: bool = True

    settings: Dict[str, Any] = field(
        default_factory=dict
    )

    def validate(self) -> None:

        if not self.platform:
            raise ManifestError(
                "physical platform must not be empty"
            )

        if (
            self.evaluation_stage
            not in SUPPORTED_EVALUATION_STAGES
        ):
            raise ManifestError(
                "unsupported evaluation_stage "
                f"{self.evaluation_stage!r}"
            )

    def to_dict(self) -> Dict[str, Any]:

        return {
            "platform": self.platform,
            "evaluation_stage": self.evaluation_stage,
            "freeze_after_baseline": self.freeze_after_baseline,
            "settings": dict(self.settings),
        }

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
    ) -> "PhysicalPolicy":

        policy = cls(
            platform=str(
                data.get(
                    "platform",
                    "nangate45",
                )
            ),
            evaluation_stage=str(
                data.get(
                    "evaluation_stage",
                    "final",
                )
            ),
            freeze_after_baseline=bool(
                data.get(
                    "freeze_after_baseline",
                    True,
                )
            ),
            settings=dict(
                data.get(
                    "settings",
                    {},
                )
            ),
        )

        policy.validate()

        return policy


# ============================================================
# DesignManifest
# ============================================================

@dataclass
class DesignManifest:
    """
    Dataset-independent canonical design description.

    Required canonical concepts:

        dataset
        design_id
        top
        source_files[]
        include_dirs[]
        defines[]
        parameters
        language
        clocks[]
        resets[]
        constraints
        verification
        physical
        source_hashes

    source_files are stored as strings so manifests remain portable
    JSON objects.

    Relative paths are resolved relative to a caller-supplied base
    directory.
    """

    dataset: str

    design_id: str

    top: str

    source_files: List[str]

    language: str

    include_dirs: List[str] = field(
        default_factory=list
    )

    defines: List[str] = field(
        default_factory=list
    )

    parameters: Dict[str, Any] = field(
        default_factory=dict
    )

    clocks: List[ClockSpec] = field(
        default_factory=list
    )

    resets: List[ResetSpec] = field(
        default_factory=list
    )

    constraints: Dict[str, Any] = field(
        default_factory=dict
    )

    verification: VerificationPolicy = field(
        default_factory=VerificationPolicy
    )

    physical: PhysicalPolicy = field(
        default_factory=PhysicalPolicy
    )

    source_hashes: Dict[str, str] = field(
        default_factory=dict
    )

    metadata: Dict[str, Any] = field(
        default_factory=dict
    )

    schema_version: str = SCHEMA_VERSION

    # --------------------------------------------------------
    # Validation
    # --------------------------------------------------------

    def validate(
        self,
        *,
        require_source_hashes: bool = True,
    ) -> None:

        if self.schema_version != SCHEMA_VERSION:
            raise ManifestError(
                "unsupported manifest schema_version "
                f"{self.schema_version!r}; "
                f"expected {SCHEMA_VERSION!r}"
            )

        if not self.dataset:
            raise ManifestError(
                "dataset must not be empty"
            )

        if not self.design_id:
            raise ManifestError(
                "design_id must not be empty"
            )

        if not self.top:
            raise ManifestError(
                "top must not be empty"
            )

        if self.language not in SUPPORTED_LANGUAGES:
            raise ManifestError(
                f"unsupported language {self.language!r}"
            )

        if not self.source_files:
            raise ManifestError(
                "source_files must contain at least one source"
            )

        if len(self.source_files) != len(
            set(self.source_files)
        ):
            raise ManifestError(
                "source_files contains duplicates"
            )

        if len(self.include_dirs) != len(
            set(self.include_dirs)
        ):
            raise ManifestError(
                "include_dirs contains duplicates"
            )

        if len(self.defines) != len(
            set(self.defines)
        ):
            raise ManifestError(
                "defines contains duplicates"
            )

        for clock in self.clocks:
            clock.validate()

        for reset in self.resets:
            reset.validate()

        self.verification.validate()

        self.physical.validate()

        # ----------------------------------------------------
        # Hash validation
        # ----------------------------------------------------

        unknown_hashes = (
            set(self.source_hashes)
            - set(self.source_files)
        )

        if unknown_hashes:
            raise ManifestError(
                "source_hashes contains entries for files "
                "not present in source_files: "
                + ", ".join(
                    sorted(unknown_hashes)
                )
            )

        for source, digest in self.source_hashes.items():

            if not is_sha256(digest):
                raise ManifestError(
                    f"invalid SHA-256 for {source!r}: "
                    f"{digest!r}"
                )

        if require_source_hashes:

            missing_hashes = (
                set(self.source_files)
                - set(self.source_hashes)
            )

            if missing_hashes:
                raise ManifestError(
                    "missing source SHA-256 values for: "
                    + ", ".join(
                        sorted(missing_hashes)
                    )
                )

    # --------------------------------------------------------
    # Path handling
    # --------------------------------------------------------

    @staticmethod
    def resolve_path(
        value: str,
        base_dir: Path,
    ) -> Path:

        path = Path(value)

        if path.is_absolute():
            return path.resolve()

        return (
            Path(base_dir)
            / path
        ).resolve()

    def resolved_source_files(
        self,
        base_dir: Path,
    ) -> List[Path]:

        return [
            self.resolve_path(
                source,
                base_dir,
            )
            for source in self.source_files
        ]

    def resolved_include_dirs(
        self,
        base_dir: Path,
    ) -> List[Path]:

        return [
            self.resolve_path(
                directory,
                base_dir,
            )
            for directory in self.include_dirs
        ]

    # --------------------------------------------------------
    # Source hashing
    # --------------------------------------------------------

    def refresh_source_hashes(
        self,
        base_dir: Path,
    ) -> Dict[str, str]:
        """
        Recompute SHA-256 independently for every source.
        """

        hashes: Dict[str, str] = {}

        for source in self.source_files:

            path = self.resolve_path(
                source,
                base_dir,
            )

            hashes[source] = sha256_file(
                path
            )

        self.source_hashes = hashes

        return dict(hashes)

    def verify_source_hashes(
        self,
        base_dir: Path,
    ) -> Dict[str, bool]:
        """
        Verify current files against hashes stored in this manifest.

        Returns:

            {
                "rtl/top.v": True,
                "rtl/helper.v": False
            }
        """

        result: Dict[str, bool] = {}

        for source in self.source_files:

            expected = self.source_hashes.get(
                source
            )

            if expected is None:
                result[source] = False
                continue

            path = self.resolve_path(
                source,
                base_dir,
            )

            if not path.is_file():
                result[source] = False
                continue

            result[source] = (
                sha256_file(path).lower()
                == expected.lower()
            )

        return result

    # --------------------------------------------------------
    # JSON conversion
    # --------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:

        self.validate()

        return {
            "schema_version": self.schema_version,
            "dataset": self.dataset,
            "design_id": self.design_id,
            "top": self.top,
            "source_files": list(
                self.source_files
            ),
            "include_dirs": list(
                self.include_dirs
            ),
            "defines": list(
                self.defines
            ),
            "parameters": dict(
                self.parameters
            ),
            "language": self.language,
            "clocks": [
                clock.to_dict()
                for clock in self.clocks
            ],
            "resets": [
                reset.to_dict()
                for reset in self.resets
            ],
            "constraints": dict(
                self.constraints
            ),
            "verification": (
                self.verification.to_dict()
            ),
            "physical": (
                self.physical.to_dict()
            ),
            "source_hashes": dict(
                self.source_hashes
            ),
            "metadata": dict(
                self.metadata
            ),
        }

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
    ) -> "DesignManifest":

        required_fields = {
            "dataset",
            "design_id",
            "top",
            "source_files",
            "language",
        }

        missing = required_fields - set(
            data.keys()
        )

        if missing:
            raise ManifestError(
                "manifest missing required fields: "
                + ", ".join(
                    sorted(missing)
                )
            )

        manifest = cls(
            schema_version=str(
                data.get(
                    "schema_version",
                    SCHEMA_VERSION,
                )
            ),
            dataset=str(
                data["dataset"]
            ),
            design_id=str(
                data["design_id"]
            ),
            top=str(
                data["top"]
            ),
            source_files=[
                str(value)
                for value in data[
                    "source_files"
                ]
            ],
            include_dirs=[
                str(value)
                for value in data.get(
                    "include_dirs",
                    [],
                )
            ],
            defines=[
                str(value)
                for value in data.get(
                    "defines",
                    [],
                )
            ],
            parameters=dict(
                data.get(
                    "parameters",
                    {},
                )
            ),
            language=str(
                data["language"]
            ),
            clocks=[
                ClockSpec.from_dict(
                    value
                )
                for value in data.get(
                    "clocks",
                    []
                )
            ],
            resets=[
                ResetSpec.from_dict(
                    value
                )
                for value in data.get(
                    "resets",
                    []
                )
            ],
            constraints=dict(
                data.get(
                    "constraints",
                    {},
                )
            ),
            verification=(
                VerificationPolicy.from_dict(
                    data.get(
                        "verification",
                        {},
                    )
                )
            ),
            physical=(
                PhysicalPolicy.from_dict(
                    data.get(
                        "physical",
                        {},
                    )
                )
            ),
            source_hashes={
                str(key): str(value)
                for key, value in dict(
                    data.get(
                        "source_hashes",
                        {},
                    )
                ).items()
            },
            metadata=dict(
                data.get(
                    "metadata",
                    {},
                )
            ),
        )

        manifest.validate()

        return manifest

    # --------------------------------------------------------
    # File IO
    # --------------------------------------------------------

    def write_json(
        self,
        path: Path,
    ) -> None:

        path = Path(path)

        path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        payload = self.to_dict()

        path.write_text(
            json.dumps(
                payload,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    @classmethod
    def read_json(
        cls,
        path: Path,
    ) -> "DesignManifest":

        path = Path(path)

        if not path.is_file():
            raise FileNotFoundError(
                f"manifest not found: {path}"
            )

        try:
            data = json.loads(
                path.read_text(
                    encoding="utf-8"
                )
            )

        except json.JSONDecodeError as exc:
            raise ManifestError(
                f"invalid manifest JSON "
                f"{path}: {exc}"
            ) from exc

        if not isinstance(data, dict):
            raise ManifestError(
                "manifest JSON root must be an object"
            )

        return cls.from_dict(data)


# ============================================================
# Self-test
# ============================================================

def run_self_test() -> None:
    """
    Completely standalone DesignManifest regression test.

    No ORFS/Yosys/OpenROAD/Groq required.
    """

    with tempfile.TemporaryDirectory() as tmp:

        root = Path(tmp)

        rtl_dir = root / "rtl"

        rtl_dir.mkdir()

        top_file = rtl_dir / "top.v"
        helper_file = rtl_dir / "helper.v"

        top_file.write_text(
            (
                "module top(\n"
                "    input wire clk,\n"
                "    input wire rst,\n"
                "    output wire y\n"
                ");\n"
                "assign y = rst;\n"
                "endmodule\n"
            ),
            encoding="utf-8",
        )

        helper_file.write_text(
            (
                "module helper(\n"
                "    input wire a,\n"
                "    output wire y\n"
                ");\n"
                "assign y = a;\n"
                "endmodule\n"
            ),
            encoding="utf-8",
        )

        manifest = DesignManifest(
            dataset="self_test",
            design_id="example_design",
            top="top",
            source_files=[
                "rtl/top.v",
                "rtl/helper.v",
            ],
            include_dirs=[
                "rtl",
            ],
            defines=[
                "NEBULA_TEST=1",
            ],
            parameters={
                "WIDTH": 32,
            },
            language="verilog",
            clocks=[
                ClockSpec(
                    name="clk",
                    port="clk",
                    period_ns=1.0,
                )
            ],
            resets=[
                ResetSpec(
                    name="rst",
                    port="rst",
                    active_level=1,
                    asynchronous=False,
                )
            ],
            constraints={
                "profile": "self_test",
            },
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
                    freeze_after_baseline=True,
                )
            ),
        )

        # Every source must be hashed independently.
        hashes = manifest.refresh_source_hashes(
            root
        )

        assert len(hashes) == 2

        assert (
            hashes["rtl/top.v"]
            != hashes["rtl/helper.v"]
        )

        manifest.validate()

        # Verify current source integrity.
        verification = (
            manifest.verify_source_hashes(
                root
            )
        )

        assert verification == {
            "rtl/top.v": True,
            "rtl/helper.v": True,
        }

        # JSON round trip.
        manifest_path = (
            root
            / "design_manifest.json"
        )

        manifest.write_json(
            manifest_path
        )

        loaded = (
            DesignManifest.read_json(
                manifest_path
            )
        )

        assert (
            loaded.to_dict()
            == manifest.to_dict()
        )

        # Modify only one source.
        helper_file.write_text(
            (
                "module helper(\n"
                "    input wire a,\n"
                "    output wire y\n"
                ");\n"
                "assign y = ~a;\n"
                "endmodule\n"
            ),
            encoding="utf-8",
        )

        changed = (
            loaded.verify_source_hashes(
                root
            )
        )

        assert changed == {
            "rtl/top.v": True,
            "rtl/helper.v": False,
        }

        print(
            "PASS: DesignManifest self-test"
        )

        print(
            "PASS: multi-source independent hashing"
        )

        print(
            "PASS: manifest JSON round-trip"
        )

        print(
            "PASS: per-source mutation detection"
        )


# ============================================================
# CLI
# ============================================================

def main() -> int:

    parser = argparse.ArgumentParser(
        description=(
            "nEBULA canonical DesignManifest utility"
        )
    )

    parser.add_argument(
        "--self-test",
        action="store_true",
        help=(
            "Run standalone DesignManifest tests"
        ),
    )

    parser.add_argument(
        "--validate",
        metavar="MANIFEST.json",
        help=(
            "Validate an existing DesignManifest JSON"
        ),
    )

    args = parser.parse_args()

    if args.self_test:

        run_self_test()

        return 0

    if args.validate:

        path = Path(
            args.validate
        )

        manifest = (
            DesignManifest.read_json(
                path
            )
        )

        print(
            "VALID DesignManifest"
        )

        print(
            f"dataset      : "
            f"{manifest.dataset}"
        )

        print(
            f"design_id    : "
            f"{manifest.design_id}"
        )

        print(
            f"top          : "
            f"{manifest.top}"
        )

        print(
            f"source files : "
            f"{len(manifest.source_files)}"
        )

        return 0

    parser.print_help()

    return 0


if __name__ == "__main__":
    raise SystemExit(
        main()
    )
