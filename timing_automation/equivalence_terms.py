#!/usr/bin/env python3
"""Canonical equivalence vocabulary for nEBULA.

This module deliberately separates proof completion, proof incompleteness,
actual disproof, unsupported verification, and tool failure.  Callers should
never infer functional inequivalence merely from a proof that did not close.
"""
from __future__ import annotations

import argparse
from enum import Enum


class EquivalenceVerdict(str, Enum):
    PROVEN_COMPLETE = "proven_complete"
    PROVEN_BOUNDED = "proven_bounded"
    NOT_PROVEN = "not_proven"
    DISPROVEN = "disproven"
    NOT_ATTEMPTED = "not_attempted"
    TOOL_ERROR = "tool_error"


PROVEN_VERDICTS = {
    EquivalenceVerdict.PROVEN_COMPLETE,
    EquivalenceVerdict.PROVEN_BOUNDED,
}


def coerce(value: str | EquivalenceVerdict) -> EquivalenceVerdict:
    if isinstance(value, EquivalenceVerdict):
        return value
    return EquivalenceVerdict(str(value))


def is_proven(value: str | EquivalenceVerdict) -> bool:
    try:
        return coerce(value) in PROVEN_VERDICTS
    except ValueError:
        return False


def describe(
    verdict: str | EquivalenceVerdict,
    *,
    method: str | None = None,
    depth: int | None = None,
) -> str:
    verdict = coerce(verdict)
    if verdict is EquivalenceVerdict.PROVEN_COMPLETE:
        suffix = f" ({method})" if method else " (combinational CEC)"
        return "Equivalent — proven exhaustively" + suffix
    if verdict is EquivalenceVerdict.PROVEN_BOUNDED:
        if depth is not None:
            return (
                "Equivalent — proven by sequential induction to depth "
                f"{depth} (method-specific proof strength)"
            )
        return "Equivalent — proven by sequential induction (method-specific proof strength)"
    if verdict is EquivalenceVerdict.NOT_PROVEN:
        return "Not proven — the proof did not close; this is not a disproof"
    if verdict is EquivalenceVerdict.DISPROVEN:
        return "Not equivalent — counterexample found"
    if verdict is EquivalenceVerdict.NOT_ATTEMPTED:
        return "Not checked — no equivalence claim is made"
    return "Unknown — verification tool failed; no equivalence claim is made"


def exit_code_for(verdict: str | EquivalenceVerdict) -> int:
    verdict = coerce(verdict)
    if verdict in PROVEN_VERDICTS:
        return 0
    if verdict is EquivalenceVerdict.NOT_PROVEN:
        return 1
    if verdict is EquivalenceVerdict.DISPROVEN:
        return 3
    return 2


def run_self_test() -> None:
    assert is_proven(EquivalenceVerdict.PROVEN_COMPLETE)
    assert is_proven("proven_bounded")
    assert not is_proven("not_proven")
    assert exit_code_for("not_proven") == 1
    assert exit_code_for("disproven") == 3
    assert exit_code_for("not_attempted") == 2
    assert "not a disproof" in describe("not_proven")
    assert "counterexample" in describe("disproven").lower()
    assert "depth 8" in describe("proven_bounded", depth=8)
    print("equivalence_terms.py self-test: PASS")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        run_self_test()
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
