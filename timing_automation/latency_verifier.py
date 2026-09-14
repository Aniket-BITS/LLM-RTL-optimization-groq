#!/usr/bin/env python3
"""Backend-authoritative fixed-cycle temporal equivalence for nEBULA.

For a tested shift D >= 0, prove after the frozen reset/warm-up contract:

    candidate_output(t) == golden_output(t-D)

which is equivalent to candidate_output(t + D) == golden_output(t).

The model's predicted latency is used only to order the proof attempts.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

try:
    from design_manifest import DesignManifest
    from latency_contract import LatencyContract
    from yosys_structure import read_verilog_command, quote_yosys_token, yosys_identifier
except ImportError:  # pragma: no cover
    from timing_automation.design_manifest import DesignManifest
    from timing_automation.latency_contract import LatencyContract
    from timing_automation.yosys_structure import read_verilog_command, quote_yosys_token, yosys_identifier

_SIMPLE_ID = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _formal_status(run: Mapping[str, Any], log_text: str) -> tuple[str, str]:
    exit_code = run.get("exit_code")
    if exit_code == 0:
        return "passed", "proof_closed"
    if exit_code == 124 or run.get("status") == "timeout":
        return "inconclusive_timeout", "formal_timeout_is_not_a_functional_failure"
    lowered = log_text.lower()
    if "proof did fail" in lowered or "model found" in lowered or "assert failed" in lowered:
        return "semantic_mismatch", "formal_counterexample"
    return "tool_failure", "formal_tool_or_setup_failure"


def _decl(name: str, width: int, *, kind: str = "wire") -> str:
    if not _SIMPLE_ID.fullmatch(name):
        raise ValueError(f"phase-1 latency harness does not support escaped port name {name!r}")
    prefix = "" if width == 1 else f"[{width - 1}:0] "
    return f"{kind} {prefix}{name}"


def _port_width(port: Mapping[str, Any]) -> int:
    bits = port.get("bits")
    if not isinstance(bits, list) or not bits:
        raise ValueError("invalid Yosys port bits")
    return len(bits)


def _build_wrapper(
    *,
    ports: Mapping[str, Mapping[str, Any]],
    contract: LatencyContract,
    shift_cycles: int,
) -> str:
    if not contract.reference_clock:
        raise ValueError("latency contract has no reference clock")
    clk = contract.reference_clock
    if clk not in ports:
        raise ValueError(f"reference clock {clk!r} not in top-level ports")

    inputs: list[tuple[str, int]] = []
    outputs: list[tuple[str, int]] = []
    for name, port in ports.items():
        direction = port.get("direction")
        width = _port_width(port)
        if direction == "input":
            inputs.append((name, width))
        elif direction == "output":
            outputs.append((name, width))
        else:
            raise ValueError(f"phase-1 latency verification does not support inout port {name!r}")
    if not outputs:
        raise ValueError("top module has no observable outputs")

    # Reset is generated internally so both designs receive exactly the same
    # initialization trace. Other inputs, including the clock, remain harness inputs.
    external_inputs = [(n, w) for n, w in inputs if n != contract.reset_port]
    lines = ["module nebula_latency_harness("]
    lines += [
        "    " + _decl(name, width, kind="input wire") + ("," if i + 1 < len(external_inputs) else "")
        for i, (name, width) in enumerate(external_inputs)
    ]
    lines.append(");")
    lines.append("    reg [31:0] nebula_phase = 0;")
    lines.append(f"    always @({contract.clock_edge} {clk}) begin")
    lines.append("        if (nebula_phase != 32'hffffffff) nebula_phase <= nebula_phase + 1'b1;")
    lines.append("    end")

    if contract.reset_port:
        active = int(contract.reset_active_level if contract.reset_active_level is not None else 1)
        inactive = 1 - active
        lines.append(
            f"    wire {contract.reset_port} = (nebula_phase < {int(contract.initial_reset_cycles)}) ? "
            f"1'b{active} : 1'b{inactive};"
        )

    for name, width in outputs:
        lines.append("    " + _decl(f"gold_{name}", width) + ";")
        lines.append("    " + _decl(f"cand_{name}", width) + ";")
        for stage in range(shift_cycles):
            lines.append("    " + _decl(f"gold_delay_{name}_{stage}", width, kind="reg") + " = 0;")

    def connections(output_prefix: str) -> str:
        conns: list[str] = []
        for name, _width in inputs:
            conns.append(f".{name}({name})")
        for name, _width in outputs:
            conns.append(f".{name}({output_prefix}_{name})")
        return ", ".join(conns)

    lines.append(f"    nebula_gold gold_i({connections('gold')});")
    lines.append(f"    nebula_gate cand_i({connections('cand')});")

    # One extra cycle beyond reset+delay avoids comparing on the release/fill edge.
    compare_phase = int(contract.initial_reset_cycles) + int(shift_cycles) + 1
    lines.append(f"    always @({contract.clock_edge} {clk}) begin")
    for name, _width in outputs:
        if shift_cycles:
            lines.append(f"        gold_delay_{name}_0 <= gold_{name};")
            for stage in range(1, shift_cycles):
                lines.append(f"        gold_delay_{name}_{stage} <= gold_delay_{name}_{stage - 1};")
    lines.append(f"        if (nebula_phase >= {compare_phase}) begin")
    for name, _width in outputs:
        expected = f"gold_delay_{name}_{shift_cycles - 1}" if shift_cycles else f"gold_{name}"
        lines.append(f"            assert(cand_{name} == {expected});")
    lines.append("        end")
    lines.append("    end")
    lines.append("endmodule")
    return "\n".join(lines) + "\n"


def _proof_script(
    *,
    golden_manifest: DesignManifest,
    golden_base_dir: Path,
    candidate_manifest: DesignManifest,
    candidate_base_dir: Path,
    wrapper: Path,
    maxsteps: int,
    seq_depth: int,
) -> str:
    gold_top = yosys_identifier(golden_manifest.top)
    cand_top = yosys_identifier(candidate_manifest.top)
    lines: list[str] = []
    for manifest, base_dir, alias, stash in (
        (golden_manifest, golden_base_dir, "nebula_gold", "gold_design"),
        (candidate_manifest, candidate_base_dir, "nebula_gate", "gate_design"),
    ):
        lines.extend([
            "design -reset-vlog",
            read_verilog_command(manifest, base_dir),
            f"prep -flatten -top {yosys_identifier(manifest.top)}",
            "memory_map",
            "opt_clean",
            f"rename {yosys_identifier(manifest.top)} {alias}",
            f"design -stash {stash}",
        ])
    lines.extend([
        "design -copy-from gold_design -as nebula_gold nebula_gold",
        "design -copy-from gate_design -as nebula_gate nebula_gate",
        f"read_verilog -formal -sv {quote_yosys_token(str(wrapper))}",
        "prep -flatten -top nebula_latency_harness",
        "async2sync",
        "opt -full",
        f"sat -verify -prove-asserts -seq {int(seq_depth)} -tempinduct -maxsteps {int(maxsteps)} "
        "-show-inputs -show-outputs nebula_latency_harness",
    ])
    return "\n".join(lines) + "\n"


def _search_order(maximum: int, current_best_total: int, predicted_iteration_delta: int | None, diagnostic_extra: int) -> list[int]:
    upper = maximum + max(0, diagnostic_extra)
    order: list[int] = []
    if predicted_iteration_delta is not None:
        hint = current_best_total + predicted_iteration_delta
        if 0 <= hint <= upper:
            order.append(hint)
    if 0 <= current_best_total <= upper and current_best_total not in order:
        order.append(current_best_total)
    for value in range(0, upper + 1):
        if value not in order:
            order.append(value)
    return order


def verify_fixed_latency(
    *,
    golden_manifest: DesignManifest,
    golden_base_dir: Path,
    candidate_manifest: DesignManifest,
    candidate_base_dir: Path,
    elaborated_golden_module: Mapping[str, Any],
    contract: LatencyContract,
    output_dir: Path,
    yosys_exe: str | Path,
    run_command,
    current_best_total_latency_regression_cycles: int = 0,
    predicted_latency_change_cycles: int | None = None,
    timeout_s: int = 120,
    maxsteps: int = 24,
) -> dict[str, Any]:
    """Try candidate-vs-Golden fixed-cycle shifts and return backend truth."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {
        "schema": "nebula_latency_result/1",
        "status": "unsupported_contract" if not contract.supported else "started",
        "passed": False,
        "reference": "frozen_golden",
        "contract": contract.to_dict(),
        "current_best_total_latency_regression_cycles": int(current_best_total_latency_regression_cycles),
        "predicted_latency_change_cycles": predicted_latency_change_cycles,
        "prediction_error_cycles": None,
        "tested_shifts": [],
        "verified_total_latency_regression_cycles": None,
        "iteration_latency_delta_cycles": None,
        "verified_shift_cycles": None,
        "max_latency_regression_cycles": int(contract.max_latency_regression_cycles),
        "formal_status": None,
    }
    if not contract.supported:
        result["reason"] = contract.reason
        return result

    ports = elaborated_golden_module.get("ports") if isinstance(elaborated_golden_module, Mapping) else None
    if not isinstance(ports, Mapping):
        result.update(status="unsupported_contract", reason="elaborated Golden ports unavailable")
        return result

    order = _search_order(
        int(contract.max_latency_regression_cycles),
        int(current_best_total_latency_regression_cycles),
        predicted_latency_change_cycles,
        int(contract.diagnostic_extra_cycles),
    )
    attempts: list[dict[str, Any]] = []
    saw_timeout = False
    saw_tool_failure = False
    for shift in order:
        shift_dir = output_dir / f"shift_{shift:03d}"
        shift_dir.mkdir(parents=True, exist_ok=True)
        wrapper = shift_dir / "latency_harness.v"
        script = shift_dir / "latency_proof.ys"
        log = shift_dir / "latency_proof.log"
        try:
            wrapper.write_text(_build_wrapper(ports=ports, contract=contract, shift_cycles=shift), encoding="utf-8")
            # Give induction enough sequential frames to see reset release and the
            # complete delay chain, while keeping the user's existing maxsteps cap.
            seq_depth = max(4, contract.initial_reset_cycles + shift + 3)
            script.write_text(
                _proof_script(
                    golden_manifest=golden_manifest,
                    golden_base_dir=Path(golden_base_dir),
                    candidate_manifest=candidate_manifest,
                    candidate_base_dir=Path(candidate_base_dir),
                    wrapper=wrapper,
                    maxsteps=maxsteps,
                    seq_depth=seq_depth,
                ),
                encoding="utf-8",
            )
            run = run_command([str(yosys_exe), "-s", str(script)], log, timeout_s)
            log_text = log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""
            status, reason = _formal_status(run, log_text)
            attempt = {
                "shift_cycles": shift,
                "status": status,
                "reason": reason,
                "exit_code": run.get("exit_code"),
                "log": str(log),
                "script": str(script),
                "script_sha256": _sha(script),
                "harness": str(wrapper),
                "harness_sha256": _sha(wrapper),
            }
        except Exception as exc:
            status = "tool_failure"
            saw_tool_failure = True
            attempt = {
                "shift_cycles": shift,
                "status": status,
                "reason": f"{type(exc).__name__}: {exc}",
            }
        attempts.append(attempt)
        result["tested_shifts"].append(shift)

        if status == "passed":
            total = int(shift)
            current = int(current_best_total_latency_regression_cycles)
            result.update(
                verified_total_latency_regression_cycles=total,
                iteration_latency_delta_cycles=total - current,
                verified_shift_cycles=total,
                formal_status="passed",
                proof_attempts=attempts,
            )
            if predicted_latency_change_cycles is not None:
                result["prediction_error_cycles"] = (total - current) - int(predicted_latency_change_cycles)
            if total <= contract.max_latency_regression_cycles:
                result.update(status="verified", passed=True, reason="temporal_equivalence_proven_within_latency_budget")
            else:
                result.update(status="latency_budget_exceeded", passed=False, reason="temporal_equivalence_proven_only_beyond_latency_budget")
            return result
        if status == "inconclusive_timeout":
            saw_timeout = True
        elif status == "tool_failure":
            saw_tool_failure = True

    result["proof_attempts"] = attempts
    if saw_timeout:
        result.update(status="inconclusive_timeout", formal_status="inconclusive_timeout",
                      reason="no permitted shift was proven and at least one proof timed out")
    elif saw_tool_failure:
        result.update(status="tool_failure", formal_status="tool_failure",
                      reason="no permitted shift was proven and at least one proof had a tool/setup failure")
    else:
        result.update(status="no_allowed_latency_relation", formal_status="no_allowed_latency_relation",
                      reason="all tested fixed-cycle relations within the bounded search produced counterexamples")
    return result


def cycle_exact_result(proof: Mapping[str, Any], *, current_best_total_latency_regression_cycles: int = 0) -> dict[str, Any]:
    """Normalize the existing zero-latency formal result into the latency taxonomy.

    Nonzero exit codes are separated into semantic counterexamples, timeouts, and
    tool/setup failures when the existing proof log is available.
    """
    raw = str(proof.get("status") or "")
    if raw == "passed":
        return {
            "schema": "nebula_latency_result/1",
            "status": "verified",
            "passed": True,
            "reference": "frozen_golden",
            "verified_total_latency_regression_cycles": 0,
            "current_best_total_latency_regression_cycles": int(current_best_total_latency_regression_cycles),
            "iteration_latency_delta_cycles": -int(current_best_total_latency_regression_cycles),
            "verified_shift_cycles": 0,
            "max_latency_regression_cycles": 0,
            "formal_status": "passed",
            "tested_shifts": [0],
        }

    log_text = ""
    log_path = proof.get("log")
    if isinstance(log_path, str):
        try:
            log_text = Path(log_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
    status, reason = _formal_status(proof, log_text)
    # Historical cal.v2.verify writes status=unproven for every non-timeout
    # nonzero exit.  With no decisive counterexample marker, keep it as a tool
    # failure instead of blaming the candidate semantically.
    if raw == "timeout":
        status, reason = "inconclusive_timeout", "formal_timeout_is_not_a_functional_failure"
    return {
        "schema": "nebula_latency_result/1",
        "status": status,
        "passed": False,
        "reference": "frozen_golden",
        "verified_total_latency_regression_cycles": None,
        "current_best_total_latency_regression_cycles": int(current_best_total_latency_regression_cycles),
        "iteration_latency_delta_cycles": None,
        "verified_shift_cycles": None,
        "max_latency_regression_cycles": 0,
        "formal_status": status,
        "tested_shifts": [0],
        "reason": reason,
    }
