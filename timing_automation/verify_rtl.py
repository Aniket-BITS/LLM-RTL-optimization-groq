#!/usr/bin/env python3
"""
verify_rtl.py

Canonical formal-equivalence execution gate for nEBULA.

This is the proof ENGINE that consumes sec_bundle.py's backend-independent
SECBundle.  It no longer accepts ad-hoc single golden/candidate RTL files.

Canonical flow
--------------

    accepted candidate from lint_candidate.py
                |
                v
          candidate DesignManifest
                |
    golden DesignManifest
                |
                v
            SECBundle
                |
                v
          verify_rtl.py
                |
                +---- backend_unsupported -> explicit unsupported result
                |
                +---- yosys + combinational_cec
                |         -> equiv_make
                |         -> equiv_simple
                |         -> equiv_status -assert
                |
                +---- yosys + sequential_equivalence
                          -> equiv_make
                          -> equiv_simple -seq N
                          -> equiv_induct -seq N
                          -> equiv_status -assert

The SECBundle is authoritative for:

    - complete golden/candidate source filelists
    - source SHA-256 values
    - include directories
    - preprocessor defines
    - top module
    - language
    - top-level parameter overrides
    - verification backend
    - verification mode

Exit codes
----------

    0  equivalence PROVEN (complete or sequential-induction proof)
    1  NOT_PROVEN (proof did not close / timeout; not a disproof)
    2  NOT_ATTEMPTED or TOOL_ERROR
    3  DISPROVEN (concrete combinational counterexample found)

Normal usage
------------

python3 timing_automation/verify_rtl.py \
    --bundle ai_runs/cand_0001.sec_bundle.json \
    --output ai_runs/cand_0001.verify.json

Optional overrides
------------------

    --yosys PATH
    --seq-depth N
    --timeout-s SECONDS

Self-test
---------

python3 timing_automation/verify_rtl.py --self-test
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence, Tuple


# =============================================================================
# Local imports
# =============================================================================

try:
    from design_manifest import DesignManifest, VerificationPolicy
    from sec_bundle import (
        SECBundle,
        SECBundleError,
        SECDesignSide,
        build_sec_bundle,
        canonical_json_sha256,
    )
    from yosys_structure import (
        YosysStructureError,
        parameter_value_text,
        quote_yosys_token,
        resolve_yosys,
        yosys_identifier,
    )
    from equivalence_terms import (
        EquivalenceVerdict,
        describe as describe_equivalence,
        exit_code_for,
    )
except ImportError:
    from timing_automation.design_manifest import DesignManifest, VerificationPolicy
    from timing_automation.sec_bundle import (
        SECBundle,
        SECBundleError,
        SECDesignSide,
        build_sec_bundle,
        canonical_json_sha256,
    )
    from timing_automation.yosys_structure import (
        YosysStructureError,
        parameter_value_text,
        quote_yosys_token,
        resolve_yosys,
        yosys_identifier,
    )
    from timing_automation.equivalence_terms import (
        EquivalenceVerdict,
        describe as describe_equivalence,
        exit_code_for,
    )


# =============================================================================
# Constants
# =============================================================================

VERIFY_SCHEMA = "rtl_verify/2"

SUPPORTED_YOSYS_MODES = {
    "combinational_cec",
    "sequential_equivalence",
}

DEFAULT_SEQ_DEPTH = 5
DEFAULT_TIMEOUT_S = 120.0
MAX_SEQ_DEPTH = 10000


# =============================================================================
# Exceptions
# =============================================================================


class VerifyRTLError(RuntimeError):
    """Base verify_rtl error."""


class VerificationSetupError(VerifyRTLError):
    """Invalid bundle/options or unsupported compile context."""


class VerificationBackendError(VerifyRTLError):
    """Verification backend could not execute correctly."""


# =============================================================================
# Generic helpers
# =============================================================================


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )


def log_tail(text: str, lines: int = 40) -> str:
    raw = text.rstrip().splitlines()
    return "\n".join(raw[-lines:])


def _positive_int(value: Any, *, name: str, default: int) -> int:
    if value is None:
        return default

    if isinstance(value, bool):
        raise VerificationSetupError(f"{name} must be an integer")

    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise VerificationSetupError(f"{name} must be an integer") from exc

    if parsed < 1:
        raise VerificationSetupError(f"{name} must be >= 1")

    if name == "seq_depth" and parsed > MAX_SEQ_DEPTH:
        raise VerificationSetupError(
            f"seq_depth must be <= {MAX_SEQ_DEPTH}; got {parsed}"
        )

    return parsed


def _positive_float(value: Any, *, name: str, default: float) -> float:
    if value is None:
        return default

    if isinstance(value, bool):
        raise VerificationSetupError(f"{name} must be numeric")

    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise VerificationSetupError(f"{name} must be numeric") from exc

    if parsed <= 0.0:
        raise VerificationSetupError(f"{name} must be > 0")

    return parsed


def resolve_seq_depth(
    bundle: SECBundle,
    cli_override: int | None,
) -> int:
    if cli_override is not None:
        return _positive_int(cli_override, name="seq_depth", default=DEFAULT_SEQ_DEPTH)

    raw = bundle.verification_options.get("seq_depth")
    if raw is None:
        # Compatibility with the old verify_rtl.py option spelling.
        raw = bundle.verification_options.get("seq")

    return _positive_int(raw, name="seq_depth", default=DEFAULT_SEQ_DEPTH)


def resolve_timeout_s(
    bundle: SECBundle,
    cli_override: float | None,
) -> float:
    if cli_override is not None:
        return _positive_float(cli_override, name="timeout_s", default=DEFAULT_TIMEOUT_S)

    raw = bundle.verification_options.get("timeout_s")
    return _positive_float(raw, name="timeout_s", default=DEFAULT_TIMEOUT_S)


def resolve_yosys_for_verify(explicit: str | None = None) -> Path:
    """
    Resolve Yosys while preserving the old YOSYS_EXE environment override.

    Order:
        1. --yosys
        2. YOSYS_EXE
        3. yosys_structure.resolve_yosys() -> PATH / ORFS install
    """

    chosen = explicit
    if chosen is None:
        env_yosys = os.environ.get("YOSYS_EXE")
        if env_yosys:
            chosen = env_yosys

    try:
        return resolve_yosys(chosen)
    except YosysStructureError as exc:
        raise VerificationBackendError(str(exc)) from exc


# =============================================================================
# SEC compile-context validation
# =============================================================================


def validate_yosys_side(side: SECDesignSide) -> None:
    """Validate one frozen SEC side for the built-in Yosys backend."""

    side.validate()

    if side.language not in {"verilog", "systemverilog"}:
        raise VerificationSetupError(
            "built-in Yosys verification backend supports only "
            f"Verilog/SystemVerilog; {side.role} language is {side.language!r}"
        )

    # Top is passed to Yosys commands as an HDL identifier, never a filename.
    try:
        yosys_identifier(side.top)
    except YosysStructureError as exc:
        raise VerificationSetupError(
            f"{side.role}: unsupported top module name {side.top!r}: {exc}"
        ) from exc

    for include_dir in side.include_dirs:
        path = Path(include_dir)
        if not path.is_dir():
            raise VerificationSetupError(
                f"{side.role}: include directory missing: {path}"
            )

    # SECBundle.read_json(..., verify_files=True) already verifies every source
    # hash. This second shape check makes helper APIs safe when called directly.
    if not side.sources:
        raise VerificationSetupError(f"{side.role}: empty source filelist")

    for source in side.sources:
        if not Path(source.resolved_path).is_file():
            raise VerificationSetupError(
                f"{side.role}: source file missing: {source.resolved_path}"
            )


def validate_bundle_for_execution(bundle: SECBundle) -> None:
    """Fail closed before any proof backend runs."""

    bundle.validate(verify_files=True)

    if bundle.mode == "backend_unsupported":
        # This mode is valid, but deliberately non-executable.
        return

    if not bundle.execution_ready:
        raise VerificationSetupError(
            f"verification mode {bundle.mode!r} is not execution-ready"
        )

    if bundle.backend.lower() == "yosys":
        if bundle.mode not in SUPPORTED_YOSYS_MODES:
            raise VerificationSetupError(
                f"Yosys backend does not implement mode {bundle.mode!r}"
            )
        validate_yosys_side(bundle.golden)
        validate_yosys_side(bundle.candidate)


# =============================================================================
# Yosys command generation
# =============================================================================


def read_verilog_command(side: SECDesignSide) -> str:
    """
    Build one complete Yosys read_verilog command from an SECDesignSide.

    This intentionally mirrors the already-tested token handling in
    yosys_structure.py:

        read_verilog [-sv] -I "dir" -DNAME=VALUE "src1" "src2" ...
    """

    validate_yosys_side(side)

    args: List[str] = ["read_verilog"]

    if side.language == "systemverilog":
        args.append("-sv")

    for include_dir in side.include_dirs:
        args.append("-I")
        args.append(quote_yosys_token(include_dir))

    for define in side.defines:
        define = str(define).strip()
        if not define:
            raise VerificationSetupError(
                f"{side.role}: empty Verilog define"
            )
        if any(ch.isspace() for ch in define):
            raise VerificationSetupError(
                f"{side.role}: Verilog define must not contain whitespace: "
                f"{define!r}"
            )

        # Yosys read_verilog requires -Dname[=definition] as ONE option.
        # Do not emit '-D "NAME=VALUE"': that does not activate the macro
        # reliably and can silently compile the wrong conditional branch.
        args.append("-D" + define)

    for source in side.sources:
        args.append(quote_yosys_token(source.resolved_path))

    return " ".join(args)


def side_preparation_lines(side: SECDesignSide) -> List[str]:
    """
    Compile and flatten one side before stashing it.

    Flattening is intentional. The old verifier copied only the top module out
    of each Yosys design stash, which is unsafe for multi-module RTL because the
    copied top can still reference child module definitions that were not
    copied. Flattening each COMPLETE source bundle before stashing removes that
    single-file/hierarchy assumption.
    """

    top = yosys_identifier(side.top)

    lines: List[str] = [
        # read_verilog keeps preprocessor state across calls; isolate each side.
        "design -reset-vlog",
        read_verilog_command(side),
    ]

    for name in sorted(side.parameters):
        value = parameter_value_text(side.parameters[name])
        lines.append(
            "chparam "
            f"-set {yosys_identifier(name)} "
            f"{value} "
            f"{top}"
        )

    lines.extend(
        [
            f"hierarchy -check -top {top}",
            f"prep -flatten -top {top}",
            "splitnets -ports",
            "check",
        ]
    )

    return lines


def build_yosys_proof_script(
    bundle: SECBundle,
    *,
    seq_depth: int,
) -> str:
    """Build the deterministic Yosys formal-equivalence script."""

    validate_bundle_for_execution(bundle)

    if bundle.backend.lower() != "yosys":
        raise VerificationSetupError(
            f"cannot build Yosys script for backend {bundle.backend!r}"
        )

    if bundle.mode not in SUPPORTED_YOSYS_MODES:
        raise VerificationSetupError(
            f"cannot build Yosys script for mode {bundle.mode!r}"
        )

    seq_depth = _positive_int(
        seq_depth,
        name="seq_depth",
        default=DEFAULT_SEQ_DEPTH,
    )

    top = yosys_identifier(bundle.golden.top)

    lines: List[str] = [
        "# ============================================================",
        "# nEBULA RTL equivalence proof",
        f"# mode: {bundle.mode}",
        "# ============================================================",
        "",
        "# ------------------------------------------------------------",
        "# GOLDEN — complete frozen filelist",
        "# ------------------------------------------------------------",
    ]

    lines.extend(side_preparation_lines(bundle.golden))
    lines.extend(
        [
            "design -stash gold",
            "",
            "# ------------------------------------------------------------",
            "# CANDIDATE — complete frozen filelist",
            "# ------------------------------------------------------------",
        ]
    )

    lines.extend(side_preparation_lines(bundle.candidate))
    lines.extend(
        [
            "design -stash gate",
            "",
            "# ------------------------------------------------------------",
            "# IMPORT FROZEN TOPS",
            "# ------------------------------------------------------------",
            f"design -copy-from gold -as gold {top}",
            f"design -copy-from gate -as gate {top}",
            "",
        ]
    )

    if bundle.mode == "combinational_cec":
        # For pure combinational CEC use an explicit miter and SAT proof.
        # This is the standard exact Yosys formulation: any output mismatch
        # creates an assertion failure and SAT must prove no counterexample.
        lines.extend(
            [
                "# ------------------------------------------------------------",
                "# COMBINATIONAL CEC: MITER + SAT",
                "# ------------------------------------------------------------",
                "miter -equiv -make_assert -make_outputs -flatten gold gate miter",
                "prep -top miter",
                "sat -verify -prove-asserts -show-inputs -show-outputs miter",
                "",
            ]
        )

    elif bundle.mode == "sequential_equivalence":
        # Sequential mode uses Yosys' state-aware equivalence cells and temporal
        # induction. Avoid -inames: matching private/internal names is not a
        # required proof assumption and can make the partitioning less robust.
        lines.extend(
            [
                "# ------------------------------------------------------------",
                "# SEQUENTIAL EQUIVALENCE",
                "# ------------------------------------------------------------",
                "equiv_make gold gate equiv",
                "prep -flatten -top equiv",
                "opt_clean -purge",
                "opt -full",
                f"equiv_simple -seq {seq_depth}",
                f"equiv_induct -seq {seq_depth}",
                "",
                "# Fail closed if even one $equiv cell remains unproven.",
                "equiv_status -assert",
                "",
            ]
        )

    return "\n".join(lines)


# =============================================================================
# Backend result model
# =============================================================================


@dataclass(frozen=True)
class BackendRun:
    ok: bool
    status: str
    reason: str
    exit_code: int
    returncode: int | None
    script_path: str | None
    log_path: str | None
    log_tail: str
    duration_s: float
    method: str
    induction_depth: int | None
    unproven_cell_count: int
    timed_out: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "reason": self.reason,
            "exit_code": self.exit_code,
            "returncode": self.returncode,
            "script": self.script_path,
            "log": self.log_path,
            "log_tail": self.log_tail,
            "duration_s": round(self.duration_s, 6),
            "method": self.method,
            "induction_depth": self.induction_depth,
            "unproven_cell_count": self.unproven_cell_count,
            "timed_out": self.timed_out,
        }


_UNPROVEN_TOTAL_RE = re.compile(
    r"Found(?:\s+a\s+total\s+of)?\s+(\d+)\s+unproven\s+\$equiv",
    flags=re.IGNORECASE,
)
_FORMAL_INCOMPLETE_RE = re.compile(
    r"(?:unproven\s+\$equiv|\$equiv\s+cells?.*unproven|equiv_status.*assert)",
    flags=re.IGNORECASE | re.DOTALL,
)
_COMBINATIONAL_CEX_RE = re.compile(
    r"(?:proof\s+did\s+fail|model\s+found|counterexample)",
    flags=re.IGNORECASE,
)


def count_unproven_cells(log: str) -> int:
    matches = [int(m.group(1)) for m in _UNPROVEN_TOTAL_RE.finditer(log)]
    return matches[-1] if matches else 0


def classify_yosys_nonzero(log: str, *, mode: str) -> Tuple[str, str, int]:
    """Classify a non-zero Yosys proof result conservatively.

    The combinational path is an explicit miter followed by ``sat -verify``.
    When that SAT proof reports a failed proof/model/counterexample, a concrete
    input assignment exists that violates equivalence, so the candidate is
    DISPROVEN.  The sequential path uses ``equiv_simple/equiv_induct``; residual
    unproven $equiv cells mean only that the proof did not close and therefore
    remain NOT_PROVEN rather than being called inequivalent.
    """
    if mode == "combinational_cec" and _COMBINATIONAL_CEX_RE.search(log):
        verdict = EquivalenceVerdict.DISPROVEN
        return (
            verdict.value,
            describe_equivalence(verdict),
            exit_code_for(verdict),
        )

    if _FORMAL_INCOMPLETE_RE.search(log):
        verdict = EquivalenceVerdict.NOT_PROVEN
        return (
            verdict.value,
            describe_equivalence(verdict),
            exit_code_for(verdict),
        )

    verdict = EquivalenceVerdict.TOOL_ERROR
    return (
        verdict.value,
        "Yosys exited with an error before a valid equivalence verdict was established.",
        exit_code_for(verdict),
    )


# =============================================================================
# Yosys backend
# =============================================================================


def run_yosys_backend(
    bundle: SECBundle,
    *,
    output_path: Path,
    yosys_exe: str | None,
    seq_depth: int,
    timeout_s: float,
) -> BackendRun:
    """Execute the built-in Yosys equivalence backend."""

    yosys = resolve_yosys_for_verify(yosys_exe)

    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    script_path = output_path.with_suffix(".ys")
    log_path = output_path.with_suffix(".log")

    script = build_yosys_proof_script(
        bundle,
        seq_depth=seq_depth,
    )
    script_path.write_text(script, encoding="utf-8")

    # There is no single source tree for an SECBundle: golden and candidate may
    # live in completely different directories. All source/include paths in the
    # bundle are therefore frozen as absolute paths and the Yosys cwd is only an
    # artifact working directory.
    work_dir = output_path.parent

    start = time.monotonic()

    try:
        process = subprocess.run(
            [str(yosys), "-s", str(script_path)],
            cwd=str(work_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        duration = time.monotonic() - start

        stdout = exc.stdout or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")

        stderr = exc.stderr or ""
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")

        log = str(stdout) + str(stderr)
        if log:
            log += "\n"
        log += f"VERIFY_RTL_TIMEOUT after {timeout_s} seconds\n"
        log_path.write_text(log, encoding="utf-8")

        verdict = EquivalenceVerdict.NOT_PROVEN
        return BackendRun(
            ok=False,
            status=verdict.value,
            reason=(
                f"{describe_equivalence(verdict)}; timeout after {timeout_s:g} seconds"
            ),
            exit_code=exit_code_for(verdict),
            returncode=None,
            script_path=str(script_path),
            log_path=str(log_path),
            log_tail=log_tail(log),
            duration_s=duration,
            method=("combinational_sat_miter" if bundle.mode == "combinational_cec" else "sequential_equivalence_induction"),
            induction_depth=(seq_depth if bundle.mode == "sequential_equivalence" else None),
            unproven_cell_count=count_unproven_cells(log),
            timed_out=True,
        )
    except OSError as exc:
        duration = time.monotonic() - start
        verdict = EquivalenceVerdict.TOOL_ERROR
        return BackendRun(
            ok=False,
            status=verdict.value,
            reason=f"failed to execute Yosys: {exc}",
            exit_code=exit_code_for(verdict),
            returncode=None,
            script_path=str(script_path),
            log_path=None,
            log_tail="",
            duration_s=duration,
            method=("combinational_sat_miter" if bundle.mode == "combinational_cec" else "sequential_equivalence_induction"),
            induction_depth=(seq_depth if bundle.mode == "sequential_equivalence" else None),
            unproven_cell_count=0,
            timed_out=False,
        )

    duration = time.monotonic() - start
    log = process.stdout or ""
    log_path.write_text(log, encoding="utf-8")

    if process.returncode == 0:
        verdict = (
            EquivalenceVerdict.PROVEN_COMPLETE
            if bundle.mode == "combinational_cec"
            else EquivalenceVerdict.PROVEN_BOUNDED
        )
        method = (
            "combinational_sat_miter"
            if bundle.mode == "combinational_cec"
            else "sequential_equivalence_induction"
        )
        depth = seq_depth if bundle.mode == "sequential_equivalence" else None
        return BackendRun(
            ok=True,
            status=verdict.value,
            reason=describe_equivalence(verdict, method=method, depth=depth),
            exit_code=exit_code_for(verdict),
            returncode=process.returncode,
            script_path=str(script_path),
            log_path=str(log_path),
            log_tail=log_tail(log),
            duration_s=duration,
            method=method,
            induction_depth=depth,
            unproven_cell_count=count_unproven_cells(log),
            timed_out=False,
        )

    status, reason, exit_code = classify_yosys_nonzero(log, mode=bundle.mode)
    method = (
        "combinational_sat_miter"
        if bundle.mode == "combinational_cec"
        else "sequential_equivalence_induction"
    )
    depth = seq_depth if bundle.mode == "sequential_equivalence" else None

    return BackendRun(
        ok=False,
        status=status,
        reason=reason,
        exit_code=exit_code,
        returncode=process.returncode,
        script_path=str(script_path),
        log_path=str(log_path),
        log_tail=log_tail(log),
        duration_s=duration,
        method=method,
        induction_depth=depth,
        unproven_cell_count=count_unproven_cells(log),
        timed_out=False,
    )


# =============================================================================
# Backend dispatch
# =============================================================================


def run_verification(
    bundle: SECBundle,
    *,
    output_path: Path,
    yosys_exe: str | None = None,
    seq_depth_override: int | None = None,
    timeout_override_s: float | None = None,
) -> Tuple[Dict[str, Any], int]:
    """
    Execute the backend selected by SECBundle.

    This dispatch layer is intentionally small so future SMT/Jasper/Formality
    adapters can be added without changing the SECBundle contract.
    """

    validate_bundle_for_execution(bundle)

    bundle_fingerprint = canonical_json_sha256(bundle.to_dict())

    base_result: Dict[str, Any] = {
        "tool": "verify_rtl.py",
        "schema": VERIFY_SCHEMA,
        "ok": False,
        "status": None,
        "reason": None,
        "bundle_fingerprint_sha256": bundle_fingerprint,
        "design": {
            "dataset": bundle.golden.dataset,
            "design_id": bundle.golden.design_id,
            "top": bundle.golden.top,
            "language": bundle.golden.language,
        },
        "verification": {
            "backend": bundle.backend,
            "mode": bundle.mode,
            "execution_ready": bundle.execution_ready,
            "options": dict(bundle.verification_options),
        },
        "golden": {
            "base_dir": bundle.golden.base_dir,
            "source_count": len(bundle.golden.sources),
            "sources": [source.to_dict() for source in bundle.golden.sources],
            "include_dirs": list(bundle.golden.include_dirs),
            "defines": list(bundle.golden.defines),
            "parameters": dict(bundle.golden.parameters),
            "manifest_fingerprint_sha256": (
                bundle.golden.manifest_fingerprint_sha256
            ),
        },
        "candidate": {
            "base_dir": bundle.candidate.base_dir,
            "source_count": len(bundle.candidate.sources),
            "sources": [source.to_dict() for source in bundle.candidate.sources],
            "include_dirs": list(bundle.candidate.include_dirs),
            "defines": list(bundle.candidate.defines),
            "parameters": dict(bundle.candidate.parameters),
            "manifest_fingerprint_sha256": (
                bundle.candidate.manifest_fingerprint_sha256
            ),
        },
        "proof": None,
    }

    # ------------------------------------------------------------------
    # Explicitly unsupported design/backend combination.
    # ------------------------------------------------------------------

    if bundle.mode == "backend_unsupported":
        reason = bundle.unsupported_reason or (
            "verification backend marked unsupported for this design"
        )
        verdict = EquivalenceVerdict.NOT_ATTEMPTED
        base_result["status"] = verdict.value
        base_result["reason"] = f"{describe_equivalence(verdict)}; {reason}"
        base_result["verification"]["unsupported_reason"] = reason
        base_result["verification"]["method"] = None
        base_result["verification"]["induction_depth"] = None
        return base_result, exit_code_for(verdict)

    backend = bundle.backend.lower()

    # ------------------------------------------------------------------
    # Built-in Yosys backend.
    # ------------------------------------------------------------------

    if backend == "yosys":
        seq_depth = resolve_seq_depth(bundle, seq_depth_override)
        timeout_s = resolve_timeout_s(bundle, timeout_override_s)

        base_result["verification"]["resolved_seq_depth"] = seq_depth
        base_result["verification"]["resolved_timeout_s"] = timeout_s

        run = run_yosys_backend(
            bundle,
            output_path=output_path,
            yosys_exe=yosys_exe,
            seq_depth=seq_depth,
            timeout_s=timeout_s,
        )

        base_result["ok"] = run.ok
        base_result["status"] = run.status
        base_result["reason"] = run.reason
        base_result["verification"]["method"] = run.method
        base_result["verification"]["induction_depth"] = run.induction_depth
        base_result["verification"]["unproven_cell_count"] = run.unproven_cell_count
        base_result["verification"]["timed_out"] = run.timed_out
        base_result["proof"] = run.to_dict()

        return base_result, run.exit_code

    # ------------------------------------------------------------------
    # Future backend adapter point.
    # ------------------------------------------------------------------

    verdict = EquivalenceVerdict.NOT_ATTEMPTED
    base_result["status"] = verdict.value
    base_result["reason"] = (
        f"{describe_equivalence(verdict)}; verification backend {bundle.backend!r} "
        "is not implemented by this verify_rtl.py build"
    )
    return base_result, exit_code_for(verdict)


# =============================================================================
# Self-test design generation
# =============================================================================


def _write_combinational_design(
    root: Path,
    *,
    variant: str,
) -> None:
    """Write a two-file combinational design."""

    rtl = root / "rtl"
    rtl.mkdir(parents=True, exist_ok=True)

    (rtl / "top.v").write_text(
        "module top (\n"
        "    input  wire [7:0] a,\n"
        "    input  wire [7:0] b,\n"
        "    output wire [7:0] y\n"
        ");\n"
        "helper u_helper (.a(a), .b(b), .y(y));\n"
        "endmodule\n",
        encoding="utf-8",
    )

    if variant == "golden":
        expression = "a & b"
    elif variant == "equivalent":
        expression = "b & a"
    elif variant == "different":
        expression = "a | b"
    else:
        raise ValueError(f"unknown combinational self-test variant {variant!r}")

    (rtl / "helper.v").write_text(
        "module helper (\n"
        "    input  wire [7:0] a,\n"
        "    input  wire [7:0] b,\n"
        "    output wire [7:0] y\n"
        ");\n"
        "`ifdef NEBULA_VERIFY_SELFTEST\n"
        f"assign y = {expression};\n"
        "`else\n"
        "assign y = 8'h00;\n"
        "`endif\n"
        "endmodule\n",
        encoding="utf-8",
    )


def _write_sequential_design(
    root: Path,
    *,
    variant: str,
) -> None:
    """Write a simple frozen-register-boundary sequential design."""

    rtl = root / "rtl"
    rtl.mkdir(parents=True, exist_ok=True)

    if variant == "golden":
        rhs = "d"
    elif variant == "equivalent":
        rhs = "d ^ 1'b0"
    elif variant == "different":
        rhs = "~d"
    else:
        raise ValueError(f"unknown sequential self-test variant {variant!r}")

    (rtl / "top.v").write_text(
        "module top (\n"
        "    input  wire clk,\n"
        "    input  wire d,\n"
        "    output wire y\n"
        ");\n"
        "reg q;\n"
        "always @(posedge clk) begin\n"
        f"    q <= {rhs};\n"
        "end\n"
        "assign y = q;\n"
        "endmodule\n",
        encoding="utf-8",
    )


def _make_selftest_manifest(
    root: Path,
    *,
    mode: str,
    sequential: bool,
    backend: str = "yosys",
) -> DesignManifest:
    source_files = ["rtl/top.v"]
    if not sequential:
        source_files.append("rtl/helper.v")

    manifest = DesignManifest(
        dataset="verify_rtl_self_test",
        design_id=("seq_test" if sequential else "comb_test"),
        top="top",
        source_files=source_files,
        include_dirs=["rtl"],
        defines=([] if sequential else ["NEBULA_VERIFY_SELFTEST=1"]),
        parameters={},
        language="verilog",
        clocks=[],
        resets=[],
        constraints={},
        verification=VerificationPolicy(
            backend=backend,
            mode=mode,
            options={
                "seq_depth": 5,
                "timeout_s": 30,
            },
        ),
    )
    manifest.refresh_source_hashes(root)
    return manifest


def _run_selftest_case(
    *,
    root: Path,
    name: str,
    mode: str,
    sequential: bool,
    candidate_variant: str,
    yosys: Path,
) -> Tuple[Dict[str, Any], int]:
    golden_root = root / f"{name}_golden"
    candidate_root = root / f"{name}_candidate"

    if sequential:
        _write_sequential_design(golden_root, variant="golden")
        _write_sequential_design(candidate_root, variant=candidate_variant)
    else:
        _write_combinational_design(golden_root, variant="golden")
        _write_combinational_design(candidate_root, variant=candidate_variant)

    golden_manifest = _make_selftest_manifest(
        golden_root,
        mode=mode,
        sequential=sequential,
    )
    candidate_manifest = _make_selftest_manifest(
        candidate_root,
        mode=mode,
        sequential=sequential,
    )

    bundle = build_sec_bundle(
        golden_manifest=golden_manifest,
        golden_base_dir=golden_root,
        candidate_manifest=candidate_manifest,
        candidate_base_dir=candidate_root,
    )

    output = root / f"{name}.verify.json"

    result, exit_code = run_verification(
        bundle,
        output_path=output,
        yosys_exe=str(yosys),
    )

    # run_verification returns the object; normal CLI writes it. Do the same in
    # self-test so script/log/report artifact behavior is exercised together.
    write_json(output, result)

    return result, exit_code


def run_self_test(yosys_exe: str | None = None) -> None:
    yosys = resolve_yosys_for_verify(yosys_exe)

    with tempfile.TemporaryDirectory(prefix="nebula_verify_rtl_") as tmp:
        root = Path(tmp).resolve()

        # --------------------------------------------------------------
        # Combinational equivalent rewrite.
        # --------------------------------------------------------------

        comb_good, comb_good_exit = _run_selftest_case(
            root=root,
            name="comb_good",
            mode="combinational_cec",
            sequential=False,
            candidate_variant="equivalent",
            yosys=yosys,
        )
        assert comb_good_exit == 0, comb_good
        assert comb_good["ok"] is True, comb_good
        assert comb_good["status"] == EquivalenceVerdict.PROVEN_COMPLETE.value, comb_good
        assert comb_good["golden"]["source_count"] == 2
        assert comb_good["candidate"]["source_count"] == 2
        assert Path(comb_good["proof"]["script"]).is_file()
        assert Path(comb_good["proof"]["log"]).is_file()

        # --------------------------------------------------------------
        # Combinational semantic mutation.
        # --------------------------------------------------------------

        comb_bad, comb_bad_exit = _run_selftest_case(
            root=root,
            name="comb_bad",
            mode="combinational_cec",
            sequential=False,
            candidate_variant="different",
            yosys=yosys,
        )
        assert comb_bad_exit == 3, comb_bad
        assert comb_bad["ok"] is False, comb_bad
        assert comb_bad["status"] == EquivalenceVerdict.DISPROVEN.value, comb_bad

        # --------------------------------------------------------------
        # Sequential equivalent rewrite with frozen state boundary.
        # --------------------------------------------------------------

        seq_good, seq_good_exit = _run_selftest_case(
            root=root,
            name="seq_good",
            mode="sequential_equivalence",
            sequential=True,
            candidate_variant="equivalent",
            yosys=yosys,
        )
        assert seq_good_exit == 0, seq_good
        assert seq_good["ok"] is True, seq_good
        assert seq_good["status"] == EquivalenceVerdict.PROVEN_BOUNDED.value, seq_good

        # --------------------------------------------------------------
        # Sequential semantic mutation.
        # --------------------------------------------------------------

        seq_bad, seq_bad_exit = _run_selftest_case(
            root=root,
            name="seq_bad",
            mode="sequential_equivalence",
            sequential=True,
            candidate_variant="different",
            yosys=yosys,
        )
        assert seq_bad_exit == 1, seq_bad
        assert seq_bad["ok"] is False, seq_bad
        assert seq_bad["status"] == EquivalenceVerdict.NOT_PROVEN.value, seq_bad

        # --------------------------------------------------------------
        # Explicit backend_unsupported must not execute a proof backend.
        # --------------------------------------------------------------

        unsupported_golden = root / "unsupported_golden"
        unsupported_candidate = root / "unsupported_candidate"
        _write_combinational_design(unsupported_golden, variant="golden")
        _write_combinational_design(unsupported_candidate, variant="equivalent")

        g_manifest = _make_selftest_manifest(
            unsupported_golden,
            mode="backend_unsupported",
            sequential=False,
        )
        c_manifest = _make_selftest_manifest(
            unsupported_candidate,
            mode="backend_unsupported",
            sequential=False,
        )

        unsupported_bundle = build_sec_bundle(
            golden_manifest=g_manifest,
            golden_base_dir=unsupported_golden,
            candidate_manifest=c_manifest,
            candidate_base_dir=unsupported_candidate,
            unsupported_reason="self-test intentionally unsupported",
        )

        unsupported_result, unsupported_exit = run_verification(
            unsupported_bundle,
            output_path=root / "unsupported.verify.json",
            yosys_exe="/definitely/not/a/yosys/binary",
        )

        assert unsupported_exit == 2
        assert unsupported_result["ok"] is False
        assert unsupported_result["status"] == EquivalenceVerdict.NOT_ATTEMPTED.value
        assert unsupported_result["proof"] is None

        print("PASS: verify_rtl self-test")
        print("PASS: SECBundle complete filelists consumed")
        print("PASS: include directories and defines propagated")
        print("PASS: combinational equivalent rewrite proven")
        print("PASS: combinational counterexample classified DISPROVEN")
        print("PASS: sequential equivalent rewrite proven")
        print("PASS: incomplete sequential proof classified NOT_PROVEN")
        print("PASS: backend_unsupported handled without proof execution")
        print("PASS: proof scripts/logs emitted")
        print(f"Yosys: {yosys}")


# =============================================================================
# CLI
# =============================================================================


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Execute nEBULA formal equivalence from a canonical sec_bundle/1."
        )
    )

    parser.add_argument(
        "--bundle",
        help="Canonical sec_bundle/1 JSON produced by sec_bundle.py.",
    )

    parser.add_argument(
        "--output",
        help="Verification-result JSON output.",
    )

    parser.add_argument(
        "--yosys",
        default=None,
        help="Optional explicit Yosys executable path.",
    )

    parser.add_argument(
        "--seq-depth",
        type=int,
        default=None,
        help=(
            "Override sequential proof depth. Default comes from "
            "SECBundle verification.options.seq_depth, then 5."
        ),
    )

    parser.add_argument(
        "--timeout-s",
        type=float,
        default=None,
        help=(
            "Override backend timeout in seconds. Default comes from "
            "SECBundle verification.options.timeout_s, then 120."
        ),
    )

    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run combinational/sequential good/bad regression proofs.",
    )

    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.self_test:
        try:
            run_self_test(yosys_exe=args.yosys)
        except Exception as exc:
            print(
                f"SELF-TEST FAILED: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return 1
        return 0

    missing: List[str] = []
    if not args.bundle:
        missing.append("--bundle")
    if not args.output:
        missing.append("--output")

    if missing:
        print(
            "ERROR: missing required argument(s): " + ", ".join(missing),
            file=sys.stderr,
        )
        return 2

    output = Path(args.output).resolve()

    try:
        # verify_files=True is critical: an SEC bundle is a frozen proof input,
        # not merely a list of paths. Any source mutation after bundle creation
        # invalidates the proof request before Yosys runs.
        bundle = SECBundle.read_json(
            Path(args.bundle),
            verify_files=True,
        )

        result, exit_code = run_verification(
            bundle,
            output_path=output,
            yosys_exe=args.yosys,
            seq_depth_override=args.seq_depth,
            timeout_override_s=args.timeout_s,
        )

    except (SECBundleError, VerificationSetupError) as exc:
        result = {
            "tool": "verify_rtl.py",
            "schema": VERIFY_SCHEMA,
            "ok": False,
            "status": EquivalenceVerdict.TOOL_ERROR.value,
            "reason": str(exc),
            "error_kind": "setup_error",
            "bundle": str(Path(args.bundle).resolve()),
            "proof": None,
        }
        exit_code = 2

    except VerificationBackendError as exc:
        result = {
            "tool": "verify_rtl.py",
            "schema": VERIFY_SCHEMA,
            "ok": False,
            "status": EquivalenceVerdict.TOOL_ERROR.value,
            "reason": str(exc),
            "error_kind": "backend_error",
            "bundle": str(Path(args.bundle).resolve()),
            "proof": None,
        }
        exit_code = 2

    except Exception as exc:
        result = {
            "tool": "verify_rtl.py",
            "schema": VERIFY_SCHEMA,
            "ok": False,
            "status": EquivalenceVerdict.TOOL_ERROR.value,
            "reason": f"{type(exc).__name__}: {exc}",
            "error_kind": "internal_error",
            "bundle": str(Path(args.bundle).resolve()),
            "proof": None,
        }
        exit_code = 2

    result["bundle"] = str(Path(args.bundle).resolve())
    write_json(output, result)

    print("============================================================")
    print("nEBULA RTL Verification")
    print("============================================================")
    print(f"Bundle  : {Path(args.bundle).resolve()}")
    print(f"Report  : {output}")
    print(f"Status  : {result.get('status')}")
    print(f"Result  : {'PASS' if result.get('ok') is True else 'REJECT'}")
    print("============================================================")
    print(json.dumps(result, indent=2))

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
