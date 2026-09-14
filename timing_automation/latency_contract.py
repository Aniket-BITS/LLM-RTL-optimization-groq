#!/usr/bin/env python3
"""Fixed-cycle latency contract construction for nEBULA RTL optimization.

Phase-1 scope is deliberately narrow and auditable:
  * one existing reference clock,
  * unchanged top-level interface,
  * fixed non-negative integer output shift relative to frozen Golden,
  * optional known reset driven identically into Golden and Candidate,
  * no CDC / variable-latency / transaction protocol semantics.

The LLM never defines this contract.  It is derived from backend design metadata.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import re
from typing import Any, Mapping

try:
    from design_manifest import DesignManifest
except ImportError:  # pragma: no cover
    from timing_automation.design_manifest import DesignManifest


RESET_NAMES = ("rst_n", "reset", "rst", "clear")


@dataclass(frozen=True)
class LatencyContract:
    schema: str = "nebula_latency_contract/2"
    supported: bool = False
    reason: str | None = None
    verification_mode: str = "fixed_cycle_shift"
    reference_clock: str | None = None
    clock_edge: str = "posedge"
    reset_port: str | None = None
    reset_active_level: int | None = None
    reset_asynchronous: bool | None = None
    initial_reset_cycles: int = 1
    golden_latency_regression_reference_cycles: int = 0
    max_latency_regression_cycles: int = 0
    diagnostic_extra_cycles: int = 1
    one_design_level_shift: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _source_text(manifest: DesignManifest, base_dir: Path) -> str:
    parts: list[str] = []
    for path in manifest.resolved_source_files(base_dir):
        try:
            parts.append(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            pass
    return "\n".join(parts)




def _port_width(port: Mapping[str, Any]) -> int | None:
    bits = port.get("bits") if isinstance(port, Mapping) else None
    return len(bits) if isinstance(bits, list) and bits else None


def _infer_clock_edge(manifest: DesignManifest, base_dir: Path, clock_port: str) -> tuple[str | None, str | None]:
    """Return (edge, error). Phase-1 supports one unambiguous edge only."""
    text = _source_text(manifest, base_dir)
    pattern = re.compile(
        r"\balways(?:_ff)?\s*@\s*\([^)]*\b(posedge|negedge)\s+"
        + re.escape(clock_port) + r"\b",
        flags=re.I | re.S,
    )
    edges = {m.group(1).lower() for m in pattern.finditer(text)}
    if len(edges) == 1:
        return next(iter(edges)), None
    if not edges:
        return None, f"could not infer an active edge for reference clock {clock_port!r}"
    return None, f"reference clock {clock_port!r} is used on multiple active edges"


def _infer_reset(
    manifest: DesignManifest,
    base_dir: Path,
    ports: Mapping[str, Any],
) -> tuple[str | None, int | None, bool | None]:
    # Canonical manifest metadata wins when exactly one reset is defined.
    if len(manifest.resets) == 1:
        reset = manifest.resets[0]
        if reset.port in ports:
            return reset.port, int(reset.active_level), bool(reset.asynchronous)
    if len(manifest.resets) > 1:
        return None, None, None

    candidates = [name for name in RESET_NAMES if name in ports]
    if len(candidates) != 1:
        return None, None, None
    name = candidates[0]
    active = 0 if name.endswith("_n") else 1
    text = _source_text(manifest, base_dir)
    async_re = re.compile(
        r"always\s*@\s*\([^)]*(?:posedge|negedge)\s+" + re.escape(name) + r"\b",
        flags=re.I | re.S,
    )
    return name, active, bool(async_re.search(text))


def build_latency_contract(
    *,
    pair: Mapping[str, Any],
    manifest: DesignManifest,
    base_dir: Path,
    elaborated_module: Mapping[str, Any],
    max_latency_regression_cycles: int,
) -> LatencyContract:
    """Derive the trusted phase-1 temporal contract from backend metadata."""
    if isinstance(max_latency_regression_cycles, bool) or not isinstance(max_latency_regression_cycles, int):
        raise ValueError("max_latency_regression_cycles must be an integer")
    if max_latency_regression_cycles < 0:
        raise ValueError("max_latency_regression_cycles must be nonnegative")

    ports = elaborated_module.get("ports") if isinstance(elaborated_module, Mapping) else None
    if not isinstance(ports, Mapping) or not ports:
        return LatencyContract(
            supported=False,
            reason="elaborated top-module ports unavailable",
            max_latency_regression_cycles=max_latency_regression_cycles,
        )

    # Budget zero deliberately preserves the already-validated cycle-exact path,
    # including combinational designs that have no clock at all.
    if max_latency_regression_cycles == 0:
        profile = pair.get("timing_profile") if isinstance(pair.get("timing_profile"), Mapping) else {}
        clock = manifest.clocks[0].port if len(manifest.clocks) == 1 and not manifest.clocks[0].virtual else profile.get("clock_port")
        return LatencyContract(
            supported=True, reason=None, verification_mode="cycle_exact_existing",
            reference_clock=clock if isinstance(clock, str) and clock else None,
            max_latency_regression_cycles=0, diagnostic_extra_cycles=0,
        )

    # Prefer canonical manifest clocks.  RTL-OPT currently carries the reference
    # clock explicitly in timing_profile, so use that as the dataset adapter fallback.
    clock_port: str | None = None
    if len(manifest.clocks) == 1 and not manifest.clocks[0].virtual:
        clock_port = manifest.clocks[0].port
    elif len(manifest.clocks) > 1:
        return LatencyContract(
            supported=False,
            reason="phase-1 latency verification supports exactly one reference clock",
            max_latency_regression_cycles=max_latency_regression_cycles,
        )
    else:
        profile = pair.get("timing_profile") if isinstance(pair.get("timing_profile"), Mapping) else {}
        raw = profile.get("clock_port")
        if isinstance(raw, str) and raw:
            clock_port = raw

    if not clock_port:
        return LatencyContract(
            supported=False,
            reason="design has no backend-declared reference clock; pipelining would require a new clock contract",
            max_latency_regression_cycles=max_latency_regression_cycles,
        )
    if clock_port not in ports or ports[clock_port].get("direction") != "input":
        return LatencyContract(
            supported=False,
            reason=f"reference clock {clock_port!r} is not a top-level input",
            reference_clock=clock_port,
            max_latency_regression_cycles=max_latency_regression_cycles,
        )

    if _port_width(ports[clock_port]) != 1:
        return LatencyContract(
            supported=False,
            reason=f"reference clock {clock_port!r} must be one bit",
            reference_clock=clock_port,
            max_latency_regression_cycles=max_latency_regression_cycles,
        )

    clock_edge, clock_edge_error = _infer_clock_edge(manifest, base_dir, clock_port)
    if clock_edge_error is not None:
        return LatencyContract(
            supported=False,
            reason=clock_edge_error,
            reference_clock=clock_port,
            max_latency_regression_cycles=max_latency_regression_cycles,
        )

    reset_port, reset_active_level, reset_async = _infer_reset(manifest, base_dir, ports)

    # cal.v2.elaborate already rejects generated/multiple clock domains for this
    # RTL-OPT runner. Keep an explicit guard here because latency verification is
    # a stronger claim than ordinary same-cycle SEC. A known reset edge is not a
    # clock domain and is excluded from this check.
    edge_signals = set(pair.get("baseline", {}).get("edge_signals") or [])
    reset_edges = set(RESET_NAMES)
    if reset_port:
        reset_edges.add(reset_port)
    non_reset_edges = {x for x in edge_signals if x not in reset_edges}
    if non_reset_edges and non_reset_edges != {clock_port}:
        return LatencyContract(
            supported=False,
            reason="phase-1 latency verification does not support multiple/generated clock edges",
            reference_clock=clock_port,
            clock_edge=clock_edge or "posedge",
            max_latency_regression_cycles=max_latency_regression_cycles,
        )

    if reset_port is not None and _port_width(ports[reset_port]) != 1:
        return LatencyContract(
            supported=False,
            reason=f"reset {reset_port!r} must be one bit for phase-1 latency verification",
            reference_clock=clock_port,
            clock_edge=clock_edge or "posedge",
            max_latency_regression_cycles=max_latency_regression_cycles,
        )
    # Multiple reset-like ports are intentionally unsupported rather than guessed.
    reset_like = [name for name in RESET_NAMES if name in ports]
    if not manifest.resets and len(reset_like) > 1:
        return LatencyContract(
            supported=False,
            reason="multiple reset-like top-level inputs require an explicit manifest reset contract",
            reference_clock=clock_port,
            max_latency_regression_cycles=max_latency_regression_cycles,
        )

    return LatencyContract(
        supported=True,
        reason=None,
        reference_clock=clock_port,
        clock_edge=clock_edge or "posedge",
        reset_port=reset_port,
        reset_active_level=reset_active_level,
        reset_asynchronous=reset_async,
        initial_reset_cycles=1,
        golden_latency_regression_reference_cycles=0,
        max_latency_regression_cycles=max_latency_regression_cycles,
        diagnostic_extra_cycles=1,
    )


def latency_policy_for_prompt(
    contract: LatencyContract,
    *,
    current_best_total_latency_regression_cycles: int,
) -> dict[str, Any]:
    current = int(current_best_total_latency_regression_cycles)
    maximum = int(contract.max_latency_regression_cycles)
    return {
        "reference": "frozen_golden",
        "verification_mode": contract.verification_mode,
        "supported": bool(contract.supported),
        "unsupported_reason": contract.reason,
        "reference_clock": contract.reference_clock,
        "max_latency_regression_cycles": maximum,
        "effective_max_latency_regression_cycles": maximum if contract.supported else 0,
        "current_best_total_latency_regression_cycles": current,
        "remaining_latency_headroom_cycles": max(0, maximum - current) if contract.supported else 0,
        "model_prediction_authority": "advisory_only",
        "backend_verification_authority": True,
        "phase1_scope": "single-clock fixed nonnegative design-level output shift",
    }
