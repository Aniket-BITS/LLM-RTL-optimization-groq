#!/usr/bin/env python3
"""
nEBULA equivalence-failure diagnoser.

Purpose
-------
Run after a candidate is rejected by the formal-equivalence gate.
The tool does NOT replace formal verification. It explains likely causes by:

1. Reading the verify JSON/log and classifying the formal outcome.
2. Extracting and grouping unproven $equiv signals/bits.
3. Comparing the golden RTL with the accepted candidate RTL.
4. Detecting pre-existing continuous-assignment drivers that disappeared.
5. Detecting suspicious arithmetic carry-width patterns such as:
       wire [N-1:0] sum = a + b;
       wire carry = sum[N-1];
   where the true carry-out would require an N+1-bit intermediate.
6. Producing concise LLM feedback for the next optimization iteration.

The analysis is conservative:
- "ERROR" findings are deterministic source-level problems.
- "WARNING" findings are strong heuristics that should be reviewed.
- An unproven $equiv cell alone is not treated as a proven counterexample.

Typical use
-----------
python3 timing_automation/diagnose_equivalence.py \
    --tag mapper_grt_unit000_017 \
    --ai-runs-dir ai_runs

Optional explicit paths
-----------------------
python3 timing_automation/diagnose_equivalence.py \
    --verify ai_runs/foo.verify.json \
    --candidate ai_runs/foo.candidate.json \
    --golden designs/src/foo/foo.v \
    --candidate-source ai_runs/foo_sources/.../foo.v \
    --output ai_runs/foo.diagnosis.json
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


try:
    from equivalence_terms import EquivalenceVerdict
except ImportError:
    from timing_automation.equivalence_terms import EquivalenceVerdict

# -----------------------------------------------------------------------------
# Generic helpers
# -----------------------------------------------------------------------------

def load_json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid JSON: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"expected JSON object: {path}")
    return value


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def rel_or_abs(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve()))
    except Exception:
        return str(path.resolve())


def compact_text(text: str, limit: int = 500) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


# -----------------------------------------------------------------------------
# Findings
# -----------------------------------------------------------------------------

@dataclass
class Finding:
    severity: str           # error | warning | info
    category: str
    message: str
    evidence: Dict[str, Any]
    confidence: str = "high"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# -----------------------------------------------------------------------------
# Formal-log analysis
# -----------------------------------------------------------------------------

UNPROVEN_RE = re.compile(
    r"Unproven\s+\$equiv.*?:\s*(?P<lhs>\\?[A-Za-z_$][\w$.\[\]:/-]*)"
    r"(?:\s+(?P<rhs>\\?[A-Za-z_$][\w$.\[\]:/-]*))?",
    re.IGNORECASE,
)

TOTAL_UNPROVEN_RE = re.compile(
    r"Found(?:\s+a\s+total\s+of)?\s+(\d+)\s+unproven\s+\$equiv",
    re.IGNORECASE,
)

BIT_SIGNAL_RE = re.compile(r"^\\?(?P<base>[^\[]+)\[(?P<bit>\d+)\](?:_(?:gold|gate))?$")


def normalize_equiv_signal(name: str) -> str:
    name = name.lstrip("\\")
    name = re.sub(r"_(gold|gate)$", "", name)
    return name


def group_signal_bits(signals: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    groups: Dict[str, List[int]] = defaultdict(list)
    scalars: List[str] = []

    for raw in signals:
        name = normalize_equiv_signal(raw)
        m = BIT_SIGNAL_RE.match(name)
        if m:
            groups[m.group("base")].append(int(m.group("bit")))
        else:
            scalars.append(name)

    result: Dict[str, Dict[str, Any]] = {}
    for base, bits in sorted(groups.items()):
        uniq = sorted(set(bits))
        result[base] = {
            "bits": uniq,
            "min_bit": min(uniq),
            "max_bit": max(uniq),
            "count": len(uniq),
        }

    if scalars:
        result["__scalars__"] = {"signals": sorted(set(scalars)), "count": len(set(scalars))}
    return result


def analyze_formal(verify: Dict[str, Any], log_text: str) -> Tuple[Dict[str, Any], List[Finding]]:
    findings: List[Finding] = []

    status = str(verify.get("status") or verify.get("proof", {}).get("status") or "unknown")
    reason = str(verify.get("reason") or verify.get("proof", {}).get("reason") or "")

    unproven_pairs: List[Tuple[str, Optional[str]]] = []
    for line in log_text.splitlines():
        m = UNPROVEN_RE.search(line)
        if m:
            unproven_pairs.append((m.group("lhs"), m.group("rhs")))

    lhs_signals = [lhs for lhs, _ in unproven_pairs]
    grouped = group_signal_bits(lhs_signals)

    totals = [int(m.group(1)) for m in TOTAL_UNPROVEN_RE.finditer(log_text)]
    final_unproven = totals[-1] if totals else len(unproven_pairs)

    explicit_counterexample = status == EquivalenceVerdict.DISPROVEN.value
    verification = verify.get("verification") if isinstance(verify.get("verification"), dict) else {}
    timeout = bool(verification.get("timed_out")) or "VERIFY_RTL_TIMEOUT" in log_text

    # Canonical verdicts are authoritative. Residual $equiv cells alone never
    # establish inequivalence.
    if status == EquivalenceVerdict.DISPROVEN.value:
        formal_class = "counterexample_reported"
    elif status == EquivalenceVerdict.NOT_PROVEN.value and timeout:
        formal_class = "proof_capacity_limited"
    elif status == EquivalenceVerdict.NOT_PROVEN.value:
        formal_class = "proof_incomplete"
    elif status in {EquivalenceVerdict.PROVEN_COMPLETE.value, EquivalenceVerdict.PROVEN_BOUNDED.value}:
        formal_class = "equivalent"
    else:
        formal_class = status

    if final_unproven > 0:
        findings.append(
            Finding(
                severity="info",
                category="formal_unproven_cells",
                message=f"Formal ended with {final_unproven} unproven $equiv cell(s).",
                evidence={"grouped_signals": grouped},
                confidence="high",
            )
        )

    return {
        "status": status,
        "reason": reason,
        "classification": formal_class,
        "final_unproven_count": final_unproven,
        "grouped_unproven_signals": grouped,
        "explicit_counterexample_reported": explicit_counterexample,
        "timeout": timeout,
    }, findings


# -----------------------------------------------------------------------------
# Candidate/golden path resolution
# -----------------------------------------------------------------------------

def find_candidate_source(candidate_root: Path, module: Optional[str], preferred_name: Optional[str]) -> Path:
    candidates = sorted(
        p for p in candidate_root.rglob("*")
        if p.is_file() and p.suffix.lower() in {".v", ".sv"}
    )
    if not candidates:
        raise RuntimeError(f"no Verilog/SystemVerilog source found under {candidate_root}")

    if preferred_name:
        same_name = [p for p in candidates if p.name == preferred_name]
        if len(same_name) == 1:
            return same_name[0]

    if module:
        module_re = re.compile(rf"\bmodule\s+{re.escape(module)}\b")
        matched = []
        for p in candidates:
            try:
                if module_re.search(p.read_text(encoding="utf-8", errors="replace")):
                    matched.append(p)
            except OSError:
                pass
        if len(matched) == 1:
            return matched[0]
        if matched:
            return matched[0]

    return candidates[0]


def resolve_paths(args: argparse.Namespace) -> Dict[str, Path]:
    ai_runs = Path(args.ai_runs_dir).resolve()
    tag = args.tag

    if tag:
        verify = Path(args.verify).resolve() if args.verify else ai_runs / f"{tag}.verify.json"
        candidate = Path(args.candidate).resolve() if args.candidate else ai_runs / f"{tag}.candidate.json"
        candidate_root = (
            Path(args.candidate_root).resolve()
            if args.candidate_root
            else ai_runs / f"{tag}_sources"
        )
        output = Path(args.output).resolve() if args.output else ai_runs / f"{tag}.diagnosis.json"
    else:
        if not args.verify:
            raise RuntimeError("provide --tag or --verify")
        verify = Path(args.verify).resolve()
        candidate = Path(args.candidate).resolve() if args.candidate else verify.with_name(
            verify.name.replace(".verify.json", ".candidate.json")
        )
        candidate_root = Path(args.candidate_root).resolve() if args.candidate_root else verify.parent
        output = Path(args.output).resolve() if args.output else verify.with_name(
            verify.name.replace(".verify.json", ".diagnosis.json")
        )

    verify_json = load_json(verify)
    candidate_json = load_json(candidate)

    target_file = None
    target_module = None
    patches = candidate_json.get("patches")
    if isinstance(patches, list) and patches:
        first = patches[0] if isinstance(patches[0], dict) else {}
        target_file = first.get("target_file")
        target_module = first.get("target_module")

    golden = Path(args.golden).resolve() if args.golden else None
    if golden is None and isinstance(target_file, str) and target_file:
        golden = Path(target_file).expanduser().resolve()

    if golden is None or not golden.is_file():
        raise RuntimeError(
            "could not resolve golden RTL; pass --golden explicitly "
            "or ensure candidate patches[].target_file points to the golden file"
        )

    candidate_source = Path(args.candidate_source).resolve() if args.candidate_source else None
    if candidate_source is None:
        candidate_source = find_candidate_source(
            candidate_root,
            module=str(target_module) if target_module else None,
            preferred_name=golden.name,
        )

    log_value = (
        verify_json.get("proof", {}).get("log")
        if isinstance(verify_json.get("proof"), dict)
        else None
    )
    verify_log = Path(args.log).resolve() if args.log else None
    if verify_log is None and isinstance(log_value, str) and log_value:
        verify_log = Path(log_value).expanduser().resolve()
    if verify_log is None:
        verify_log = verify.with_suffix(".log")

    return {
        "verify": verify,
        "candidate": candidate,
        "candidate_root": candidate_root,
        "golden": golden,
        "candidate_source": candidate_source,
        "verify_log": verify_log,
        "output": output,
    }


# -----------------------------------------------------------------------------
# Source-diff and driver analysis
# -----------------------------------------------------------------------------

IDENT = r"[A-Za-z_][A-Za-z0-9_$]*"

ASSIGN_RE = re.compile(
    rf"^\s*assign\s+(?P<lhs>{IDENT}(?:\s*\[[^\]]+\])?)\s*=",
    re.MULTILINE,
)

DECL_INIT_RE = re.compile(
    rf"^\s*(?:wire|logic)\b(?:\s+(?:signed|unsigned))?"
    rf"(?:\s*\[[^\]]+\])?\s+(?P<lhs>{IDENT})\s*=",
    re.MULTILINE,
)

PROC_ASSIGN_RE = re.compile(
    rf"(?m)^\s*(?P<lhs>{IDENT}(?:\s*\[[^\]]+\])?)\s*(?:<=|=)\s*"
)


def base_lhs(lhs: str) -> str:
    return re.sub(r"\s*\[.*$", "", lhs.strip())


def collect_drivers(text: str) -> Dict[str, List[Dict[str, Any]]]:
    drivers: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    # Strip comments without shifting diagnostic line numbers. This remains
    # heuristic; compiler driver analysis in yosys_structure is authoritative.
    text = re.sub(r"/\*.*?\*/|//[^\n]*", lambda m: "\n" * m.group().count("\n"), text, flags=re.S)
    lines = text.splitlines()

    for lineno, line in enumerate(lines, start=1):
        for m in re.finditer(rf"\bassign\s+(?P<lhs>{IDENT}(?:\s*\[[^\]]+\])?)\s*=", line):
            name = base_lhs(m.group("lhs"))
            drivers[name].append({"kind": "continuous_assign", "line": lineno, "text": line.strip()})

        m = re.match(
            rf"^\s*(?:wire|logic)\b(?:\s+(?:signed|unsigned))?"
            rf"(?:\s*\[[^\]]+\])?\s+(?P<lhs>{IDENT})\s*=",
            line,
        )
        if m:
            name = m.group("lhs")
            drivers[name].append({"kind": "declaration_init", "line": lineno, "text": line.strip()})
            continue

        # Procedural assignments are intentionally lower confidence because a
        # regex parser cannot fully understand blocks/generate constructs.
        m = re.match(rf"^\s*(?P<lhs>{IDENT}(?:\s*\[[^\]]+\])?)\s*(?:<=|=)\s*", line)
        if m:
            name = base_lhs(m.group("lhs"))
            drivers[name].append({"kind": "procedural_assign", "line": lineno, "text": line.strip()})

    return dict(drivers)


def analyze_lost_drivers(golden_text: str, candidate_text: str) -> List[Finding]:
    findings: List[Finding] = []
    gd = collect_drivers(golden_text)
    cd = collect_drivers(candidate_text)

    for net, golden_drivers in sorted(gd.items()):
        if net not in cd:
            findings.append(
                Finding(
                    severity="error",
                    category="preexisting_driver_removed",
                    message=f"Pre-existing driven signal {net!r} has no detected driver in the candidate.",
                    evidence={
                        "signal": net,
                        "golden_drivers": golden_drivers,
                    },
                    confidence="high" if all(x["kind"] != "procedural_assign" for x in golden_drivers) else "medium",
                )
            )

    return findings


def unified_diff(golden_path: Path, candidate_path: Path, golden_text: str, candidate_text: str) -> str:
    diff = difflib.unified_diff(
        golden_text.splitlines(),
        candidate_text.splitlines(),
        fromfile=str(golden_path),
        tofile=str(candidate_path),
        lineterm="",
    )
    return "\n".join(diff)


# -----------------------------------------------------------------------------
# Arithmetic-width heuristics
# -----------------------------------------------------------------------------

# Handles simple constant ranges such as [7:0], [15:8], [WIDTH:0] is purposely
# not guessed because parameter arithmetic needs elaboration.
DECL_EXPR_RE = re.compile(
    rf"^\s*(?:wire|logic)\s+"
    rf"(?P<signed>signed\s+)?"
    rf"\[(?P<msb>\d+)\s*:\s*(?P<lsb>\d+)\]\s+"
    rf"(?P<name>{IDENT})\s*=\s*(?P<expr>.+?);\s*$"
)

SCALAR_INIT_RE = re.compile(
    rf"^\s*(?:wire|logic)\s+(?P<name>{IDENT})\s*=\s*(?P<expr>.+?);\s*$"
)

BIT_REF_RE = re.compile(rf"\b(?P<name>{IDENT})\s*\[\s*(?P<bit>\d+)\s*\]")


def analyze_arithmetic_width(candidate_text: str) -> List[Finding]:
    findings: List[Finding] = []
    declarations: Dict[str, Dict[str, Any]] = {}
    scalar_exprs: List[Dict[str, Any]] = []

    for lineno, raw in enumerate(candidate_text.splitlines(), start=1):
        line = raw.strip()

        m = DECL_EXPR_RE.match(raw)
        if m:
            msb = int(m.group("msb"))
            lsb = int(m.group("lsb"))
            width = abs(msb - lsb) + 1
            expr = m.group("expr").strip()
            declarations[m.group("name")] = {
                "width": width,
                "msb": msb,
                "lsb": lsb,
                "expr": expr,
                "line": lineno,
                "text": line,
                "is_addition": "+" in expr,
            }
            continue

        m = SCALAR_INIT_RE.match(raw)
        if m:
            scalar_exprs.append({
                "name": m.group("name"),
                "expr": m.group("expr").strip(),
                "line": lineno,
                "text": line,
            })

    # Strong heuristic: an N-bit addition result is later indexed at its MSB
    # and that bit is assigned into a scalar carry-like signal.
    for scalar in scalar_exprs:
        expr = scalar["expr"]
        refs = list(BIT_REF_RE.finditer(expr))
        if len(refs) != 1:
            continue

        ref_name = refs[0].group("name")
        bit = int(refs[0].group("bit"))
        decl = declarations.get(ref_name)
        if not decl or not decl["is_addition"]:
            continue

        top_bit = max(decl["msb"], decl["lsb"])
        if bit != top_bit:
            continue

        carry_like = bool(re.search(r"(?:^|_)(?:c|carry|cout|co)\d*$", scalar["name"], re.IGNORECASE))
        severity = "error" if carry_like else "warning"

        findings.append(
            Finding(
                severity=severity,
                category="possible_truncated_addition_carry",
                message=(
                    f"{scalar['name']!r} is derived from bit {bit} of {ref_name!r}, "
                    f"but {ref_name!r} is only {decl['width']} bits wide and is produced by an addition. "
                    "If this bit is intended as carry-out, the addition is truncated: "
                    "an N-bit block requires an N+1-bit intermediate to preserve carry-out."
                ),
                evidence={
                    "carry_signal": scalar["name"],
                    "carry_line": scalar["line"],
                    "carry_expression": scalar["expr"],
                    "sum_signal": ref_name,
                    "sum_width": decl["width"],
                    "sum_line": decl["line"],
                    "sum_expression": decl["expr"],
                },
                confidence="high" if carry_like else "medium",
            )
        )

    return findings


# -----------------------------------------------------------------------------
# Patch analysis
# -----------------------------------------------------------------------------

def analyze_candidate_patches(candidate: Dict[str, Any], golden_text: str) -> Tuple[List[Dict[str, Any]], List[Finding]]:
    patch_summaries: List[Dict[str, Any]] = []
    findings: List[Finding] = []
    golden_lines = golden_text.splitlines()

    patches = candidate.get("patches")
    if not isinstance(patches, list):
        return patch_summaries, findings

    for index, patch in enumerate(patches):
        if not isinstance(patch, dict):
            continue

        rng = patch.get("changed_source_range")
        replacement = patch.get("replacement_text")
        if not isinstance(rng, dict) or not isinstance(replacement, str):
            continue

        try:
            start = int(rng["start_line"])
            end = int(rng["end_line"])
        except Exception:
            continue

        original_lines = golden_lines[max(0, start - 1):max(0, end)]
        original_text = "\n".join(original_lines)

        original_drivers = collect_drivers(original_text)
        replacement_drivers = collect_drivers(replacement)

        disappeared_inside_patch = sorted(set(original_drivers) - set(replacement_drivers))
        patch_summary = {
            "patch_index": index,
            "patch_id": patch.get("patch_id"),
            "range": [start, end],
            "original_text": original_text,
            "replacement_text": replacement,
            "original_driven_signals": sorted(original_drivers),
            "replacement_driven_signals": sorted(replacement_drivers),
            "drivers_removed_within_patch_text": disappeared_inside_patch,
        }
        patch_summaries.append(patch_summary)

        if disappeared_inside_patch:
            findings.append(
                Finding(
                    severity="warning",
                    category="patch_removed_original_driver",
                    message=(
                        f"Patch {patch.get('patch_id', index)!r} replaces source range {start}-{end} "
                        f"and does not recreate driver(s): {', '.join(disappeared_inside_patch)}."
                    ),
                    evidence={
                        "patch_index": index,
                        "patch_id": patch.get("patch_id"),
                        "range": [start, end],
                        "signals": disappeared_inside_patch,
                    },
                    confidence="high",
                )
            )

    return patch_summaries, findings


# -----------------------------------------------------------------------------
# Correlation / explanation
# -----------------------------------------------------------------------------

def correlate(findings: Sequence[Finding], formal: Mapping[str, Any]) -> List[Finding]:
    extra: List[Finding] = []
    unproven = formal.get("grouped_unproven_signals")
    if not isinstance(unproven, dict):
        return extra

    lost = {
        str(f.evidence.get("signal"))
        for f in findings
        if f.category == "preexisting_driver_removed"
    }

    arithmetic_sums = {
        str(f.evidence.get("sum_signal"))
        for f in findings
        if f.category == "possible_truncated_addition_carry"
    }

    unproven_bases = set(unproven)

    overlap_lost = sorted(lost & unproven_bases)
    if overlap_lost:
        extra.append(
            Finding(
                severity="error",
                category="formal_static_correlation",
                message=(
                    "Formal unproven signals overlap with signals whose pre-existing drivers disappeared: "
                    + ", ".join(overlap_lost)
                ),
                evidence={"signals": overlap_lost},
                confidence="high",
            )
        )

    # Arithmetic sums often feed a differently named output/result, so direct
    # overlap is not required. Still report if the sum itself is unproven.
    overlap_arith = sorted(arithmetic_sums & unproven_bases)
    if overlap_arith:
        extra.append(
            Finding(
                severity="error",
                category="formal_arithmetic_correlation",
                message=(
                    "Formal could not prove arithmetic signal(s) that also show a carry-width hazard: "
                    + ", ".join(overlap_arith)
                ),
                evidence={"signals": overlap_arith},
                confidence="high",
            )
        )

    return extra


# -----------------------------------------------------------------------------
# LLM feedback
# -----------------------------------------------------------------------------

def build_feedback(findings: Sequence[Finding], formal: Mapping[str, Any]) -> str:
    decisive = [f for f in findings if f.severity == "error"]
    warnings = [f for f in findings if f.severity == "warning"]

    parts = [
        f"Previous candidate was rejected by the equivalence gate; formal classification="
        f"{formal.get('classification')}, unproven_equiv_cells={formal.get('final_unproven_count')}."
    ]

    for f in decisive[:5]:
        parts.append(f"ERROR: {f.message}")

    if not decisive:
        for f in warnings[:4]:
            parts.append(f"WARNING: {f.message}")

    parts.append(
        "Do not repeat these faults. Preserve every pre-existing non-target behavior, "
        "and preserve full arithmetic intermediate width whenever downstream carry/result bits depend on it."
    )
    return " ".join(parts)


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Diagnose nEBULA candidates rejected by the formal-equivalence gate."
    )
    ap.add_argument("--tag", help="Optimizer tag, e.g. mapper_grt_unit000_017")
    ap.add_argument("--ai-runs-dir", default="ai_runs")
    ap.add_argument("--verify", help="Explicit verify JSON")
    ap.add_argument("--log", help="Explicit verify log")
    ap.add_argument("--candidate", help="Explicit candidate JSON")
    ap.add_argument("--golden", help="Explicit golden RTL source")
    ap.add_argument("--candidate-root", help="Accepted candidate source bundle root")
    ap.add_argument("--candidate-source", help="Explicit candidate RTL source")
    ap.add_argument("--output", help="Diagnosis JSON output")
    ap.add_argument(
        "--fail-on-diagnosed-error",
        action="store_true",
        help="Return exit code 1 when one or more deterministic/high-confidence errors are found.",
    )
    return ap.parse_args()


def main() -> int:
    args = parse_args()

    try:
        paths = resolve_paths(args)
        verify = load_json(paths["verify"])
        candidate = load_json(paths["candidate"])

        log_text = ""
        if paths["verify_log"].is_file():
            log_text = paths["verify_log"].read_text(encoding="utf-8", errors="replace")

        golden_text = paths["golden"].read_text(encoding="utf-8", errors="replace")
        candidate_text = paths["candidate_source"].read_text(encoding="utf-8", errors="replace")

        findings: List[Finding] = []

        formal, formal_findings = analyze_formal(verify, log_text)
        findings.extend(formal_findings)

        patch_summaries, patch_findings = analyze_candidate_patches(candidate, golden_text)
        findings.extend(patch_findings)

        findings.extend(analyze_lost_drivers(golden_text, candidate_text))
        findings.extend(analyze_arithmetic_width(candidate_text))
        findings.extend(correlate(findings, formal))

        diff_text = unified_diff(
            paths["golden"],
            paths["candidate_source"],
            golden_text,
            candidate_text,
        )

        error_count = sum(f.severity == "error" for f in findings)
        warning_count = sum(f.severity == "warning" for f in findings)

        if error_count:
            diagnosis = "likely_functional_mismatch_identified"
        elif warning_count:
            diagnosis = "suspicious_source_changes_identified"
        elif formal.get("final_unproven_count", 0):
            diagnosis = "formal_failure_not_localized_by_static_checks"
        else:
            diagnosis = "no_failure_detected"

        report = {
            "schema": "equivalence_diagnosis/1",
            "tag": args.tag,
            "diagnosis": diagnosis,
            "formal": formal,
            "paths": {k: rel_or_abs(v) for k, v in paths.items()},
            "candidate": {
                "chosen_pattern_id": candidate.get("chosen_pattern_id"),
                "target_unit_id": candidate.get("target_unit_id"),
                "patches": patch_summaries,
            },
            "summary": {
                "error_count": error_count,
                "warning_count": warning_count,
                "finding_count": len(findings),
            },
            "findings": [f.to_dict() for f in findings],
            "llm_feedback": build_feedback(findings, formal),
            "rtl_diff": diff_text,
        }

        write_json(paths["output"], report)

        print("=" * 76)
        print("nEBULA EQUIVALENCE FAILURE DIAGNOSIS")
        print("=" * 76)
        print(f"Diagnosis      : {diagnosis}")
        print(f"Formal class   : {formal.get('classification')}")
        print(f"Unproven cells : {formal.get('final_unproven_count')}")
        print(f"Errors         : {error_count}")
        print(f"Warnings       : {warning_count}")
        print(f"Report         : {paths['output']}")
        print("-" * 76)

        for f in findings:
            if f.severity not in {"error", "warning"}:
                continue
            print(f"[{f.severity.upper()}] {f.category}")
            print(f"  {f.message}")

        print("-" * 76)
        print("LLM FEEDBACK")
        print(compact_text(report["llm_feedback"], 1200))
        print("=" * 76)

        if args.fail_on_diagnosed_error and error_count:
            return 1
        return 0

    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
