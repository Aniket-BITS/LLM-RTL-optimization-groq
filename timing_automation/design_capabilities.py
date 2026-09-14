#!/usr/bin/env python3
"""Conservative preflight capability summary for nEBULA designs.

The result is advisory for orchestration and prompt construction.  It never
replaces lint/formal verification.  The aim is to avoid spending model/formal
budget on transformations that are impossible under the currently supported
contract and to make scalability limitations explicit before attempt 1.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Mapping

try:
    from design_manifest import DesignManifest
except ImportError:  # pragma: no cover
    from timing_automation.design_manifest import DesignManifest

SCHEMA = "design_capabilities/1"
RESET_NAMES = {"reset", "rst", "rst_n", "clear"}


def _read_sources(manifest: DesignManifest, base_dir: Path) -> str:
    return "\n".join(
        p.read_text(encoding="utf-8", errors="replace")
        for p in manifest.resolved_source_files(base_dir)
        if p.is_file()
    )


def _state_bits(module: Mapping[str, Any]) -> int:
    total = 0
    cells = module.get("cells") if isinstance(module, Mapping) else None
    if not isinstance(cells, Mapping):
        return 0
    for cell in cells.values():
        if not isinstance(cell, Mapping):
            continue
        ctype = str(cell.get("type") or "").lower()
        if "dff" not in ctype and "latch" not in ctype:
            continue
        params = cell.get("parameters") if isinstance(cell.get("parameters"), Mapping) else {}
        width = params.get("WIDTH", 1)
        try:
            if isinstance(width, str) and set(width) <= {"0", "1"}:
                value = int(width, 2)
            else:
                value = int(width)
        except Exception:
            value = 1
        total += max(1, value)
    return total


def analyze_design_capabilities(
    *,
    pair: Mapping[str, Any],
    manifest: DesignManifest,
    base_dir: Path,
    elaborated_module: Mapping[str, Any],
    latency_contract: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    text = _read_sources(manifest, Path(base_dir))
    signed_ports = bool(re.search(r"\b(?:input|output|inout)\s+(?:wire\s+|reg\s+|logic\s+)?signed\b", text))
    memory_like = bool(re.search(r"\b(?:reg|logic)\s*(?:signed\s*)?(?:\[[^\]]+\]\s*)?[A-Za-z_$][\w$]*\s*\[[^\]]+\]", text))
    state_bits = _state_bits(elaborated_module)
    edges = set(pair.get("baseline", {}).get("edge_signals") or [])
    non_reset_edges = sorted(x for x in edges if str(x).lower() not in RESET_NAMES)
    if manifest.clocks:
        declared = [c.port for c in manifest.clocks if not c.virtual]
        clock_ports = sorted(set(declared))
    else:
        profile = pair.get("timing_profile") if isinstance(pair.get("timing_profile"), Mapping) else {}
        clock = profile.get("clock_port")
        clock_ports = [clock] if isinstance(clock, str) and clock else non_reset_edges
    clock_ports = sorted(set(str(x) for x in clock_ports if x))

    complexity = "low"
    if memory_like or state_bits >= 512:
        complexity = "high"
    elif state_bits >= 128:
        complexity = "medium"

    contract = dict(latency_contract or {})
    latency_supported = bool(contract.get("supported")) if contract else len(clock_ports) == 1
    latency_reason = contract.get("reason") if contract else (
        None if latency_supported else "latency-changing optimization requires exactly one supported reference clock"
    )
    guided_risk = "memory_or_register_array_endpoints" if memory_like else None

    return {
        "schema": SCHEMA,
        "design": pair.get("id") or manifest.design_id,
        "top": manifest.top,
        "clock_ports": clock_ports,
        "clock_count": len(clock_ports),
        "has_reset": bool(manifest.resets) or bool(re.search(r"\b(?:reset|rst|rst_n|clear)\b", text, flags=re.I)),
        "signed_top_ports_detected": signed_ports,
        "contains_memory_or_register_array": memory_like,
        "estimated_sequential_state_bits": state_bits,
        "expected_formal_complexity": complexity,
        "guided_mapping_risk": guided_risk,
        "latency_change_supported": latency_supported,
        "latency_change_unsupported_reason": latency_reason,
        "recommended_optimization_mode": "cycle_exact_only" if not latency_supported else "latency_aware_allowed",
        "authority": "advisory_preflight_only_backend_verification_remains_authoritative",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--manifest", type=Path, required=True)
    ap.add_argument("--base-dir", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()
    manifest = DesignManifest.read_json(args.manifest)
    # Standalone CLI intentionally emits source-level capability information;
    # orchestrators may enrich it with elaborated state estimates.
    result = analyze_design_capabilities(
        pair={"id": manifest.design_id, "baseline": {"edge_signals": []}},
        manifest=manifest,
        base_dir=args.base_dir,
        elaborated_module={},
        latency_contract=None,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
