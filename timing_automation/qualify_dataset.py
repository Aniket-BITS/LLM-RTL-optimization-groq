#!/usr/bin/env python3
"""
qualify_dataset.py

Pre-optimization dataset qualification gate for nEBULA.

Purpose
-------
Before a design enters the optimizer, determine whether the current
DesignManifest and source tree are actually usable by the deterministic
lint/formal pipeline.

Qualification checks:

    1. DesignManifest is valid and carries complete source hashes.
    2. SourceBundle can be constructed and every source hash matches disk.
    3. Yosys is available.
    4. The complete source filelist elaborates with the manifest's
       include_dirs, defines, parameters and top.
    5. The design is classified from Yosys JSON as combinational or sequential.
    6. Sequential designs have usable clock metadata.
    7. Clock/reset metadata that explicitly names a top-level port is sane.
    8. The manifest verification mode agrees with the inferred design class,
       unless it is explicitly backend_unsupported.
    9. formal_backend.py reports that the requested backend/mode is executable.

This file does NOT:
    - synthesize or place/route the design
    - run equivalence checking
    - mutate the DesignManifest
    - silently choose a different formal backend

Normal usage
------------

python3 timing_automation/qualify_dataset.py \
    --manifest path/to/design_manifest.json \
    --base-dir . \
    --output ai_runs/design_qualification.json

Optional backend override:

python3 timing_automation/qualify_dataset.py \
    --manifest path/to/design_manifest.json \
    --base-dir . \
    --backend yosys

Self-test
---------

python3 timing_automation/qualify_dataset.py --self-test
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Sequence, Tuple


# =============================================================================
# Local imports
# =============================================================================

try:
    from design_manifest import DesignManifest, VerificationPolicy
    from source_bundle import SourceBundle
    from yosys_structure import (
        YosysDesignRuns,
        YosysStructureError,
        resolve_yosys,
        run_yosys_views,
    )
    from formal_backend import (
        DEFAULT_REGISTRY,
        MODE_BACKEND_UNSUPPORTED,
        MODE_COMBINATIONAL_CEC,
        MODE_SEQUENTIAL_EQUIVALENCE,
        BackendDecision,
    )
except ImportError:
    from timing_automation.design_manifest import DesignManifest, VerificationPolicy
    from timing_automation.source_bundle import SourceBundle
    from timing_automation.yosys_structure import (
        YosysDesignRuns,
        YosysStructureError,
        resolve_yosys,
        run_yosys_views,
    )
    from timing_automation.formal_backend import (
        DEFAULT_REGISTRY,
        MODE_BACKEND_UNSUPPORTED,
        MODE_COMBINATIONAL_CEC,
        MODE_SEQUENTIAL_EQUIVALENCE,
        BackendDecision,
    )


# =============================================================================
# Constants
# =============================================================================

QUALIFICATION_SCHEMA = "dataset_qualification/1"

STATUS_QUALIFIED = "qualified"
STATUS_REJECTED = "rejected"
STATUS_UNSUPPORTED = "unsupported"

SEQUENTIAL_CELL_PREFIXES = (
    "$dff",
    "$adff",
    "$aldff",
    "$sdff",
    "$ff",
    "$sr",
    "$dlatch",
    "$adlatch",
    "$dffsr",
    "$_DFF",
    "$_DFFE",
    "$_DLATCH",
    "$_SR",
    "$_FF",
)

LATCH_CELL_PREFIXES = (
    "$dlatch",
    "$adlatch",
    "$_DLATCH",
)

MEMORY_CELL_PREFIXES = (
    "$mem",
)


# =============================================================================
# Exceptions
# =============================================================================


class DatasetQualificationError(RuntimeError):
    """Raised when qualification cannot be invoked correctly."""


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


def _short_error(exc: BaseException, limit: int = 4000) -> str:
    text = f"{type(exc).__name__}: {exc}"
    if len(text) <= limit:
        return text
    return text[: limit - 16] + "...[truncated]"


def _mapping_copy(value: Any) -> Dict[str, Any]:
    """
    Convert manifest metadata objects such as ClockSpec/ResetSpec to a dict.

    DesignManifest uses dataclass-like objects with to_dict(), but this helper
    also accepts mappings so older manifests remain diagnosable.
    """

    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}

    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        raw = to_dict()
        if isinstance(raw, Mapping):
            return {str(key): item for key, item in raw.items()}

    raw_dict = getattr(value, "__dict__", None)
    if isinstance(raw_dict, Mapping):
        return {
            str(key): item
            for key, item in raw_dict.items()
            if not str(key).startswith("_")
        }

    return {}


def _policy_value(policy: Any, key: str, default: Any = None) -> Any:
    if isinstance(policy, Mapping):
        return policy.get(key, default)
    return getattr(policy, key, default)


def _module_object(data: Mapping[str, Any]) -> Dict[str, Any]:
    modules = data.get("modules") or {}
    if not isinstance(modules, dict):
        return {}
    return modules


def _top_ports(runs: YosysDesignRuns, top: str) -> Dict[str, Dict[str, Any]]:
    modules = _module_object(runs.declarations.json_data)
    module = modules.get(top)

    if not isinstance(module, Mapping):
        return {}

    ports = module.get("ports") or {}
    if not isinstance(ports, Mapping):
        return {}

    result: Dict[str, Dict[str, Any]] = {}

    for name, raw in ports.items():
        if not isinstance(raw, Mapping):
            continue

        bits = raw.get("bits") or []
        result[str(name)] = {
            "direction": str(raw.get("direction", "")),
            "width": len(bits) if isinstance(bits, list) else 0,
        }

    return result


def _strip_simple_bit_select(signal: str) -> str:
    value = signal.strip()

    if value.startswith("\\"):
        value = value[1:]

    if "[" in value:
        value = value.split("[", 1)[0]

    return value


def _metadata_signal(
    metadata: Mapping[str, Any],
    top_ports: Mapping[str, Any],
) -> Tuple[str | None, str | None]:
    """
    Return (signal, source_key).

    Explicit port/signal/net/source keys are authoritative. "name" is used
    only when it directly matches a top-level port, because many manifests use
    name as a logical clock label rather than the RTL port name.
    """

    for key in ("port", "signal", "net", "source"):
        raw = metadata.get(key)
        if isinstance(raw, str) and raw.strip():
            return _strip_simple_bit_select(raw), key

    raw_name = metadata.get("name")
    if isinstance(raw_name, str) and raw_name.strip():
        candidate = _strip_simple_bit_select(raw_name)
        if candidate in top_ports:
            return candidate, "name"

    return None, None


def _positive_period_check(metadata: Mapping[str, Any]) -> Tuple[bool, str | None]:
    for key in ("period_ns", "period_ps", "period"):
        if key not in metadata:
            continue

        value = metadata.get(key)

        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False, f"{key} must be numeric"

        if float(value) <= 0:
            return False, f"{key} must be > 0"

    return True, None


# =============================================================================
# Yosys design classification
# =============================================================================


def _parameter_has_one(value: Any) -> bool:
    if isinstance(value, bool):
        return bool(value)

    if isinstance(value, int):
        return value != 0

    if isinstance(value, str):
        compact = value.strip().lower().replace("_", "")

        # Yosys JSON frequently stores parameter bit-vectors as strings.
        if compact and all(ch in "01xz" for ch in compact):
            return "1" in compact

        try:
            return int(compact, 0) != 0
        except ValueError:
            return "1" in compact

    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return any(_parameter_has_one(item) for item in value)

    return False


def _memory_cell_is_sequential(cell: Mapping[str, Any]) -> bool:
    parameters = cell.get("parameters") or {}

    if not isinstance(parameters, Mapping):
        return False

    for key in (
        "RD_CLK_ENABLE",
        "WR_CLK_ENABLE",
        "CLK_ENABLE",
    ):
        if key in parameters and _parameter_has_one(parameters[key]):
            return True

    # $memwr-style cells can encode a clock port even when parameter naming
    # differs between Yosys versions. Treat an explicit non-empty CLK
    # connection as sequential evidence.
    connections = cell.get("connections") or {}
    if isinstance(connections, Mapping):
        clk = connections.get("CLK")
        if isinstance(clk, list) and clk:
            return True

    return False


def _cell_is_latch(cell_type: str) -> bool:
    return cell_type.startswith(LATCH_CELL_PREFIXES)


def _cell_is_memory(cell_type: str) -> bool:
    return cell_type.startswith(MEMORY_CELL_PREFIXES)


def _cell_is_sequential(cell_type: str, cell: Mapping[str, Any]) -> bool:
    if cell_type.startswith(SEQUENTIAL_CELL_PREFIXES):
        return True

    if _cell_is_memory(cell_type):
        return _memory_cell_is_sequential(cell)

    return False


@dataclass(frozen=True)
class DesignClassification:
    inferred_mode: str
    has_sequential_state: bool
    has_latches: bool
    has_memories: bool
    module_count: int
    cell_count: int
    sequential_cell_count: int
    latch_cell_count: int
    memory_cell_count: int
    sequential_cell_types: Dict[str, int]
    all_cell_types: Dict[str, int]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "inferred_mode": self.inferred_mode,
            "has_sequential_state": self.has_sequential_state,
            "has_latches": self.has_latches,
            "has_memories": self.has_memories,
            "module_count": self.module_count,
            "cell_count": self.cell_count,
            "sequential_cell_count": self.sequential_cell_count,
            "latch_cell_count": self.latch_cell_count,
            "memory_cell_count": self.memory_cell_count,
            "sequential_cell_types": dict(self.sequential_cell_types),
            "all_cell_types": dict(self.all_cell_types),
        }


def classify_elaborated_design(
    runs: YosysDesignRuns,
) -> DesignClassification:
    """
    Classify the elaborated hierarchy from Yosys JSON.

    Yosys `proc` has already converted behavioral always blocks into cells,
    so sequential state is detected from compiler-derived cells rather than
    RTL regexes.
    """

    modules = _module_object(runs.elaborated.json_data)

    all_types: Counter[str] = Counter()
    sequential_types: Counter[str] = Counter()

    cell_count = 0
    sequential_count = 0
    latch_count = 0
    memory_count = 0

    for _module_name, raw_module in sorted(modules.items()):
        if not isinstance(raw_module, Mapping):
            continue

        cells = raw_module.get("cells") or {}
        if not isinstance(cells, Mapping):
            continue

        for _cell_name, raw_cell in cells.items():
            if not isinstance(raw_cell, Mapping):
                continue

            cell_type = str(raw_cell.get("type", ""))
            if not cell_type:
                continue

            cell_count += 1
            all_types[cell_type] += 1

            if _cell_is_memory(cell_type):
                memory_count += 1

            if _cell_is_latch(cell_type):
                latch_count += 1

            if _cell_is_sequential(cell_type, raw_cell):
                sequential_count += 1
                sequential_types[cell_type] += 1

    has_sequential_state = sequential_count > 0

    inferred_mode = (
        MODE_SEQUENTIAL_EQUIVALENCE
        if has_sequential_state
        else MODE_COMBINATIONAL_CEC
    )

    return DesignClassification(
        inferred_mode=inferred_mode,
        has_sequential_state=has_sequential_state,
        has_latches=latch_count > 0,
        has_memories=memory_count > 0,
        module_count=len(modules),
        cell_count=cell_count,
        sequential_cell_count=sequential_count,
        latch_cell_count=latch_count,
        memory_cell_count=memory_count,
        sequential_cell_types=dict(sorted(sequential_types.items())),
        all_cell_types=dict(sorted(all_types.items())),
    )


# =============================================================================
# Clock/reset metadata validation
# =============================================================================


def validate_clock_metadata(
    manifest: DesignManifest,
    classification: DesignClassification,
    top_ports: Mapping[str, Mapping[str, Any]],
) -> Tuple[bool, List[Dict[str, Any]], List[str], List[str]]:
    """
    Validate declared clock metadata.

    A sequential design must declare at least one clock. If a clock object
    explicitly names a port/signal/net/source, that signal must be a top input.
    """

    raw_clocks = list(getattr(manifest, "clocks", []) or [])
    details: List[Dict[str, Any]] = []
    reasons: List[str] = []
    warnings: List[str] = []

    if classification.has_sequential_state and not raw_clocks:
        reasons.append(
            "sequential state detected by Yosys but DesignManifest.clocks is empty"
        )
        return False, details, reasons, warnings

    all_ok = True

    for index, clock in enumerate(raw_clocks):
        metadata = _mapping_copy(clock)
        signal, source_key = _metadata_signal(metadata, top_ports)

        period_ok, period_reason = _positive_period_check(metadata)

        item: Dict[str, Any] = {
            "index": index,
            "metadata": metadata,
            "resolved_top_signal": signal,
            "signal_source_key": source_key,
            "period_sane": period_ok,
            "port_sane": True,
        }

        if not period_ok:
            all_ok = False
            item["port_sane"] = item["port_sane"]
            reasons.append(f"clock[{index}]: {period_reason}")

        if signal is not None:
            port = top_ports.get(signal)

            if port is None:
                all_ok = False
                item["port_sane"] = False
                reasons.append(
                    f"clock[{index}] references top signal {signal!r}, "
                    "but that port does not exist"
                )
            else:
                direction = str(port.get("direction", ""))
                if direction != "input":
                    all_ok = False
                    item["port_sane"] = False
                    reasons.append(
                        f"clock[{index}] signal {signal!r} is not a top input "
                        f"(direction={direction!r})"
                    )
        else:
            warnings.append(
                f"clock[{index}] has no explicit port/signal/net/source field "
                "that can be checked against the top interface"
            )

        details.append(item)

    return all_ok, details, reasons, warnings


def validate_reset_metadata(
    manifest: DesignManifest,
    top_ports: Mapping[str, Mapping[str, Any]],
) -> Tuple[bool, List[Dict[str, Any]], List[str], List[str]]:
    raw_resets = list(getattr(manifest, "resets", []) or [])
    details: List[Dict[str, Any]] = []
    reasons: List[str] = []
    warnings: List[str] = []
    all_ok = True

    for index, reset in enumerate(raw_resets):
        metadata = _mapping_copy(reset)
        signal, source_key = _metadata_signal(metadata, top_ports)

        item: Dict[str, Any] = {
            "index": index,
            "metadata": metadata,
            "resolved_top_signal": signal,
            "signal_source_key": source_key,
            "port_sane": True,
        }

        if signal is not None:
            port = top_ports.get(signal)

            if port is None:
                all_ok = False
                item["port_sane"] = False
                reasons.append(
                    f"reset[{index}] references top signal {signal!r}, "
                    "but that port does not exist"
                )
            else:
                direction = str(port.get("direction", ""))
                if direction != "input":
                    all_ok = False
                    item["port_sane"] = False
                    reasons.append(
                        f"reset[{index}] signal {signal!r} is not a top input "
                        f"(direction={direction!r})"
                    )
        else:
            warnings.append(
                f"reset[{index}] has no explicit port/signal/net/source field "
                "that can be checked against the top interface"
            )

        details.append(item)

    return all_ok, details, reasons, warnings


# =============================================================================
# Main qualification API
# =============================================================================


def qualify_dataset(
    *,
    manifest: DesignManifest,
    base_dir: Path,
    yosys_exe: str | None = None,
    backend_override: str | None = None,
    require_timing: bool = True,
) -> Dict[str, Any]:
    """
    Qualify one DesignManifest for entry into the optimizer.

    Returns a structured result instead of raising for ordinary design
    rejection/unsupported outcomes.
    """

    base_dir = Path(base_dir).resolve()

    checks: Dict[str, bool] = {}
    reasons: List[str] = []
    warnings: List[str] = []

    source_bundle: SourceBundle | None = None
    runs: YosysDesignRuns | None = None
    classification: DesignClassification | None = None
    backend_decision: BackendDecision | None = None
    yosys_path: str | None = None
    top_ports: Dict[str, Dict[str, Any]] = {}
    clock_details: List[Dict[str, Any]] = []
    reset_details: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------
    # 1. Manifest schema + source hashes present.
    # ------------------------------------------------------------------

    try:
        manifest.validate(require_source_hashes=True)
        checks["manifest_valid"] = True
    except Exception as exc:
        checks["manifest_valid"] = False
        reasons.append(f"DesignManifest validation failed: {_short_error(exc)}")

    # ------------------------------------------------------------------
    # 2. SourceBundle + exact source hash verification.
    # ------------------------------------------------------------------

    if checks["manifest_valid"]:
        try:
            source_bundle = SourceBundle.from_manifest(
                manifest,
                base_dir,
                verify_manifest_hashes=True,
            )
            checks["source_bundle_valid"] = True
        except Exception as exc:
            checks["source_bundle_valid"] = False
            reasons.append(f"SourceBundle validation failed: {_short_error(exc)}")
    else:
        checks["source_bundle_valid"] = False

    # ------------------------------------------------------------------
    # 3. Yosys availability.
    # ------------------------------------------------------------------

    if checks["source_bundle_valid"]:
        try:
            yosys_path = str(resolve_yosys(yosys_exe))
            checks["yosys_available"] = True
        except Exception as exc:
            checks["yosys_available"] = False
            reasons.append(f"Yosys unavailable: {_short_error(exc)}")
    else:
        checks["yosys_available"] = False

    # ------------------------------------------------------------------
    # 4. Complete compile-context elaboration.
    # ------------------------------------------------------------------

    if checks["yosys_available"]:
        try:
            runs = run_yosys_views(
                manifest,
                base_dir,
                yosys_exe=yosys_path,
            )

            declaration_modules = _module_object(runs.declarations.json_data)
            elaborated_modules = _module_object(runs.elaborated.json_data)

            top_in_declarations = manifest.top in declaration_modules
            top_in_elaboration = manifest.top in elaborated_modules

            checks["top_declared"] = top_in_declarations
            checks["top_elaborated"] = top_in_elaboration
            checks["yosys_elaborates"] = (
                top_in_declarations and top_in_elaboration
            )

            if not top_in_declarations:
                reasons.append(
                    f"top module {manifest.top!r} missing from Yosys declaration view"
                )

            if not top_in_elaboration:
                reasons.append(
                    f"top module {manifest.top!r} missing after Yosys hierarchy elaboration"
                )

        except Exception as exc:
            checks["top_declared"] = False
            checks["top_elaborated"] = False
            checks["yosys_elaborates"] = False
            reasons.append(f"Yosys elaboration failed: {_short_error(exc)}")
    else:
        checks["top_declared"] = False
        checks["top_elaborated"] = False
        checks["yosys_elaborates"] = False

    # ------------------------------------------------------------------
    # 5. Compiler-derived design classification.
    # ------------------------------------------------------------------

    if runs is not None and checks["yosys_elaborates"]:
        classification = classify_elaborated_design(runs)
        top_ports = _top_ports(runs, manifest.top)
        checks["design_classified"] = True
    else:
        checks["design_classified"] = False

    # ------------------------------------------------------------------
    # 6. Clock/reset metadata sanity.
    # ------------------------------------------------------------------

    if classification is not None:
        (
            clock_ok,
            clock_details,
            clock_reasons,
            clock_warnings,
        ) = validate_clock_metadata(
            manifest,
            classification,
            top_ports,
        )

        (
            reset_ok,
            reset_details,
            reset_reasons,
            reset_warnings,
        ) = validate_reset_metadata(
            manifest,
            top_ports,
        )

        checks["clock_metadata_sane"] = clock_ok
        checks["reset_metadata_sane"] = reset_ok

        reasons.extend(clock_reasons)
        reasons.extend(reset_reasons)
        warnings.extend(clock_warnings)
        warnings.extend(reset_warnings)

        if classification.has_latches:
            warnings.append(
                "Yosys detected latch state; qualification does not reject "
                "baseline latches, but optimizer safety checks must preserve "
                "the register/latch boundary"
            )

        if classification.has_memories:
            warnings.append(
                "Yosys detected memory cells; formal support may depend on the "
                "selected backend and memory semantics"
            )

        if not classification.has_sequential_state and getattr(manifest, "clocks", []):
            warnings.append(
                "DesignManifest declares clock metadata but Yosys detected no "
                "sequential state; inferred verification mode is combinational_cec"
            )

    else:
        checks["clock_metadata_sane"] = False
        checks["reset_metadata_sane"] = False

    # ------------------------------------------------------------------
    # 7. Verification policy must agree with inferred design class.
    # ------------------------------------------------------------------

    verification = getattr(manifest, "verification", None)

    policy_backend = _policy_value(verification, "backend")
    policy_mode = _policy_value(verification, "mode")
    policy_options = _policy_value(verification, "options", {})

    if not isinstance(policy_options, Mapping):
        policy_options = {}

    verification_policy_present = (
        isinstance(policy_backend, str)
        and bool(policy_backend.strip())
        and isinstance(policy_mode, str)
        and bool(policy_mode.strip())
    )

    checks["verification_policy_present"] = verification_policy_present

    selected_backend = (
        backend_override.strip()
        if isinstance(backend_override, str) and backend_override.strip()
        else (
            policy_backend.strip()
            if isinstance(policy_backend, str)
            else ""
        )
    )

    if not verification_policy_present:
        reasons.append(
            "DesignManifest.verification must define non-empty backend and mode"
        )

    explicit_backend_unsupported = (
        verification_policy_present
        and policy_mode == MODE_BACKEND_UNSUPPORTED
    )

    if classification is not None and verification_policy_present:
        if explicit_backend_unsupported:
            # This is an intentional policy state, not a design-class mismatch.
            checks["verification_mode_matches_design"] = True
        else:
            mode_matches = policy_mode == classification.inferred_mode
            checks["verification_mode_matches_design"] = mode_matches

            if not mode_matches:
                reasons.append(
                    "verification mode does not match compiler-derived design "
                    f"class: manifest={policy_mode!r}, "
                    f"inferred={classification.inferred_mode!r}"
                )
    else:
        checks["verification_mode_matches_design"] = False

    # ------------------------------------------------------------------
    # 8. Formal backend capability.
    # ------------------------------------------------------------------

    if (
        classification is not None
        and verification_policy_present
        and selected_backend
    ):
        if explicit_backend_unsupported:
            unsupported_reason = None

            for key in ("unsupported_reason", "reason"):
                raw = policy_options.get(key)
                if isinstance(raw, str) and raw.strip():
                    unsupported_reason = raw.strip()
                    break

            backend_decision = DEFAULT_REGISTRY.decision(
                backend=selected_backend,
                mode=MODE_BACKEND_UNSUPPORTED,
                bundle_execution_ready=False,
                unsupported_reason=unsupported_reason,
            )
        else:
            backend_decision = DEFAULT_REGISTRY.decision(
                backend=selected_backend,
                mode=classification.inferred_mode,
                bundle_execution_ready=True,
            )

        checks["formal_backend_ready"] = backend_decision.ok

        if not backend_decision.ok:
            warnings.append(
                "formal backend is not execution-ready: "
                f"{backend_decision.status}: {backend_decision.reason}"
            )
    else:
        checks["formal_backend_ready"] = False

    # ------------------------------------------------------------------
    # 9. Final status.
    # ------------------------------------------------------------------

    try:
        from constraint_model import validate_model
    except ImportError:
        from timing_automation.constraint_model import validate_model
    timing_qualification = validate_model(manifest.clocks, manifest.constraints, top_ports) if require_timing else {"ok": True, "structural_only": True, "errors": []}
    checks["timing_constraints_complete"] = timing_qualification["ok"]
    reasons.extend(timing_qualification["errors"])

    hard_checks = (
        "timing_constraints_complete",
        "manifest_valid",
        "source_bundle_valid",
        "yosys_available",
        "yosys_elaborates",
        "design_classified",
        "clock_metadata_sane",
        "reset_metadata_sane",
        "verification_policy_present",
        "verification_mode_matches_design",
    )

    hard_ok = all(checks.get(name) is True for name in hard_checks)

    if hard_ok and checks.get("formal_backend_ready") is True:
        status = STATUS_QUALIFIED
        qualified = True
    elif hard_ok and backend_decision is not None:
        status = STATUS_UNSUPPORTED
        qualified = False
    else:
        status = STATUS_REJECTED
        qualified = False

    # ------------------------------------------------------------------
    # Result.
    # ------------------------------------------------------------------

    result: Dict[str, Any] = {
        "tool": "qualify_dataset.py",
        "schema": QUALIFICATION_SCHEMA,
        "status": status,
        "qualified": qualified,
        "design": {
            "dataset": getattr(manifest, "dataset", None),
            "design_id": getattr(manifest, "design_id", None),
            "top": getattr(manifest, "top", None),
            "language": getattr(manifest, "language", None),
            "base_dir": str(base_dir),
            "source_count": len(getattr(manifest, "source_files", []) or []),
        },
        "verification_policy": {
            "manifest_backend": policy_backend,
            "selected_backend": selected_backend or None,
            "manifest_mode": policy_mode,
            "options": dict(policy_options),
        },
        "classification": (
            classification.to_dict()
            if classification is not None
            else None
        ),
        "top_interface": {
            "ports": top_ports,
        },
        "timing_qualification": timing_qualification,
        "timing_metadata": {
            "clocks": clock_details,
            "resets": reset_details,
        },
        "formal_backend": (
            backend_decision.to_dict()
            if backend_decision is not None
            else None
        ),
        "environment": {
            "yosys": yosys_path,
        },
        "checks": checks,
        "warnings": warnings,
        "reasons": reasons,
    }

    return result


# =============================================================================
# Self-test
# =============================================================================


def _write_comb_design(root: Path) -> None:
    rtl = root / "rtl"
    rtl.mkdir(parents=True, exist_ok=True)

    (rtl / "qualify_defs.vh").write_text(
        "`define QUALIFY_MASK 8'h0f\n",
        encoding="utf-8",
    )

    (rtl / "top.v").write_text(
        "`include \"qualify_defs.vh\"\n"
        "module top (\n"
        "    input  wire [7:0] a,\n"
        "    input  wire [7:0] b,\n"
        "    output wire [7:0] y\n"
        ");\n"
        "`ifdef NEBULA_QUALIFY_TEST\n"
        "assign y = (a & b) ^ `QUALIFY_MASK;\n"
        "`else\n"
        "assign y = 8'h00;\n"
        "`endif\n"
        "endmodule\n",
        encoding="utf-8",
    )


def _write_seq_design(root: Path) -> None:
    rtl = root / "rtl"
    rtl.mkdir(parents=True, exist_ok=True)

    (rtl / "top.v").write_text(
        "module top (\n"
        "    input  wire       clk,\n"
        "    input  wire [7:0] d,\n"
        "    output reg  [7:0] q\n"
        ");\n"
        "always @(posedge clk) begin\n"
        "    q <= d;\n"
        "end\n"
        "endmodule\n",
        encoding="utf-8",
    )


def _make_manifest(
    root: Path,
    *,
    design_id: str,
    mode: str,
    backend: str = "yosys",
    include_dirs: Sequence[str] = (),
    defines: Sequence[str] = (),
) -> DesignManifest:
    manifest = DesignManifest(
        dataset="qualify_dataset_self_test",
        design_id=design_id,
        top="top",
        source_files=["rtl/top.v"],
        include_dirs=list(include_dirs),
        defines=list(defines),
        parameters={},
        language="verilog",
        clocks=[],
        resets=[],
        constraints={},
        verification=VerificationPolicy(
            backend=backend,
            mode=mode,
            options={"timeout_s": 30},
        ),
    )
    manifest.refresh_source_hashes(root)
    return manifest


def run_self_test(yosys_exe: str | None = None) -> None:
    yosys = resolve_yosys(yosys_exe)

    with tempfile.TemporaryDirectory(prefix="nebula_qualify_dataset_") as tmp:
        root = Path(tmp).resolve()

        # ------------------------------------------------------------------
        # 1. Valid combinational dataset.
        # ------------------------------------------------------------------

        comb_root = root / "comb"
        _write_comb_design(comb_root)

        comb_manifest = _make_manifest(
            comb_root,
            design_id="comb",
            mode=MODE_COMBINATIONAL_CEC,
            include_dirs=["rtl"],
            defines=["NEBULA_QUALIFY_TEST=1"],
        )

        comb = qualify_dataset(
            require_timing=False,
            manifest=comb_manifest,
            base_dir=comb_root,
            yosys_exe=str(yosys),
        )

        assert comb["qualified"] is True, comb
        assert comb["status"] == STATUS_QUALIFIED
        assert comb["classification"]["inferred_mode"] == MODE_COMBINATIONAL_CEC
        assert comb["checks"]["source_bundle_valid"] is True
        assert comb["checks"]["yosys_elaborates"] is True
        assert comb["checks"]["formal_backend_ready"] is True

        # ------------------------------------------------------------------
        # 2. Sequential RTL is compiler-classified and rejected if clock
        #    metadata is missing.
        # ------------------------------------------------------------------

        seq_root = root / "seq_missing_clock"
        _write_seq_design(seq_root)

        seq_manifest = _make_manifest(
            seq_root,
            design_id="seq_missing_clock",
            mode=MODE_SEQUENTIAL_EQUIVALENCE,
        )

        seq = qualify_dataset(
            require_timing=False,
            manifest=seq_manifest,
            base_dir=seq_root,
            yosys_exe=str(yosys),
        )

        assert seq["qualified"] is False
        assert seq["status"] == STATUS_REJECTED
        assert seq["classification"]["inferred_mode"] == MODE_SEQUENTIAL_EQUIVALENCE
        assert seq["classification"]["has_sequential_state"] is True
        assert seq["checks"]["clock_metadata_sane"] is False

        # ------------------------------------------------------------------
        # 3. Mode mismatch must fail closed.
        # ------------------------------------------------------------------

        mismatch_manifest = _make_manifest(
            comb_root,
            design_id="comb_mode_mismatch",
            mode=MODE_SEQUENTIAL_EQUIVALENCE,
            include_dirs=["rtl"],
            defines=["NEBULA_QUALIFY_TEST=1"],
        )

        mismatch = qualify_dataset(
            require_timing=False,
            manifest=mismatch_manifest,
            base_dir=comb_root,
            yosys_exe=str(yosys),
        )

        assert mismatch["qualified"] is False
        assert mismatch["status"] == STATUS_REJECTED
        assert mismatch["checks"]["verification_mode_matches_design"] is False

        # ------------------------------------------------------------------
        # 4. Known but unimplemented backend is UNSUPPORTED, not qualified.
        # ------------------------------------------------------------------

        unavailable_manifest = _make_manifest(
            comb_root,
            design_id="comb_jasper",
            mode=MODE_COMBINATIONAL_CEC,
            backend="jaspergold",
            include_dirs=["rtl"],
            defines=["NEBULA_QUALIFY_TEST=1"],
        )

        unavailable = qualify_dataset(
            require_timing=False,
            manifest=unavailable_manifest,
            base_dir=comb_root,
            yosys_exe=str(yosys),
        )

        assert unavailable["qualified"] is False
        assert unavailable["status"] == STATUS_UNSUPPORTED
        assert unavailable["formal_backend"]["status"] == "backend_unavailable"

        # ------------------------------------------------------------------
        # 5. Explicit backend_unsupported policy remains explicit.
        # ------------------------------------------------------------------

        unsupported_manifest = _make_manifest(
            comb_root,
            design_id="comb_explicit_unsupported",
            mode=MODE_BACKEND_UNSUPPORTED,
            backend="yosys",
            include_dirs=["rtl"],
            defines=["NEBULA_QUALIFY_TEST=1"],
        )

        unsupported = qualify_dataset(
            require_timing=False,
            manifest=unsupported_manifest,
            base_dir=comb_root,
            yosys_exe=str(yosys),
        )

        assert unsupported["qualified"] is False
        assert unsupported["status"] == STATUS_UNSUPPORTED
        assert unsupported["formal_backend"]["status"] == "design_unsupported"

        # ------------------------------------------------------------------
        # 6. Stale source hash must be rejected before optimization.
        # ------------------------------------------------------------------

        stale_root = root / "stale"
        _write_comb_design(stale_root)

        stale_manifest = _make_manifest(
            stale_root,
            design_id="stale",
            mode=MODE_COMBINATIONAL_CEC,
            include_dirs=["rtl"],
            defines=["NEBULA_QUALIFY_TEST=1"],
        )

        (stale_root / "rtl" / "top.v").write_text(
            (stale_root / "rtl" / "top.v").read_text(encoding="utf-8")
            + "\n// source mutated after manifest hashing\n",
            encoding="utf-8",
        )

        stale = qualify_dataset(
            require_timing=False,
            manifest=stale_manifest,
            base_dir=stale_root,
            yosys_exe=str(yosys),
        )

        assert stale["qualified"] is False
        assert stale["status"] == STATUS_REJECTED
        assert stale["checks"]["source_bundle_valid"] is False

        print("PASS: qualify_dataset self-test")
        print("PASS: valid combinational dataset qualified")
        print("PASS: complete include/define compile context elaborated")
        print("PASS: Yosys-derived combinational/sequential classification works")
        print("PASS: sequential design without clock metadata rejected")
        print("PASS: verification-mode mismatch rejected")
        print("PASS: unavailable formal backend reported as unsupported")
        print("PASS: backend_unsupported policy preserved explicitly")
        print("PASS: stale source hash rejected before optimization")
        print(f"Yosys: {yosys}")


# =============================================================================
# CLI
# =============================================================================


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Qualify one nEBULA DesignManifest before optimization."
        )
    )

    parser.add_argument(
        "--manifest",
        default=None,
        help="DesignManifest JSON to qualify.",
    )

    parser.add_argument(
        "--base-dir",
        default=".",
        help="Base directory used to resolve manifest source/include paths.",
    )

    parser.add_argument(
        "--backend",
        default=None,
        help=(
            "Optional formal-backend override used only for qualification. "
            "The manifest is not modified."
        ),
    )

    parser.add_argument(
        "--yosys",
        default=None,
        help="Optional explicit Yosys executable path.",
    )

    parser.add_argument(
        "--output",
        default=None,
        help="Optional qualification JSON output.",
    )

    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run standalone regression tests.",
    )

    parser.add_argument("--structural-only", action="store_true", help="Check RTL/formal compatibility only; does not qualify a physical optimization baseline.")
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

    if not args.manifest:
        print("ERROR: --manifest is required", file=sys.stderr)
        return 2

    try:
        manifest_path = Path(args.manifest)

        if not manifest_path.is_file():
            raise DatasetQualificationError(
                f"manifest file not found: {manifest_path}"
            )

        manifest = DesignManifest.read_json(manifest_path)

        result = qualify_dataset(
            manifest=manifest,
            base_dir=Path(args.base_dir),
            yosys_exe=args.yosys,
            backend_override=args.backend,
            require_timing=not args.structural_only,
        )

        if args.output:
            write_json(Path(args.output), result)

        print(json.dumps(result, indent=2))

        return 0 if result.get("qualified") is True else 1

    except DatasetQualificationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(
            f"ERROR: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
