#!/usr/bin/env python3
"""
sec_bundle.py

Canonical SEC (sequential-equivalence-checking) bundle for nEBULA.

This file is intentionally a DESCRIPTION layer, not the proof backend.
It freezes everything a verification backend needs to compare an accepted
candidate against its golden design:

    - complete source filelists
    - include directories
    - preprocessor defines
    - top module
    - language
    - top-level parameters
    - clocks / resets metadata
    - independent source SHA-256 hashes
    - verification backend
    - requested verification mode (the achieved proof strength is reported by verify_rtl.py)

Supported verification modes:

    combinational_cec
    sequential_equivalence
    backend_unsupported

The next step, verify_rtl.py, will consume this bundle instead of assuming a
single golden .v and a single candidate .v.

Normal usage
------------

python3 timing_automation/sec_bundle.py \
    --golden-manifest path/to/golden/design_manifest.json \
    --golden-base-dir . \
    --candidate-manifest ai_runs/cand_0001_sources/design_manifest.json \
    --candidate-base-dir ai_runs/cand_0001_sources \
    --output ai_runs/cand_0001.sec_bundle.json

Self-test
---------

python3 timing_automation/sec_bundle.py --self-test
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence, Tuple


# =============================================================================
# Local imports
# =============================================================================

try:
    from design_manifest import (
        DesignManifest,
        VerificationPolicy,
    )
except ImportError:
    from timing_automation.design_manifest import (
        DesignManifest,
        VerificationPolicy,
    )


# =============================================================================
# Constants
# =============================================================================

SEC_BUNDLE_SCHEMA = "sec_bundle/1"

SUPPORTED_MODES = {
    "combinational_cec",
    "sequential_equivalence",
    "backend_unsupported",
}


# =============================================================================
# Exceptions
# =============================================================================


class SECBundleError(RuntimeError):
    """Raised when a safe SEC bundle cannot be constructed or validated."""


# =============================================================================
# Generic helpers
# =============================================================================


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    path = Path(path)
    return sha256_bytes(path.read_bytes())


def canonical_json_sha256(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return sha256_bytes(payload)


def is_sha256(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )


def load_json(path: Path) -> Dict[str, Any]:
    path = Path(path)
    if not path.is_file():
        raise SECBundleError(f"JSON file not found: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SECBundleError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SECBundleError(f"JSON root must be an object: {path}")
    return value


def clock_dicts(manifest: DesignManifest) -> List[Dict[str, Any]]:
    return [clock.to_dict() for clock in manifest.clocks]


def reset_dicts(manifest: DesignManifest) -> List[Dict[str, Any]]:
    return [reset.to_dict() for reset in manifest.resets]


# =============================================================================
# Source-file description
# =============================================================================


@dataclass(frozen=True)
class SECSourceFile:
    manifest_path: str
    resolved_path: str
    sha256: str

    def validate(self) -> None:
        if not self.manifest_path:
            raise SECBundleError("SEC source manifest_path must be non-empty")
        if not self.resolved_path:
            raise SECBundleError("SEC source resolved_path must be non-empty")
        if not is_sha256(self.sha256):
            raise SECBundleError(
                f"invalid SEC source SHA-256 for {self.manifest_path!r}"
            )

    def verify_on_disk(self) -> None:
        self.validate()
        path = Path(self.resolved_path)
        if not path.is_file():
            raise SECBundleError(f"SEC source missing on disk: {path}")
        actual = sha256_file(path)
        if actual.lower() != self.sha256.lower():
            raise SECBundleError(
                f"SEC source hash mismatch for {self.manifest_path!r}: "
                f"bundle={self.sha256}, disk={actual}"
            )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "manifest_path": self.manifest_path,
            "resolved_path": self.resolved_path,
            "sha256": self.sha256,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SECSourceFile":
        source = cls(
            manifest_path=str(value.get("manifest_path", "")),
            resolved_path=str(value.get("resolved_path", "")),
            sha256=str(value.get("sha256", "")),
        )
        source.validate()
        return source


# =============================================================================
# Golden/candidate compile descriptions
# =============================================================================


@dataclass(frozen=True)
class SECDesignSide:
    role: str
    dataset: str
    design_id: str
    top: str
    language: str
    base_dir: str
    sources: Tuple[SECSourceFile, ...]
    include_dirs: Tuple[str, ...]
    defines: Tuple[str, ...]
    parameters: Dict[str, Any]
    clocks: Tuple[Dict[str, Any], ...]
    resets: Tuple[Dict[str, Any], ...]
    manifest_fingerprint_sha256: str

    def validate(self) -> None:
        if self.role not in {"golden", "candidate"}:
            raise SECBundleError(f"invalid SEC side role: {self.role!r}")
        if not self.dataset:
            raise SECBundleError(f"{self.role}: dataset must be non-empty")
        if not self.design_id:
            raise SECBundleError(f"{self.role}: design_id must be non-empty")
        if not self.top:
            raise SECBundleError(f"{self.role}: top must be non-empty")
        if self.language not in {"verilog", "systemverilog", "vhdl", "mixed"}:
            raise SECBundleError(
                f"{self.role}: unsupported language {self.language!r}"
            )
        if not self.base_dir:
            raise SECBundleError(f"{self.role}: base_dir must be non-empty")
        if not self.sources:
            raise SECBundleError(f"{self.role}: source filelist must not be empty")
        if not is_sha256(self.manifest_fingerprint_sha256):
            raise SECBundleError(
                f"{self.role}: invalid manifest_fingerprint_sha256"
            )

        seen_manifest_paths = set()
        seen_resolved_paths = set()

        for source in self.sources:
            source.validate()
            if source.manifest_path in seen_manifest_paths:
                raise SECBundleError(
                    f"{self.role}: duplicate manifest source path "
                    f"{source.manifest_path!r}"
                )
            if source.resolved_path in seen_resolved_paths:
                raise SECBundleError(
                    f"{self.role}: duplicate resolved source path "
                    f"{source.resolved_path!r}"
                )
            seen_manifest_paths.add(source.manifest_path)
            seen_resolved_paths.add(source.resolved_path)

        if len(set(self.include_dirs)) != len(self.include_dirs):
            raise SECBundleError(f"{self.role}: duplicate include directories")
        if len(set(self.defines)) != len(self.defines):
            raise SECBundleError(f"{self.role}: duplicate defines")

    def verify_files(self) -> None:
        self.validate()
        for source in self.sources:
            source.verify_on_disk()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "role": self.role,
            "dataset": self.dataset,
            "design_id": self.design_id,
            "top": self.top,
            "language": self.language,
            "base_dir": self.base_dir,
            "sources": [source.to_dict() for source in self.sources],
            "include_dirs": list(self.include_dirs),
            "defines": list(self.defines),
            "parameters": dict(self.parameters),
            "clocks": [dict(clock) for clock in self.clocks],
            "resets": [dict(reset) for reset in self.resets],
            "manifest_fingerprint_sha256": self.manifest_fingerprint_sha256,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SECDesignSide":
        raw_sources = value.get("sources")
        if not isinstance(raw_sources, list):
            raise SECBundleError("SEC side sources must be an array")

        raw_include_dirs = value.get("include_dirs", [])
        raw_defines = value.get("defines", [])
        raw_parameters = value.get("parameters", {})
        raw_clocks = value.get("clocks", [])
        raw_resets = value.get("resets", [])

        if not isinstance(raw_include_dirs, list):
            raise SECBundleError("SEC side include_dirs must be an array")
        if not isinstance(raw_defines, list):
            raise SECBundleError("SEC side defines must be an array")
        if not isinstance(raw_parameters, dict):
            raise SECBundleError("SEC side parameters must be an object")
        if not isinstance(raw_clocks, list):
            raise SECBundleError("SEC side clocks must be an array")
        if not isinstance(raw_resets, list):
            raise SECBundleError("SEC side resets must be an array")

        side = cls(
            role=str(value.get("role", "")),
            dataset=str(value.get("dataset", "")),
            design_id=str(value.get("design_id", "")),
            top=str(value.get("top", "")),
            language=str(value.get("language", "")),
            base_dir=str(value.get("base_dir", "")),
            sources=tuple(SECSourceFile.from_dict(item) for item in raw_sources),
            include_dirs=tuple(str(item) for item in raw_include_dirs),
            defines=tuple(str(item) for item in raw_defines),
            parameters=dict(raw_parameters),
            clocks=tuple(dict(item) for item in raw_clocks if isinstance(item, dict)),
            resets=tuple(dict(item) for item in raw_resets if isinstance(item, dict)),
            manifest_fingerprint_sha256=str(
                value.get("manifest_fingerprint_sha256", "")
            ),
        )
        side.validate()
        return side


# =============================================================================
# Canonical SEC bundle
# =============================================================================


@dataclass(frozen=True)
class SECBundle:
    schema: str
    backend: str
    mode: str
    execution_ready: bool
    unsupported_reason: str | None
    verification_options: Dict[str, Any]
    golden: SECDesignSide
    candidate: SECDesignSide

    def validate(self, *, verify_files: bool = False) -> None:
        if self.schema != SEC_BUNDLE_SCHEMA:
            raise SECBundleError(
                f"SEC bundle schema must be {SEC_BUNDLE_SCHEMA!r}; "
                f"got {self.schema!r}"
            )

        if not isinstance(self.backend, str) or not self.backend:
            raise SECBundleError("SEC backend must be a non-empty string")

        if self.mode not in SUPPORTED_MODES:
            raise SECBundleError(
                f"unsupported SEC mode {self.mode!r}; expected one of "
                f"{sorted(SUPPORTED_MODES)}"
            )

        if not isinstance(self.verification_options, dict):
            raise SECBundleError("verification_options must be an object")

        self.golden.validate()
        self.candidate.validate()

        # ------------------------------------------------------------------
        # The two sides must describe the same logical design boundary.
        # ------------------------------------------------------------------

        identity_fields = (
            "dataset",
            "design_id",
            "top",
            "language",
        )

        for field in identity_fields:
            golden_value = getattr(self.golden, field)
            candidate_value = getattr(self.candidate, field)
            if golden_value != candidate_value:
                raise SECBundleError(
                    f"golden/candidate {field} mismatch: "
                    f"{golden_value!r} != {candidate_value!r}"
                )

        # Compile-time semantic controls must be identical. Candidate include
        # directories are allowed to differ because lint_candidate.py stages
        # sources into an isolated tree and adds staging search directories.
        if self.golden.defines != self.candidate.defines:
            raise SECBundleError("golden/candidate defines differ")

        if self.golden.parameters != self.candidate.parameters:
            raise SECBundleError("golden/candidate top parameters differ")

        if self.golden.clocks != self.candidate.clocks:
            raise SECBundleError("golden/candidate clock metadata differs")

        if self.golden.resets != self.candidate.resets:
            raise SECBundleError("golden/candidate reset metadata differs")

        if self.mode == "backend_unsupported":
            if self.execution_ready:
                raise SECBundleError(
                    "backend_unsupported SEC bundle cannot be execution_ready"
                )
            if not isinstance(self.unsupported_reason, str) or not self.unsupported_reason:
                raise SECBundleError(
                    "backend_unsupported mode requires unsupported_reason"
                )
        else:
            if not self.execution_ready:
                raise SECBundleError(
                    f"SEC mode {self.mode!r} must be execution_ready"
                )
            if self.unsupported_reason is not None:
                raise SECBundleError(
                    "supported SEC modes must not carry unsupported_reason"
                )

        if verify_files:
            self.golden.verify_files()
            self.candidate.verify_files()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": self.schema,
            "verification": {
                "backend": self.backend,
                "mode": self.mode,
                "execution_ready": self.execution_ready,
                "unsupported_reason": self.unsupported_reason,
                "options": dict(self.verification_options),
            },
            "golden": self.golden.to_dict(),
            "candidate": self.candidate.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SECBundle":
        verification = value.get("verification")
        if not isinstance(verification, dict):
            raise SECBundleError("SEC bundle verification must be an object")

        golden = value.get("golden")
        candidate = value.get("candidate")
        if not isinstance(golden, dict):
            raise SECBundleError("SEC bundle golden must be an object")
        if not isinstance(candidate, dict):
            raise SECBundleError("SEC bundle candidate must be an object")

        options = verification.get("options", {})
        if not isinstance(options, dict):
            raise SECBundleError("verification.options must be an object")

        bundle = cls(
            schema=str(value.get("schema", "")),
            backend=str(verification.get("backend", "")),
            mode=str(verification.get("mode", "")),
            execution_ready=bool(verification.get("execution_ready", False)),
            unsupported_reason=(
                str(verification["unsupported_reason"])
                if verification.get("unsupported_reason") is not None
                else None
            ),
            verification_options=dict(options),
            golden=SECDesignSide.from_dict(golden),
            candidate=SECDesignSide.from_dict(candidate),
        )
        bundle.validate()
        return bundle

    @classmethod
    def read_json(cls, path: Path, *, verify_files: bool = False) -> "SECBundle":
        bundle = cls.from_dict(load_json(path))
        bundle.validate(verify_files=verify_files)
        return bundle

    def write_json(self, path: Path) -> None:
        self.validate()
        write_json(path, self.to_dict())


# =============================================================================
# Manifest -> SEC side conversion
# =============================================================================


def manifest_source_side(
    manifest: DesignManifest,
    base_dir: Path,
    *,
    role: str,
) -> SECDesignSide:
    """Freeze one DesignManifest into a backend-independent SEC compile side."""

    manifest.validate(require_source_hashes=True)
    base_dir = Path(base_dir).resolve()

    sources: List[SECSourceFile] = []

    for source_name in manifest.source_files:
        source_path = manifest.resolve_path(source_name, base_dir).resolve()

        if not source_path.is_file():
            raise SECBundleError(
                f"{role}: source file missing: {source_path}"
            )

        expected_hash = manifest.source_hashes.get(source_name)
        if not is_sha256(expected_hash):
            raise SECBundleError(
                f"{role}: manifest has no valid SHA-256 for {source_name!r}"
            )

        actual_hash = sha256_file(source_path)
        if actual_hash.lower() != str(expected_hash).lower():
            raise SECBundleError(
                f"{role}: source hash mismatch for {source_name!r}: "
                f"manifest={expected_hash}, disk={actual_hash}"
            )

        sources.append(
            SECSourceFile(
                manifest_path=source_name,
                resolved_path=str(source_path),
                sha256=actual_hash,
            )
        )

    include_dirs = tuple(
        str(path.resolve())
        for path in manifest.resolved_include_dirs(base_dir)
    )

    side = SECDesignSide(
        role=role,
        dataset=manifest.dataset,
        design_id=manifest.design_id,
        top=manifest.top,
        language=manifest.language,
        base_dir=str(base_dir),
        sources=tuple(sources),
        include_dirs=include_dirs,
        defines=tuple(manifest.defines),
        parameters=dict(manifest.parameters),
        clocks=tuple(clock_dicts(manifest)),
        resets=tuple(reset_dicts(manifest)),
        manifest_fingerprint_sha256=canonical_json_sha256(manifest.to_dict()),
    )
    side.validate()
    return side


# =============================================================================
# Bundle construction
# =============================================================================


def build_sec_bundle(
    *,
    golden_manifest: DesignManifest,
    golden_base_dir: Path,
    candidate_manifest: DesignManifest,
    candidate_base_dir: Path,
    mode: str | None = None,
    backend: str | None = None,
    unsupported_reason: str | None = None,
) -> SECBundle:
    """
    Construct the canonical verification bundle.

    The golden manifest is authoritative for verification policy unless an
    explicit CLI/API override is supplied.
    """

    golden_manifest.validate(require_source_hashes=True)
    candidate_manifest.validate(require_source_hashes=True)

    chosen_mode = mode or golden_manifest.verification.mode
    chosen_backend = backend or golden_manifest.verification.backend

    if chosen_mode not in SUPPORTED_MODES:
        raise SECBundleError(
            f"unsupported SEC mode {chosen_mode!r}; expected one of "
            f"{sorted(SUPPORTED_MODES)}"
        )

    if not isinstance(chosen_backend, str) or not chosen_backend:
        raise SECBundleError("verification backend must be non-empty")

    # Candidate manifests emitted by lint_candidate.py are clones of the golden
    # logical manifest. A policy mismatch here therefore indicates corruption or
    # an unsafe manual modification and must fail closed.
    if candidate_manifest.verification.backend != golden_manifest.verification.backend:
        raise SECBundleError(
            "candidate verification backend differs from golden manifest"
        )

    if candidate_manifest.verification.mode != golden_manifest.verification.mode:
        raise SECBundleError(
            "candidate verification mode differs from golden manifest"
        )

    golden_side = manifest_source_side(
        golden_manifest,
        golden_base_dir,
        role="golden",
    )

    candidate_side = manifest_source_side(
        candidate_manifest,
        candidate_base_dir,
        role="candidate",
    )

    options = dict(golden_manifest.verification.options)

    reason: str | None = None
    execution_ready = chosen_mode != "backend_unsupported"

    if chosen_mode == "backend_unsupported":
        reason = unsupported_reason
        if reason is None:
            raw_reason = options.get("reason")
            if isinstance(raw_reason, str) and raw_reason:
                reason = raw_reason
        if reason is None:
            reason = "verification backend marked unsupported for this design"

    bundle = SECBundle(
        schema=SEC_BUNDLE_SCHEMA,
        backend=chosen_backend,
        mode=chosen_mode,
        execution_ready=execution_ready,
        unsupported_reason=reason,
        verification_options=options,
        golden=golden_side,
        candidate=candidate_side,
    )

    bundle.validate(verify_files=True)
    return bundle


# =============================================================================
# Self-test
# =============================================================================


def _write_design(root: Path, *, candidate: bool) -> None:
    rtl = root / "rtl"
    rtl.mkdir(parents=True, exist_ok=True)

    (rtl / "top.v").write_text(
        "module top (\n"
        "    input  wire clk,\n"
        "    input  wire [7:0] a,\n"
        "    output wire [7:0] y\n"
        ");\n"
        "wire [7:0] t;\n"
        + (
            "assign t = (a ^ 8'h00);\n"
            if candidate
            else
            "assign t = a;\n"
        )
        + "child u_child (.a(t), .y(y));\n"
        "endmodule\n",
        encoding="utf-8",
    )

    (rtl / "child.v").write_text(
        "module child (\n"
        "    input  wire [7:0] a,\n"
        "    output wire [7:0] y\n"
        ");\n"
        "assign y = a;\n"
        "endmodule\n",
        encoding="utf-8",
    )


def _make_manifest(
    root: Path,
    *,
    mode: str = "sequential_equivalence",
    parameters: Mapping[str, Any] | None = None,
) -> DesignManifest:
    manifest = DesignManifest(
        dataset="sec_bundle_self_test",
        design_id="sec_test",
        top="top",
        source_files=["rtl/top.v", "rtl/child.v"],
        include_dirs=[],
        defines=["NEBULA_SEC_TEST=1"],
        parameters=dict(parameters or {}),
        language="verilog",
        clocks=[],
        resets=[],
        constraints={},
        verification=VerificationPolicy(
            backend="yosys",
            mode=mode,
            options={"timeout_s": 30},
        ),
    )
    manifest.refresh_source_hashes(root)
    return manifest


def run_self_test() -> None:
    with tempfile.TemporaryDirectory(prefix="nebula_sec_bundle_") as tmp:
        root = Path(tmp).resolve()
        golden_root = root / "golden"
        candidate_root = root / "candidate"

        _write_design(golden_root, candidate=False)
        _write_design(candidate_root, candidate=True)

        golden_manifest = _make_manifest(golden_root)
        candidate_manifest = _make_manifest(candidate_root)

        # ------------------------------------------------------------------
        # Standard sequential bundle.
        # ------------------------------------------------------------------

        bundle = build_sec_bundle(
            golden_manifest=golden_manifest,
            golden_base_dir=golden_root,
            candidate_manifest=candidate_manifest,
            candidate_base_dir=candidate_root,
        )

        assert bundle.mode == "sequential_equivalence"
        assert bundle.backend == "yosys"
        assert bundle.execution_ready is True
        assert len(bundle.golden.sources) == 2
        assert len(bundle.candidate.sources) == 2
        assert bundle.golden.defines == ("NEBULA_SEC_TEST=1",)
        assert bundle.candidate.defines == bundle.golden.defines

        # ------------------------------------------------------------------
        # JSON round-trip.
        # ------------------------------------------------------------------

        json_path = root / "bundle.json"
        bundle.write_json(json_path)
        loaded = SECBundle.read_json(json_path, verify_files=True)
        assert loaded.to_dict() == bundle.to_dict()

        # ------------------------------------------------------------------
        # Explicit combinational CEC mode override.
        # ------------------------------------------------------------------

        cec = build_sec_bundle(
            golden_manifest=golden_manifest,
            golden_base_dir=golden_root,
            candidate_manifest=candidate_manifest,
            candidate_base_dir=candidate_root,
            mode="combinational_cec",
        )
        assert cec.mode == "combinational_cec"
        assert cec.execution_ready is True

        # ------------------------------------------------------------------
        # backend_unsupported is representable without pretending proof ran.
        # ------------------------------------------------------------------

        unsupported = build_sec_bundle(
            golden_manifest=golden_manifest,
            golden_base_dir=golden_root,
            candidate_manifest=candidate_manifest,
            candidate_base_dir=candidate_root,
            mode="backend_unsupported",
            unsupported_reason="memory semantics not supported by active backend",
        )
        assert unsupported.execution_ready is False
        assert unsupported.unsupported_reason is not None

        # ------------------------------------------------------------------
        # Compile-time parameter mismatch must fail closed.
        # ------------------------------------------------------------------

        bad_parameter_manifest = _make_manifest(
            candidate_root,
            parameters={"WIDTH": 4},
        )

        parameter_rejected = False
        try:
            build_sec_bundle(
                golden_manifest=golden_manifest,
                golden_base_dir=golden_root,
                candidate_manifest=bad_parameter_manifest,
                candidate_base_dir=candidate_root,
            )
        except SECBundleError:
            parameter_rejected = True
        assert parameter_rejected

        # ------------------------------------------------------------------
        # Stale candidate source hash must fail closed.
        # ------------------------------------------------------------------

        stale_manifest = _make_manifest(candidate_root)
        (candidate_root / "rtl" / "top.v").write_text(
            (candidate_root / "rtl" / "top.v").read_text(encoding="utf-8")
            + "// mutated after manifest hash\n",
            encoding="utf-8",
        )

        stale_rejected = False
        try:
            build_sec_bundle(
                golden_manifest=golden_manifest,
                golden_base_dir=golden_root,
                candidate_manifest=stale_manifest,
                candidate_base_dir=candidate_root,
            )
        except SECBundleError:
            stale_rejected = True
        assert stale_rejected

        print("PASS: sec_bundle self-test")
        print("PASS: complete golden/candidate filelists frozen")
        print("PASS: independent source SHA-256 values verified")
        print("PASS: include/define/parameter compile context captured")
        print("PASS: sequential_equivalence mode represented")
        print("PASS: combinational_cec mode represented")
        print("PASS: backend_unsupported mode represented explicitly")
        print("PASS: JSON round-trip preserved bundle")
        print("PASS: compile-time parameter mismatch rejected")
        print("PASS: stale source mutation rejected")


# =============================================================================
# CLI
# =============================================================================


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build a canonical golden/candidate SEC bundle from two "
            "DesignManifest files."
        )
    )

    parser.add_argument("--golden-manifest")
    parser.add_argument("--golden-base-dir", default=".")
    parser.add_argument("--candidate-manifest")
    parser.add_argument("--candidate-base-dir", default=".")
    parser.add_argument(
        "--mode",
        choices=sorted(SUPPORTED_MODES),
        default=None,
        help="Optional verification-mode override.",
    )
    parser.add_argument(
        "--backend",
        default=None,
        help="Optional verification-backend override.",
    )
    parser.add_argument(
        "--unsupported-reason",
        default=None,
        help="Reason when --mode backend_unsupported is selected.",
    )
    parser.add_argument("--output")
    parser.add_argument("--self-test", action="store_true")

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

    missing = []
    if not args.golden_manifest:
        missing.append("--golden-manifest")
    if not args.candidate_manifest:
        missing.append("--candidate-manifest")
    if not args.output:
        missing.append("--output")

    if missing:
        print(
            "ERROR: missing required argument(s): " + ", ".join(missing),
            file=sys.stderr,
        )
        return 2

    try:
        golden_manifest = DesignManifest.read_json(Path(args.golden_manifest))
        candidate_manifest = DesignManifest.read_json(Path(args.candidate_manifest))

        bundle = build_sec_bundle(
            golden_manifest=golden_manifest,
            golden_base_dir=Path(args.golden_base_dir),
            candidate_manifest=candidate_manifest,
            candidate_base_dir=Path(args.candidate_base_dir),
            mode=args.mode,
            backend=args.backend,
            unsupported_reason=args.unsupported_reason,
        )

        bundle.write_json(Path(args.output))

    except (SECBundleError, ValueError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(
            f"ERROR: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 2

    summary = {
        "ok": True,
        "output": str(Path(args.output)),
        "backend": bundle.backend,
        "mode": bundle.mode,
        "execution_ready": bundle.execution_ready,
        "top": bundle.golden.top,
        "golden_sources": len(bundle.golden.sources),
        "candidate_sources": len(bundle.candidate.sources),
    }

    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
