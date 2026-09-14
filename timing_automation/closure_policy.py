#!/usr/bin/env python3
"""Pure, deterministic stop policy for nEBULA closure orchestration."""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence


@dataclass(frozen=True)
class ClosurePolicy:
    target_wns_ns: float = 0.0
    min_gain_ns: float = 0.005
    noise_floor_wns_ns: float = 0.0
    max_flows: int = 7
    patience: int = 1

    def validate(self) -> None:
        for name, value in (
            ("target_wns_ns", self.target_wns_ns),
            ("min_gain_ns", self.min_gain_ns),
            ("noise_floor_wns_ns", self.noise_floor_wns_ns),
        ):
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)):
                raise ValueError(f"{name} must be finite")
        if self.min_gain_ns < 0 or self.noise_floor_wns_ns < 0:
            raise ValueError("gain/noise thresholds must be non-negative")
        if self.max_flows < 1:
            raise ValueError("max_flows must be >= 1")
        if self.patience < 1:
            raise ValueError("patience must be >= 1")


@dataclass(frozen=True)
class StopDecision:
    stop: bool
    reason: str
    detail: str
    latest_wns_ns: Optional[float] = None
    effective_min_gain_ns: Optional[float] = None
    flows_used: int = 0
    flows_remaining: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "stop": self.stop,
            "reason": self.reason,
            "detail": self.detail,
            "latest_wns_ns": self.latest_wns_ns,
            "effective_min_gain_ns": self.effective_min_gain_ns,
            "flows_used": self.flows_used,
            "flows_remaining": self.flows_remaining,
        }


def _wns(metrics: Mapping[str, Any]) -> float:
    value = metrics.get("wns_ns")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError("metrics.wns_ns must be finite")
    return float(value)


def evaluate_stop(
    baseline: Mapping[str, Any],
    promoted_candidates: Sequence[Mapping[str, Any]],
    policy: ClosurePolicy,
    *,
    flows_used: int,
    has_requestable_problem: bool = True,
) -> StopDecision:
    """Evaluate closure stopping without I/O or subprocesses.

    ``flows_used`` counts actual ORFS physical runs, not LLM attempts. A lint or
    formal rejection therefore does not consume the flow budget.
    """
    policy.validate()
    if flows_used < 0:
        raise ValueError("flows_used must be >= 0")
    remaining = max(0, policy.max_flows - flows_used)
    effective_gain = max(float(policy.min_gain_ns), float(policy.noise_floor_wns_ns))

    latest = _wns(promoted_candidates[-1]) if promoted_candidates else _wns(baseline)

    if latest >= policy.target_wns_ns:
        return StopDecision(True, "target_met", f"WNS {latest:.6f} >= target {policy.target_wns_ns:.6f}", latest, effective_gain, flows_used, remaining)
    if not has_requestable_problem:
        return StopDecision(True, "no_requestable_problem", "no hard-valid timing problem remains", latest, effective_gain, flows_used, remaining)
    if flows_used >= policy.max_flows:
        return StopDecision(True, "flow_budget_exhausted", f"physical-flow budget {policy.max_flows} exhausted", latest, effective_gain, flows_used, remaining)

    if promoted_candidates:
        series = [_wns(baseline)] + [_wns(c) for c in promoted_candidates]
        gains = [series[i] - series[i - 1] for i in range(1, len(series))]
        if len(gains) >= policy.patience:
            recent = gains[-policy.patience:]
            # Equal-to-threshold is meaningful progress; only strictly smaller
            # gains are diminishing returns.
            if all(gain < effective_gain for gain in recent):
                return StopDecision(
                    True,
                    "diminishing_returns",
                    f"last {policy.patience} promoted gain(s) {recent} are below effective threshold {effective_gain:.6f} ns",
                    latest,
                    effective_gain,
                    flows_used,
                    remaining,
                )

    return StopDecision(False, "continue", "closure policy permits another attempt", latest, effective_gain, flows_used, remaining)


def run_self_test() -> None:
    base = {"wns_ns": -0.20}
    p = ClosurePolicy(target_wns_ns=0.0, min_gain_ns=0.005, noise_floor_wns_ns=0.002, max_flows=5, patience=1)
    assert evaluate_stop(base, [{"wns_ns": 0.0}], p, flows_used=2).reason == "target_met"
    assert evaluate_stop(base, [], p, flows_used=1, has_requestable_problem=False).reason == "no_requestable_problem"
    assert evaluate_stop(base, [], p, flows_used=5).reason == "flow_budget_exhausted"
    assert evaluate_stop(base, [{"wns_ns": -0.197}], p, flows_used=2).reason == "diminishing_returns"
    assert evaluate_stop(base, [{"wns_ns": -0.195}], p, flows_used=2).reason == "continue"  # exactly 0.005
    noisy = ClosurePolicy(min_gain_ns=0.001, noise_floor_wns_ns=0.004, max_flows=5)
    d = evaluate_stop(base, [{"wns_ns": -0.197}], noisy, flows_used=2)
    assert d.reason == "diminishing_returns" and d.effective_min_gain_ns == 0.004
    print("PASS: closure_policy self-test")
    print("PASS: flow budget is independent of attempt count")
    print("PASS: diminishing returns uses max(min_gain, measured WNS noise)")


def main() -> int:
    p = argparse.ArgumentParser(description="Pure nEBULA closure stop-policy evaluator.")
    p.add_argument("--self-test", action="store_true")
    args = p.parse_args()
    if args.self_test:
        try:
            run_self_test()
            return 0
        except Exception as exc:
            print(f"SELF-TEST FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
    p.error("only --self-test is exposed directly; the closure driver imports evaluate_stop()")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
