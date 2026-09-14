#!/usr/bin/env python3
"""
build_optimizer_input.py v2

Final trusted boundary between timing/RTL analysis and optimization.

Responsibilities
----------------
1. Discover ALL artifacts for one FLOW_VARIANT.
2. Auto-classify mapped path groups that are missing classification.
3. Verify run identity and artifact completeness.
4. Merge all classified path groups into globally unique path IDs.
5. Validate timing arithmetic and global WNS/TNS consistency.
6. Resolve clock periods from authoritative metrics and/or SDC constraints.
7. Resolve RTL source file, module, target lines, source context, and SHA-256.
8. Guard against source files modified after analysis artifacts were produced.
9. Cross-check classifier operator counts against RTL mapping operator counts.
10. Group bit-level timing paths into conservative optimization regions.
11. Attach detailed timing/physical evidence and legal transformation families.
12. Emit an explicit validation/readiness gate.

The optimizer must act ONLY when:
    validation.optimizer_ready == true
and only on regions where:
    eligibility.optimization_ready == true

This script intentionally does NOT generate or modify RTL. Candidate-specific
equivalence checking and PPA re-measurement happen after optimization because
those facts do not exist until a candidate RTL version exists.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


SCHEMA_VERSION = "2.3.0"
CONTEXT_ARCHITECTURE_VERSION = "1.0"
DESIGN_CONTEXT_SCHEMA = "optimizer_design_context/1"
METRIC_DECIMALS = 6
NS_TOLERANCE_DEFAULT = 0.005
SOURCE_MTIME_TOLERANCE_SEC = 1.0

# Prompt/context management defaults.  These are policy values only; the
# final rendered prompt is intentionally constructed immediately before an
# LLM call by the downstream renderer/orchestrator.
DEFAULT_SERVICE_MODEL_TOKEN_BUDGET = 8000
DEFAULT_SOFT_INPUT_TOKEN_TARGET = 5000
DEFAULT_HARD_INPUT_TOKEN_LIMIT = 6000
DEFAULT_COMPLETION_RESERVE_TOKENS = 2000
DEFAULT_ESTIMATED_CHARS_PER_TOKEN = 4.0
CONFIDENCE_ORDER = {
    "unknown": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
}


# ---------------------------------------------------------------------------
# Basic helpers
# ---------------------------------------------------------------------------

def fatal(msg: str) -> None:
    raise RuntimeError(msg)


def load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        fatal(f"JSON file not found: {path}")
    try:
        with path.open("r", encoding="utf-8") as f:
            obj = json.load(f)
    except json.JSONDecodeError as e:
        fatal(f"Invalid JSON in {path}: {e}")
    if not isinstance(obj, dict):
        fatal(f"Expected top-level JSON object in {path}")
    return obj


def write_json(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, sort_keys=False)
        f.write("\n")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def as_float(v: Any) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        try:
            x = float(v)
            return x if math.isfinite(x) else None
        except Exception:
            return None
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        m = re.search(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?", s)
        if m:
            try:
                x = float(m.group(0))
                return x if math.isfinite(x) else None
            except ValueError:
                pass
    return None


def normalize_metric(v: Any) -> Optional[float]:
    """Normalize timing/PPA numeric values to the canonical six decimals."""
    x = as_float(v)
    if x is None:
        return None
    x = round(x, METRIC_DECIMALS)
    return 0.0 if x == 0 else x


def normalize_text(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


def normalize_design(v: Any) -> Optional[str]:
    if isinstance(v, dict):
        for k in ("name", "design", "design_name"):
            if k in v:
                return normalize_text(v[k])
        return None
    return normalize_text(v)


def normalize_platform(v: Any) -> Optional[str]:
    if isinstance(v, dict):
        for k in ("name", "platform", "platform_name"):
            if k in v:
                return normalize_text(v[k])
        return None
    return normalize_text(v)


def normalize_flow_variant(v: Any) -> Optional[str]:
    if isinstance(v, dict):
        for k in ("flow_variant", "variant", "name"):
            if k in v:
                return normalize_text(v[k])
        return None
    return normalize_text(v)


def identity_of(obj: Dict[str, Any]) -> Dict[str, Optional[str]]:
    return {
        "design": normalize_design(obj.get("design")),
        "platform": normalize_platform(obj.get("platform")),
        "flow_variant": normalize_flow_variant(obj.get("flow_variant")),
    }


def recursive_items(obj: Any, path: Tuple[str, ...] = ()) -> Iterable[Tuple[Tuple[str, ...], Any]]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = path + (str(k),)
            yield p, v
            yield from recursive_items(v, p)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            p = path + (str(i),)
            yield p, v
            yield from recursive_items(v, p)


def deep_find_numeric(obj: Any, preferred_keys: Sequence[str], fuzzy_tokens: Sequence[str] = ()) -> Optional[float]:
    preferred = {k.lower() for k in preferred_keys}

    # Exact key match first.
    for p, v in recursive_items(obj):
        if p and p[-1].lower() in preferred:
            x = as_float(v)
            if x is not None:
                return x

    # Fuzzy match only as a controlled fallback.
    for p, v in recursive_items(obj):
        if not p:
            continue
        key = p[-1].lower()
        if fuzzy_tokens and all(tok.lower() in key for tok in fuzzy_tokens):
            x = as_float(v)
            if x is not None:
                return x
    return None


def append_unique(seq: List[str], msg: str) -> None:
    if msg not in seq:
        seq.append(msg)


# ---------------------------------------------------------------------------
# Artifact discovery
# ---------------------------------------------------------------------------

def group_from_filename(name: str, variant: str, suffix: str) -> Optional[str]:
    prefix = variant + "_"
    if not name.startswith(prefix) or not name.endswith(suffix):
        return None
    middle = name[len(prefix): -len(suffix)]
    return middle if middle else None


def discover_artifacts(ai_runs_dir: Path, variant: str) -> Dict[str, Any]:
    if not ai_runs_dir.exists():
        fatal(f"ai_runs directory not found: {ai_runs_dir}")

    timing: Dict[str, Path] = {}
    mapped: Dict[str, Path] = {}
    classified: Dict[str, Path] = {}

    for p in sorted(ai_runs_dir.glob(f"{variant}_*.json")):
        g = group_from_filename(p.name, variant, "_timing_paths.json")
        if g:
            timing[g] = p
            continue
        g = group_from_filename(p.name, variant, "_rtl_mapped.json")
        if g:
            mapped[g] = p
            continue
        g = group_from_filename(p.name, variant, "_classified.json")
        if g:
            classified[g] = p
            continue

    return {
        "manifest": ai_runs_dir / f"{variant}.json",
        "metrics": ai_runs_dir / f"{variant}_metrics.json",
        "rtl_graph": ai_runs_dir / f"{variant}_rtl_graph.json",
        "timing": timing,
        "mapped": mapped,
        "classified": classified,
    }


def artifact_record(path: Path, role: str, group: Optional[str] = None) -> Dict[str, Any]:
    rec = {
        "role": role,
        "path": str(path),
        "exists": path.exists(),
        "group": group,
    }
    if path.exists():
        st = path.stat()
        rec.update({
            "sha256": sha256_file(path),
            "size_bytes": st.st_size,
            "mtime_epoch": st.st_mtime,
        })
    return rec


# ---------------------------------------------------------------------------
# Auto-classification
# ---------------------------------------------------------------------------

def auto_classify_missing(
    artifacts: Dict[str, Any],
    script_dir: Path,
    auto_classify: bool,
    fatal_errors: List[str],
    warnings: List[str],
) -> None:
    missing = sorted(set(artifacts["mapped"]) - set(artifacts["classified"]))
    if not missing:
        return

    if not auto_classify:
        append_unique(
            fatal_errors,
            "Mapped path groups are missing classification: " + ", ".join(missing),
        )
        return

    classify_script = script_dir / "classify_path.py"
    if not classify_script.exists():
        append_unique(
            fatal_errors,
            f"Cannot auto-classify missing groups because classify_path.py was not found: {classify_script}",
        )
        return

    for group in missing:
        mapped_path = artifacts["mapped"][group]
        output_path = mapped_path.with_name(
            mapped_path.name.replace("_rtl_mapped.json", "_classified.json")
        )

        cmd = [
            sys.executable,
            str(classify_script),
            "--mapped-json",
            str(mapped_path),
            "--output",
            str(output_path),
        ]

        print(f"[builder] Auto-classifying missing group: {group}")
        proc = subprocess.run(cmd, text=True)
        if proc.returncode != 0:
            append_unique(
                fatal_errors,
                f"Classification failed for path group '{group}' with return code {proc.returncode}",
            )
        elif not output_path.exists():
            append_unique(
                fatal_errors,
                f"Classifier returned success but did not create: {output_path}",
            )
        else:
            artifacts["classified"][group] = output_path
            append_unique(warnings, f"Auto-generated missing classification for group '{group}'.")


# ---------------------------------------------------------------------------
# Metrics and clocks
# ---------------------------------------------------------------------------

def extract_baseline_metrics(metrics: Dict[str, Any]) -> Dict[str, Optional[float]]:
    """Extract the frozen four-field canonical baseline PPA object."""
    wns = deep_find_numeric(
        metrics,
        preferred_keys=(
            "wns_ns", "wns", "worst_slack_ns", "worst_slack",
            "finish__timing__setup__ws",
            "timing__setup__ws",
            "setup__ws",
        ),
        fuzzy_tokens=("wns",),
    )
    if wns is None:
        wns = deep_find_numeric(metrics, (), fuzzy_tokens=("worst", "slack"))

    tns = deep_find_numeric(
        metrics,
        preferred_keys=(
            "tns_ns", "tns", "total_negative_slack_ns", "total_negative_slack",
            "finish__timing__setup__tns",
            "timing__setup__tns",
            "setup__tns",
        ),
        fuzzy_tokens=("tns",),
    )

    area = deep_find_numeric(
        metrics,
        preferred_keys=(
            "area_um2", "area", "design_area", "core_area",
            "finish__design__instance__area",
            "place__design__instance__area",
            "design__instance__area",
        ),
        fuzzy_tokens=("area",),
    )

    # power_mw is intentionally unit-explicit.  The collector converts ORFS'
    # finish__power__total from watts to milliwatts before this stage.
    power = deep_find_numeric(
        metrics,
        preferred_keys=(
            "power_mw",
            "total_power_mw",
        ),
        fuzzy_tokens=("power", "mw"),
    )

    return {
        "wns_ns": normalize_metric(wns),
        "tns_ns": normalize_metric(tns),
        "area_um2": normalize_metric(area),
        "power_mw": normalize_metric(power),
    }


def collect_clock_periods_from_json(obj: Any) -> Dict[str, float]:
    found: Dict[str, float] = {}

    # Common dictionary forms.
    for _, v in recursive_items(obj):
        if isinstance(v, dict):
            name = None
            for nk in ("clock_name", "name", "clock", "path_group"):
                if nk in v and isinstance(v[nk], str):
                    name = v[nk].strip()
                    if name:
                        break

            period = None
            for pk in ("period_ns", "clock_period_ns", "clock_period", "period"):
                if pk in v:
                    period = as_float(v[pk])
                    if period is not None:
                        break

            if name and period is not None and period > 0:
                found.setdefault(name, period)

    return found


def resolve_candidate_path(raw: str, bases: Sequence[Path]) -> Optional[Path]:
    expanded = os.path.expandvars(raw.strip().strip("\"'"))
    p = Path(expanded)
    candidates = [p] if p.is_absolute() else [base / p for base in bases]
    for c in candidates:
        try:
            r = c.resolve()
        except Exception:
            r = c
        if r.exists():
            return r
    return None


def discover_sdc_files(
    manifest: Dict[str, Any],
    manifest_path: Path,
    design: Optional[str],
    platform: Optional[str],
) -> List[Path]:
    result: List[Path] = []
    seen = set()

    bases = [
        Path.cwd(),
        manifest_path.parent,
    ]

    config_raw = normalize_text(manifest.get("config"))
    config_path: Optional[Path] = None
    if config_raw:
        config_path = resolve_candidate_path(config_raw, bases)
        if config_path:
            bases.append(config_path.parent)

    def add(p: Path) -> None:
        try:
            rp = p.resolve()
        except Exception:
            rp = p
        key = str(rp)
        if rp.exists() and rp.is_file() and rp.suffix.lower() == ".sdc" and key not in seen:
            seen.add(key)
            result.append(rp)

    # Explicit *.sdc tokens in config.mk.
    if config_path and config_path.exists():
        try:
            text = config_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            text = ""
        for token in re.findall(r"""[^\s"'=]+\.sdc""", text):
            p = resolve_candidate_path(token, bases)
            if p:
                add(p)

        # Design config directories are typically small. Scan nearby.
        for p in config_path.parent.rglob("*.sdc"):
            add(p)

    # Conventional ORFS design directory fallback.
    if design and platform:
        conventional = Path.cwd() / "designs" / platform / design
        if conventional.exists():
            for p in conventional.rglob("*.sdc"):
                add(p)

    return sorted(result)


def resolve_manifest_config_path(
    manifest: Dict[str, Any],
    manifest_path: Path,
) -> Optional[Path]:
    raw = normalize_text(manifest.get("config"))
    if not raw:
        return None
    return resolve_candidate_path(
        raw,
        [Path.cwd(), manifest_path.parent],
    )


def parse_makefile_scalars(path: Optional[Path]) -> Dict[str, str]:
    """
    Read simple scalar assignments from config.mk.

    This is intentionally not a GNU Make evaluator. It resolves the common
    ORFS forms needed for timing constraints, e.g.:

        CLOCK_PERIOD = 1.0
        export CLOCK_PERIOD := 1.0
        CLOCK_PERIOD ?= 1.0
        CLOCK_PORT = clk

    Values are retained as strings so they may be used for either Tcl names
    or numeric expressions.
    """
    if path is None or not path.exists():
        return {}

    values: Dict[str, str] = {}
    text = path.read_text(encoding="utf-8", errors="replace")
    text = re.sub(r"\\\s*\n", " ", text)

    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue

        m = re.match(
            r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*(?:\?=|:=|\+=|=)\s*(.*?)\s*$",
            line,
        )
        if not m:
            continue

        name, value = m.group(1), m.group(2).strip()
        if not value:
            continue

        # Minimal make-variable interpolation for already-known scalar vars.
        def repl_make_var(mm: re.Match[str]) -> str:
            key = mm.group(1) or mm.group(2)
            return values.get(key, mm.group(0))

        value = re.sub(
            r"\$\(([^)]+)\)|\$\{([^}]+)\}",
            repl_make_var,
            value,
        )
        values[name] = value.strip().strip("\"'")

    return values


def read_tcl_word(text: str, start: int) -> Tuple[Optional[str], int]:
    """
    Read one Tcl word beginning at `start`, preserving bracket/brace/quote
    expressions such as:
        $::env(CLOCK_PERIOD)
        ${CLOCK_PERIOD}
        [expr {$::env(CLOCK_PERIOD) / 2.0}]
        "core_clock"
    """
    n = len(text)
    i = start
    while i < n and text[i].isspace():
        i += 1
    if i >= n:
        return None, i

    begin = i
    brace = 0
    bracket = 0
    paren = 0
    quote = False
    escape = False

    while i < n:
        ch = text[i]

        if escape:
            escape = False
            i += 1
            continue

        if ch == "\\":
            escape = True
            i += 1
            continue

        if ch == '"' and brace == 0:
            quote = not quote
            i += 1
            continue

        if not quote:
            if ch == "{":
                brace += 1
            elif ch == "}" and brace > 0:
                brace -= 1
            elif ch == "[":
                bracket += 1
            elif ch == "]" and bracket > 0:
                bracket -= 1
            elif ch == "(":
                paren += 1
            elif ch == ")" and paren > 0:
                paren -= 1
            elif ch.isspace() and brace == 0 and bracket == 0 and paren == 0:
                break
            elif ch == ";" and brace == 0 and bracket == 0 and paren == 0:
                break

        i += 1

    return text[begin:i].strip(), i


def split_tcl_commands(text: str) -> List[str]:
    """Split Tcl/SDC commands without breaking nested []/{} or quoted text."""
    commands: List[str] = []
    buf: List[str] = []
    brace = bracket = 0
    quote = False
    escape = False

    for ch in text:
        if escape:
            buf.append(ch)
            escape = False
            continue

        if ch == "\\":
            buf.append(ch)
            escape = True
            continue

        if ch == '"' and brace == 0:
            quote = not quote
            buf.append(ch)
            continue

        if not quote:
            if ch == "{":
                brace += 1
            elif ch == "}" and brace > 0:
                brace -= 1
            elif ch == "[":
                bracket += 1
            elif ch == "]" and bracket > 0:
                bracket -= 1

            if ch in (";", "\n") and brace == 0 and bracket == 0:
                command = "".join(buf).strip()
                if command:
                    commands.append(command)
                buf = []
                continue

        buf.append(ch)

    command = "".join(buf).strip()
    if command:
        commands.append(command)

    return commands


_ENV_PATTERNS = (
    re.compile(r"^\$::env\(([^)]+)\)$"),
    re.compile(r"^\$env\(([^)]+)\)$"),
)
_TCL_VAR_PATTERN = re.compile(
    r"^\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|::([A-Za-z_][A-Za-z0-9_]*)|([A-Za-z_][A-Za-z0-9_]*))$"
)


def _lookup_scalar(
    name: str,
    tcl_vars: Dict[str, str],
    external_vars: Dict[str, str],
) -> Optional[str]:
    if name in tcl_vars:
        return tcl_vars[name]
    if name in external_vars:
        return external_vars[name]
    if name in os.environ:
        return os.environ[name]
    return None


def resolve_tcl_string(
    word: Optional[str],
    tcl_vars: Dict[str, str],
    external_vars: Dict[str, str],
    depth: int = 0,
) -> Optional[str]:
    if word is None or depth > 12:
        return None

    s = word.strip()
    if not s:
        return None

    if (s.startswith("{") and s.endswith("}")) or (
        s.startswith('"') and s.endswith('"')
    ):
        s = s[1:-1].strip()

    for pattern in _ENV_PATTERNS:
        m = pattern.fullmatch(s)
        if m:
            value = _lookup_scalar(m.group(1), tcl_vars, external_vars)
            if value is None:
                return None
            return resolve_tcl_string(value, tcl_vars, external_vars, depth + 1) or value

    m = _TCL_VAR_PATTERN.fullmatch(s)
    if m:
        name = next(g for g in m.groups() if g is not None)
        value = _lookup_scalar(name, tcl_vars, external_vars)
        if value is None:
            return None
        return resolve_tcl_string(value, tcl_vars, external_vars, depth + 1) or value

    # Substitute embedded Tcl/environment variables inside a quoted/braced
    # scalar. This is useful for expressions and generated names.
    def repl_env(mm: re.Match[str]) -> str:
        name = mm.group(1)
        value = _lookup_scalar(name, tcl_vars, external_vars)
        return value if value is not None else mm.group(0)

    s = re.sub(r"\$::env\(([^)]+)\)", repl_env, s)
    s = re.sub(r"\$env\(([^)]+)\)", repl_env, s)

    def repl_var(mm: re.Match[str]) -> str:
        name = mm.group(1) or mm.group(2) or mm.group(3)
        value = _lookup_scalar(name, tcl_vars, external_vars)
        return value if value is not None else mm.group(0)

    s = re.sub(
        r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|::([A-Za-z_][A-Za-z0-9_]*)|([A-Za-z_][A-Za-z0-9_]*))",
        repl_var,
        s,
    )
    return s.strip().strip("{}\"'")


def safe_numeric_expr(expr: str) -> Optional[float]:
    """
    Evaluate a tiny arithmetic subset safely. Allowed:
        numeric constants, + - * / // % ** and parentheses.
    """
    import ast
    import operator

    bin_ops = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv,
        ast.Mod: operator.mod,
        ast.Pow: operator.pow,
    }
    unary_ops = {
        ast.UAdd: operator.pos,
        ast.USub: operator.neg,
    }

    try:
        node = ast.parse(expr, mode="eval")
    except SyntaxError:
        return None

    def ev(n: ast.AST) -> float:
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return float(n.value)
        if isinstance(n, ast.BinOp) and type(n.op) in bin_ops:
            return float(bin_ops[type(n.op)](ev(n.left), ev(n.right)))
        if isinstance(n, ast.UnaryOp) and type(n.op) in unary_ops:
            return float(unary_ops[type(n.op)](ev(n.operand)))
        raise ValueError("unsupported expression")

    try:
        value = ev(node)
    except Exception:
        return None

    return value if math.isfinite(value) else None


def resolve_tcl_numeric(
    word: Optional[str],
    tcl_vars: Dict[str, str],
    external_vars: Dict[str, str],
) -> Optional[float]:
    if word is None:
        return None

    s = word.strip()

    # [expr {...}] / [expr ...]
    if s.startswith("[expr") and s.endswith("]"):
        inner = s[len("[expr"): -1].strip()
        if inner.startswith("{") and inner.endswith("}"):
            inner = inner[1:-1].strip()
        substituted = resolve_tcl_string(inner, tcl_vars, external_vars)
        if substituted is None:
            return None
        direct = as_float(substituted)
        # as_float is intentionally permissive; for an actual arithmetic
        # expression prefer the safe evaluator first.
        evaluated = safe_numeric_expr(substituted)
        return evaluated if evaluated is not None else direct

    resolved = resolve_tcl_string(s, tcl_vars, external_vars)
    if resolved is None:
        return None

    # Exact numeric literal first.
    try:
        value = float(resolved)
        return value if math.isfinite(value) else None
    except ValueError:
        pass

    return safe_numeric_expr(resolved)


def option_word(command: str, option: str) -> Optional[str]:
    m = re.search(r"(?<!\S)" + re.escape(option) + r"(?!\S)", command)
    if not m:
        return None
    word, _ = read_tcl_word(command, m.end())
    return word


def parse_sdc_clocks(
    path: Path,
    external_vars: Optional[Dict[str, str]] = None,
) -> Dict[str, Dict[str, Any]]:
    """
    Parse authoritative create_clock constraints while resolving common ORFS
    Tcl/config forms:

      create_clock -name core_clock -period 1.0 ...
      set clk_period 1.0
      create_clock -name core_clock -period $clk_period ...
      create_clock -name core_clock -period ${CLOCK_PERIOD} ...
      create_clock -name core_clock -period $::env(CLOCK_PERIOD) ...
      create_clock -name $clock_name -period [expr {$::env(CLOCK_PERIOD)}] ...

    This is not a general Tcl interpreter. If the period cannot be resolved
    deterministically, the builder fails closed rather than guessing.
    """
    external_vars = dict(external_vars or {})
    text = path.read_text(encoding="utf-8", errors="replace")
    text = re.sub(r"\\\s*\n", " ", text)

    commands = split_tcl_commands(text)
    tcl_vars: Dict[str, str] = {}

    # Resolve scalar `set` commands iteratively so aliases can chain.
    pending: List[Tuple[str, str]] = []
    for command in commands:
        clean = command.split("#", 1)[0].strip()
        m = re.match(r"^set\s+([A-Za-z_][A-Za-z0-9_]*)\s+", clean)
        if not m:
            continue
        word, _ = read_tcl_word(clean, m.end())
        if word is not None:
            pending.append((m.group(1), word))

    for _ in range(max(1, len(pending) + 1)):
        changed = False
        for name, word in pending:
            resolved = resolve_tcl_string(word, tcl_vars, external_vars)
            if resolved is not None and tcl_vars.get(name) != resolved:
                tcl_vars[name] = resolved
                changed = True
        if not changed:
            break

    clocks: Dict[str, Dict[str, Any]] = {}

    for command in commands:
        line = command.split("#", 1)[0].strip()
        if not re.search(r"(^|\s)create_clock(\s|$)", line):
            continue

        period_word = option_word(line, "-period")
        period = resolve_tcl_numeric(period_word, tcl_vars, external_vars)
        if period is None or period <= 0:
            continue

        name_word = option_word(line, "-name")
        name = resolve_tcl_string(name_word, tcl_vars, external_vars)

        if not name:
            # No -name: derive from get_ports target when unambiguous.
            tm = re.search(
                r"\[get_ports(?:\s+-[^\]]+)*\s+\{?([^\}\]\s]+)",
                line,
            )
            if tm:
                target_word = tm.group(1)
                name = resolve_tcl_string(target_word, tcl_vars, external_vars) or target_word

        if not name:
            continue

        clocks[name] = {
            "name": name,
            "period_ns": period,
            "source": "sdc",
            "source_file": str(path),
            "period_expression": period_word,
            "name_expression": name_word,
        }

    return clocks


def resolve_clocks(
    metrics: Dict[str, Any],
    manifest: Dict[str, Any],
    manifest_path: Path,
    clock_names: Sequence[str],
    design: Optional[str],
    platform: Optional[str],
) -> Tuple[List[Dict[str, Any]], List[str], List[str]]:
    fatal_errors: List[str] = []
    warnings: List[str] = []

    json_periods = collect_clock_periods_from_json(metrics)
    sdc_files = discover_sdc_files(manifest, manifest_path, design, platform)

    config_path = resolve_manifest_config_path(manifest, manifest_path)
    config_vars = parse_makefile_scalars(config_path)

    # Environment values take precedence over config defaults because ORFS may
    # override Make variables for a specific run.
    external_vars = dict(config_vars)
    for key, value in os.environ.items():
        external_vars[key] = value

    sdc_clocks: Dict[str, Dict[str, Any]] = {}

    for sdc in sdc_files:
        try:
            parsed = parse_sdc_clocks(sdc, external_vars=external_vars)
            for name, info in parsed.items():
                if name in sdc_clocks and not math.isclose(
                    sdc_clocks[name]["period_ns"],
                    info["period_ns"],
                    abs_tol=1e-12,
                ):
                    append_unique(
                        fatal_errors,
                        f"Conflicting SDC periods found for clock '{name}': "
                        f"{sdc_clocks[name]['period_ns']} vs {info['period_ns']} ns",
                    )
                else:
                    sdc_clocks[name] = info
        except Exception as e:
            append_unique(warnings, f"Could not parse SDC {sdc}: {e}")

    result: List[Dict[str, Any]] = []

    for name in sorted(set(clock_names)):
        jp = json_periods.get(name)
        sp = sdc_clocks.get(name)

        # If exactly one authoritative SDC clock exists but its -name came
        # indirectly from a target/variable and does not match the STA path
        # group, do NOT silently rename it unless there is no ambiguity.
        if sp is None and len(sdc_clocks) == 1:
            only = next(iter(sdc_clocks.values()))
            append_unique(
                warnings,
                f"STA path group is '{name}' but the only resolved SDC clock is "
                f"'{only['name']}'. The builder will not assume they are identical.",
            )

        if jp is not None and sp is not None:
            if not math.isclose(jp, sp["period_ns"], abs_tol=1e-6):
                append_unique(
                    fatal_errors,
                    f"Clock period disagreement for '{name}': metrics={jp} ns, "
                    f"SDC={sp['period_ns']} ns",
                )
                continue

            result.append({
                "name": name,
                "period_ns": jp,
                "source": "metrics_and_sdc_agree",
                "source_file": sp["source_file"],
                "period_expression": sp.get("period_expression"),
                "name_expression": sp.get("name_expression"),
            })

        elif sp is not None:
            result.append(sp)

        elif jp is not None:
            result.append({
                "name": name,
                "period_ns": jp,
                "source": "metrics",
                "source_file": None,
                "period_expression": None,
                "name_expression": None,
            })

        else:
            parsed_names = sorted(sdc_clocks)
            detail = (
                f" Resolved SDC clocks: {parsed_names}."
                if parsed_names
                else " No resolvable create_clock command was found."
            )
            append_unique(
                fatal_errors,
                f"Could not resolve an authoritative clock period for path group '{name}'."
                + detail,
            )

    if config_path:
        warnings.append(f"Constraint config inspected: {config_path}")
    if sdc_files:
        warnings.append("SDC files inspected: " + ", ".join(str(p) for p in sdc_files))

    return result, fatal_errors, warnings


# ---------------------------------------------------------------------------
# RTL source resolution
# ---------------------------------------------------------------------------

SRC_RE = re.compile(
    r"(?P<file>.+?\.(?:svh|sv|vh|v)):(?P<l1>\d+)"
    r"(?:\.\d+)?(?:-(?P<l2>\d+)(?:\.\d+)?)?"
)


def parse_src_string(s: str) -> Optional[Dict[str, Any]]:
    s = s.strip()
    m = SRC_RE.search(s)
    if not m:
        return None
    return {
        "source_file": m.group("file"),
        "line_start": int(m.group("l1")),
        "line_end": int(m.group("l2") or m.group("l1")),
        "module": None,
        "raw": s,
    }


def source_candidates_from_obj(obj: Any) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []

    def walk(v: Any) -> None:
        if isinstance(v, dict):
            file_value = None
            for fk in ("source_file", "file", "filename", "rtl_file"):
                if isinstance(v.get(fk), str) and re.search(r"\.(?:svh|sv|vh|v)(?::|$)", v[fk]):
                    file_value = v[fk]
                    break

            l1 = None
            l2 = None
            for lk in ("line_start", "start_line", "line", "lineno"):
                if lk in v:
                    try:
                        l1 = int(v[lk])
                    except Exception:
                        pass
                    if l1 is not None:
                        break
            for lk in ("line_end", "end_line"):
                if lk in v:
                    try:
                        l2 = int(v[lk])
                    except Exception:
                        pass
                    if l2 is not None:
                        break

            module = None
            for mk in ("module", "module_name"):
                if isinstance(v.get(mk), str) and v[mk].strip():
                    module = v[mk].strip()
                    break

            if file_value:
                parsed = parse_src_string(file_value)
                if parsed:
                    if l1 is None:
                        l1 = parsed["line_start"]
                    if l2 is None:
                        l2 = parsed["line_end"]
                    file_value = parsed["source_file"]
                candidates.append({
                    "source_file": file_value,
                    "line_start": l1,
                    "line_end": l2 or l1,
                    "module": module,
                    "raw": v,
                })

            for sk in ("src", "source", "source_location", "location"):
                if isinstance(v.get(sk), str):
                    parsed = parse_src_string(v[sk])
                    if parsed:
                        parsed["module"] = module
                        candidates.append(parsed)

            for child in v.values():
                walk(child)

        elif isinstance(v, list):
            for child in v:
                walk(child)

        elif isinstance(v, str):
            parsed = parse_src_string(v)
            if parsed:
                candidates.append(parsed)

    walk(obj)

    # Deduplicate.
    dedup: List[Dict[str, Any]] = []
    seen = set()
    for c in candidates:
        key = (
            c.get("source_file"),
            c.get("line_start"),
            c.get("line_end"),
            c.get("module"),
        )
        if key not in seen:
            seen.add(key)
            dedup.append(c)
    return dedup


def operator_tokens_from_obj(
    obj: Any,
    allowed_ops: Optional[Sequence[str]] = None,
) -> List[str]:
    """
    Extract explicit Yosys operator tokens (e.g. $add, $mux) from mapper
    metadata. This does not infer semantics from names; it only preserves
    operator tokens already present in upstream evidence.
    """
    allowed = set(allowed_ops or [])
    found: List[str] = []

    def walk(v: Any) -> None:
        if isinstance(v, dict):
            for k, child in v.items():
                if isinstance(k, str) and k.startswith("$"):
                    if not allowed or k in allowed:
                        found.append(k)
                walk(child)
        elif isinstance(v, list):
            for child in v:
                walk(child)
        elif isinstance(v, str):
            for tok in re.findall(r"\$[A-Za-z_][A-Za-z0-9_$]*", v):
                if not allowed or tok in allowed:
                    found.append(tok)

    walk(obj)
    return sorted(set(found))


def infer_operator_tokens_from_source(
    source_text: str,
    allowed_ops: Sequence[str],
) -> List[str]:
    """
    Conservative syntax-based operator identification used only when mapper
    metadata did not label a source location. Returned tokens are tagged by
    the caller as source-syntax evidence, not mapper evidence.
    """
    allowed = set(allowed_ops)
    found: List[str] = []

    checks = (
        ("$mux", "?" in source_text and ":" in source_text),
        ("$xor", "^" in source_text),
        ("$add", "+" in source_text),
        ("$sub", "-" in source_text),
        ("$mul", "*" in source_text),
        ("$div", "/" in source_text),
        ("$mod", "%" in source_text),
        ("$and", "&" in source_text),
        ("$or", "|" in source_text),
        ("$not", "~" in source_text),
        ("$eq", "==" in source_text),
        ("$ne", "!=" in source_text),
        ("$lt", "<" in source_text),
        ("$gt", ">" in source_text),
    )
    for op, condition in checks:
        if condition and op in allowed:
            found.append(op)

    return sorted(set(found))


def resolve_all_source_candidates(
    rtl_mapping: Dict[str, Any],
    classified_path: Path,
) -> Tuple[List[Dict[str, Any]], str]:
    """
    Preserve every authoritative mapped RTL source location.

    Priority:
      1. rtl_regions              (authoritative mapper regions)
      2. candidate_rtl_regions    (fallback only)
      3. whole rtl_mapping        (last-resort compatibility fallback)

    We deliberately do not combine candidate_rtl_regions with rtl_regions
    when authoritative rtl_regions exist, because doing so can introduce
    broader speculative locations into the trusted optimizer packet.
    """
    source_kind = "none"
    candidates: List[Dict[str, Any]] = []

    if isinstance(rtl_mapping.get("rtl_regions"), list) and rtl_mapping["rtl_regions"]:
        source_kind = "rtl_regions"
        candidates = source_candidates_from_obj(rtl_mapping["rtl_regions"])
    elif (
        isinstance(rtl_mapping.get("candidate_rtl_regions"), list)
        and rtl_mapping["candidate_rtl_regions"]
    ):
        source_kind = "candidate_rtl_regions"
        candidates = source_candidates_from_obj(rtl_mapping["candidate_rtl_regions"])
    else:
        source_kind = "rtl_mapping_fallback"
        candidates = source_candidates_from_obj(rtl_mapping)

    bases = [Path.cwd(), classified_path.parent]
    resolved: List[Dict[str, Any]] = []

    for c in candidates:
        raw_file = normalize_text(c.get("source_file"))
        if not raw_file:
            continue

        resolved_file = resolve_candidate_path(raw_file, bases)
        if resolved_file is None:
            # Keep unresolved candidates out of the trusted set. normalize_path
            # will fail closed if no valid source locations remain.
            continue

        line_start = c.get("line_start")
        line_end = c.get("line_end") or line_start
        if line_start is None:
            continue

        cc = dict(c)
        cc["source_file"] = str(resolved_file)
        cc["line_start"] = int(line_start)
        cc["line_end"] = int(line_end)
        resolved.append(cc)

    # Deduplicate while preserving independently labelled operator evidence.
    dedup: Dict[Tuple[str, int, int, Optional[str]], Dict[str, Any]] = {}
    for c in resolved:
        key = (
            str(Path(c["source_file"]).resolve()),
            int(c["line_start"]),
            int(c["line_end"]),
            normalize_text(c.get("module")),
        )
        if key not in dedup:
            dedup[key] = c
        else:
            # Merge raw evidence so explicit operator tags are not lost.
            prior = dedup[key]
            prior_raw = prior.get("raw")
            new_raw = c.get("raw")
            prior["raw"] = [prior_raw, new_raw]

    ordered = list(dedup.values())
    ordered.sort(
        key=lambda c: (
            str(Path(c["source_file"]).resolve()),
            int(c["line_start"]),
            int(c["line_end"]),
        )
    )
    return ordered, source_kind


MODULE_START_RE = re.compile(
    r"^\s*module\s+(?:automatic\s+)?(?P<name>\\?[A-Za-z_$][A-Za-z0-9_$\\.]*)"
)
ENDMODULE_RE = re.compile(r"^\s*endmodule\b")


def find_verilog_modules(lines: Sequence[str]) -> List[Dict[str, Any]]:
    modules: List[Dict[str, Any]] = []
    current = None

    for idx, line in enumerate(lines, start=1):
        m = MODULE_START_RE.match(line)
        if m and current is None:
            current = {
                "name": m.group("name"),
                "line_start": idx,
                "line_end": None,
            }
            continue

        if current is not None and ENDMODULE_RE.match(line):
            current["line_end"] = idx
            modules.append(current)
            current = None

    if current is not None:
        current["line_end"] = len(lines)
        modules.append(current)

    return modules


def resolve_module(
    lines: Sequence[str],
    target_line: Optional[int],
    hinted_module: Optional[str],
    design: Optional[str],
) -> Optional[Dict[str, Any]]:
    modules = find_verilog_modules(lines)
    if not modules:
        return None

    if hinted_module:
        for m in modules:
            if m["name"] == hinted_module:
                return m

    if target_line is not None:
        containing = [
            m for m in modules
            if m["line_start"] <= target_line <= (m["line_end"] or len(lines))
        ]
        if len(containing) == 1:
            return containing[0]

    if design:
        for m in modules:
            if m["name"] == design:
                return m

    if len(modules) == 1:
        return modules[0]

    return None


def extract_upstream_source_hash(obj: Any) -> Optional[str]:
    for p, v in recursive_items(obj):
        if not p or not isinstance(v, str):
            continue
        key = p[-1].lower()
        if "sha256" in key and re.fullmatch(r"[0-9a-fA-F]{64}", v.strip()):
            return v.strip().lower()
    return None


def source_snapshot(
    source_file: Path,
    target_start: int,
    target_end: int,
    hinted_module: Optional[str],
    design: Optional[str],
    context_lines: int,
    analysis_artifact: Path,
    upstream_hash: Optional[str],
) -> Dict[str, Any]:
    text = source_file.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()

    if target_start < 1 or target_start > len(lines):
        fatal(
            f"Mapped source line {target_start} is outside {source_file} "
            f"(1..{len(lines)})"
        )
    target_end = max(target_start, target_end)
    target_end = min(target_end, len(lines))

    module = resolve_module(lines, target_start, hinted_module, design)
    if module is None:
        fatal(
            f"Could not unambiguously resolve RTL module containing "
            f"{source_file}:{target_start}"
        )

    module_start = module["line_start"]
    module_end = module["line_end"] or len(lines)

    context_start = max(module_start, target_start - context_lines)
    context_end = min(module_end, target_end + context_lines)

    file_hash = sha256_file(source_file)
    source_mtime = source_file.stat().st_mtime
    analysis_mtime = analysis_artifact.stat().st_mtime

    source_newer_than_analysis = (
        source_mtime > analysis_mtime + SOURCE_MTIME_TOLERANCE_SEC
    )

    upstream_hash_matches = None
    if upstream_hash:
        upstream_hash_matches = (upstream_hash.lower() == file_hash.lower())

    return {
        "file": str(source_file),
        "sha256": file_hash,
        "module": module["name"],
        "module_line_start": module_start,
        "module_line_end": module_end,
        "module_text": "\n".join(lines[module_start - 1: module_end]),
        "target_line_start": target_start,
        "target_line_end": target_end,
        "target_text": "\n".join(lines[target_start - 1: target_end]),
        "context_line_start": context_start,
        "context_line_end": context_end,
        "context_text": "\n".join(lines[context_start - 1: context_end]),
        "integrity": {
            "source_mtime_epoch": source_mtime,
            "analysis_artifact_path": str(analysis_artifact.resolve()),
            "analysis_artifact_mtime_epoch": analysis_mtime,
            "source_newer_than_analysis_artifact": source_newer_than_analysis,
            "upstream_source_sha256": upstream_hash,
            "upstream_hash_matches_current_source": upstream_hash_matches,
            "freshness_assurance": (
                "cryptographic"
                if upstream_hash is not None
                else "mtime_guard_only"
            ),
        },
    }


def aggregate_source_span(
    locations: Sequence[Dict[str, Any]],
    context_lines: int,
) -> Optional[Dict[str, Any]]:
    """
    Build a single deterministic source span around a set of exact mapped
    source locations. All locations must belong to the same file/module.
    """
    if not locations:
        return None

    files = {loc["file"] for loc in locations}
    modules = {loc["module"] for loc in locations}
    hashes = {loc["sha256"] for loc in locations}

    if len(files) != 1 or len(modules) != 1 or len(hashes) != 1:
        return None

    first = locations[0]
    lines = first["module_text"].splitlines()
    module_start = int(first["module_line_start"])
    module_end = int(first["module_line_end"])

    start = min(int(loc["target_line_start"]) for loc in locations)
    end = max(int(loc["target_line_end"]) for loc in locations)
    context_start = max(module_start, start - context_lines)
    context_end = min(module_end, end + context_lines)

    # module_text starts at module_start, so translate absolute line numbers.
    def slice_abs(abs_start: int, abs_end: int) -> str:
        rel_start = abs_start - module_start
        rel_end = abs_end - module_start + 1
        return "\n".join(lines[rel_start:rel_end])

    exact_locations = []
    for loc in sorted(
        locations,
        key=lambda x: (x["target_line_start"], x["target_line_end"]),
    ):
        exact_locations.append({
            "file": loc["file"],
            "sha256": loc["sha256"],
            "module": loc["module"],
            "line_start": loc["target_line_start"],
            "line_end": loc["target_line_end"],
            "text": loc["target_text"],
            "operator_roles": loc.get("operator_roles", []),
            "operator_role_evidence": loc.get("operator_role_evidence", "unresolved"),
        })

    return {
        "file": first["file"],
        "sha256": first["sha256"],
        "module": first["module"],
        "module_line_start": module_start,
        "module_line_end": module_end,
        "source_locations": exact_locations,
        "target_line_start": start,
        "target_line_end": end,
        "target_text": slice_abs(start, end),
        "context_line_start": context_start,
        "context_line_end": context_end,
        "context_text": slice_abs(context_start, context_end),
        "module_text": first["module_text"],
        "integrity": first["integrity"],
    }


def source_location_key(loc: Dict[str, Any]) -> Tuple[str, str, int, int]:
    return (
        str(loc["file"]),
        str(loc["module"]),
        int(loc["line_start"]),
        int(loc["line_end"]),
    )


def intervals_overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    return not (a_end < b_start or b_end < a_start)


def source_location_sets_overlap(
    a_locations: Sequence[Dict[str, Any]],
    b_locations: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    shared: List[Dict[str, Any]] = []

    for a in a_locations:
        for b in b_locations:
            if a["file"] != b["file"] or a["module"] != b["module"]:
                continue
            if intervals_overlap(
                int(a["line_start"]),
                int(a["line_end"]),
                int(b["line_start"]),
                int(b["line_end"]),
            ):
                shared.append({
                    "file": a["file"],
                    "module": a["module"],
                    "a_line_start": int(a["line_start"]),
                    "a_line_end": int(a["line_end"]),
                    "b_line_start": int(b["line_start"]),
                    "b_line_end": int(b["line_end"]),
                })

    return shared


# ---------------------------------------------------------------------------
# Path normalization and consistency checking
# ---------------------------------------------------------------------------

def normalize_operator_counts(v: Any) -> Dict[str, int]:
    if not isinstance(v, dict):
        return {}
    out: Dict[str, int] = {}
    for k, value in v.items():
        try:
            out[str(k)] = int(value)
        except Exception:
            pass
    return dict(sorted(out.items()))


def min_confidence_ok(value: str, minimum: str) -> bool:
    return CONFIDENCE_ORDER.get(value.lower(), 0) >= CONFIDENCE_ORDER.get(minimum.lower(), 3)


def normalize_path(
    raw: Dict[str, Any],
    artifact_group: str,
    classified_path: Path,
    design: Optional[str],
    context_lines: int,
    minimum_confidence: str,
    fatal_errors: List[str],
    warnings: List[str],
    source_cache: Dict[Tuple[str, int, int, str, str], Dict[str, Any]],
) -> Dict[str, Any]:
    original_id = normalize_text(raw.get("path_id"))
    if original_id is None:
        original_id = "unknown"

    global_id = f"{artifact_group}:{original_id}"

    rtl = raw.get("rtl_mapping") if isinstance(raw.get("rtl_mapping"), dict) else {}
    cls = raw.get("classification") if isinstance(raw.get("classification"), dict) else {}

    slack = normalize_metric(raw.get("slack_ns"))
    arrival = normalize_metric(raw.get("arrival_time_ns"))
    required = normalize_metric(raw.get("required_time_ns"))
    comb_delay = normalize_metric(raw.get("combinational_delay_ns"))
    cell_delay = normalize_metric(raw.get("cell_delay_sum_ns"))
    net_delay = normalize_metric(raw.get("net_delay_sum_ns"))
    logic_depth = raw.get("logic_depth")
    max_fanout = raw.get("max_fanout")

    path_errors: List[str] = []
    path_warnings: List[str] = []

    for name, value in (
        ("slack_ns", slack),
        ("arrival_time_ns", arrival),
        ("required_time_ns", required),
        ("combinational_delay_ns", comb_delay),
    ):
        if value is None:
            path_errors.append(f"missing {name}")

    if logic_depth is None:
        path_errors.append("missing logic_depth")
    if max_fanout is None:
        path_errors.append("missing max_fanout")

    timing_identity_ok = None
    if slack is not None and arrival is not None and required is not None:
        timing_identity_ok = math.isclose(
            required - arrival,
            slack,
            abs_tol=NS_TOLERANCE_DEFAULT,
        )
        if not timing_identity_ok:
            path_errors.append(
                f"timing identity mismatch: required-arrival={required-arrival:.6f} "
                f"but slack={slack:.6f}"
            )

    mapping_status = normalize_text(rtl.get("status")) or "unknown"
    has_user_source = bool(rtl.get("has_user_rtl_source"))
    if mapping_status != "mapped":
        path_errors.append(
            f"rtl mapping status is '{mapping_status}', expected 'mapped'"
        )
    if not has_user_source:
        path_errors.append("path has no user RTL source")

    class_status = normalize_text(cls.get("status")) or "unknown"
    structural_class = normalize_text(cls.get("structural_class")) or "unknown"
    structural_pattern = normalize_text(cls.get("structural_pattern")) or "unknown"
    confidence = (normalize_text(cls.get("confidence")) or "unknown").lower()

    if class_status != "classified":
        path_errors.append(
            f"classification status is '{class_status}', expected 'classified'"
        )
    if structural_class == "unknown" or structural_pattern == "unknown":
        path_errors.append("classification is missing structural class/pattern")
    if not min_confidence_ok(confidence, minimum_confidence):
        path_errors.append(
            f"classification confidence '{confidence}' is below required "
            f"'{minimum_confidence}'"
        )

    mapped_ops = normalize_operator_counts(rtl.get("operator_counts"))
    classified_ops = normalize_operator_counts(
        cls.get("effective_operator_counts")
        if cls.get("effective_operator_counts") is not None
        else cls.get("original_logical_operator_counts")
    )

    operator_counts_match = (
        mapped_ops == classified_ops
        if classified_ops
        else False
    )
    if not classified_ops:
        path_errors.append(
            "classifier did not provide effective/original logical operator counts"
        )
    elif not operator_counts_match:
        path_errors.append(
            f"operator count mismatch: mapping={mapped_ops}, "
            f"classification={classified_ops}"
        )

    # Preserve every authoritative mapper source location instead of collapsing
    # a multi-operator cone to a single line.
    source_candidates, source_location_provenance = resolve_all_source_candidates(
        rtl,
        classified_path,
    )
    source_locations: List[Dict[str, Any]] = []
    upstream_hash = extract_upstream_source_hash(rtl)

    if not source_candidates:
        path_errors.append(
            "could not resolve any RTL source location from rtl_mapping"
        )
    else:
        for candidate in source_candidates:
            source_file = Path(str(candidate["source_file"]))
            line_start = int(candidate["line_start"])
            line_end = int(candidate.get("line_end") or line_start)

            cache_key = (
                str(source_file.resolve()),
                line_start,
                line_end,
                upstream_hash or "",
                str(classified_path.resolve()),
            )

            try:
                if cache_key not in source_cache:
                    source_cache[cache_key] = source_snapshot(
                        source_file=source_file.resolve(),
                        target_start=line_start,
                        target_end=line_end,
                        hinted_module=candidate.get("module"),
                        design=design,
                        context_lines=context_lines,
                        analysis_artifact=classified_path,
                        upstream_hash=upstream_hash,
                    )

                loc = dict(source_cache[cache_key])

                mapper_roles = operator_tokens_from_obj(
                    candidate.get("raw"),
                    allowed_ops=list(mapped_ops),
                )
                if mapper_roles:
                    roles = mapper_roles
                    role_evidence = "mapper_metadata"
                else:
                    roles = infer_operator_tokens_from_source(
                        loc["target_text"],
                        allowed_ops=list(mapped_ops),
                    )
                    role_evidence = (
                        "source_syntax"
                        if roles
                        else "unresolved"
                    )

                loc["operator_roles"] = roles
                loc["operator_role_evidence"] = role_evidence
                source_locations.append(loc)

            except Exception as e:
                path_errors.append(str(e))

    # Deduplicate exact resolved locations.
    dedup_locations: Dict[Tuple[str, str, int, int], Dict[str, Any]] = {}
    for loc in source_locations:
        key = (
            loc["file"],
            loc["module"],
            int(loc["target_line_start"]),
            int(loc["target_line_end"]),
        )
        if key not in dedup_locations:
            dedup_locations[key] = loc
        else:
            merged_roles = sorted(set(
                dedup_locations[key].get("operator_roles", [])
                + loc.get("operator_roles", [])
            ))
            dedup_locations[key]["operator_roles"] = merged_roles
            if (
                dedup_locations[key].get("operator_role_evidence") == "unresolved"
                and loc.get("operator_role_evidence") != "unresolved"
            ):
                dedup_locations[key]["operator_role_evidence"] = loc.get(
                    "operator_role_evidence"
                )

    source_locations = sorted(
        dedup_locations.values(),
        key=lambda x: (
            x["file"],
            x["module"],
            x["target_line_start"],
            x["target_line_end"],
        ),
    )

    # Every location in one timing path's RTL cone must belong to the same
    # user RTL module/file for a single-region optimization packet.
    source_span = aggregate_source_span(source_locations, context_lines)
    if source_locations and source_span is None:
        path_errors.append(
            "mapped RTL source locations span multiple files/modules/hashes; "
            "cannot form one trusted path source cone"
        )

    if source_locations:
        for loc in source_locations:
            integ = loc["integrity"]
            if integ["source_newer_than_analysis_artifact"]:
                path_errors.append(
                    "RTL source file is newer than the classification artifact; "
                    "analysis may be stale"
                )
            if integ["upstream_hash_matches_current_source"] is False:
                path_errors.append(
                    "upstream RTL source SHA-256 does not match current source file"
                )

        if all(
            loc["integrity"]["freshness_assurance"] == "mtime_guard_only"
            for loc in source_locations
        ):
            path_warnings.append(
                "upstream source hash unavailable; source freshness is guarded "
                "by mtime and a build-time source snapshot/hash"
            )

    # Check whether mapper/source-location evidence covers all mapped operators.
    source_roles = sorted(set(
        op
        for loc in source_locations
        for op in loc.get("operator_roles", [])
    ))
    uncovered_ops = sorted(set(mapped_ops) - set(source_roles))
    source_operator_coverage_complete = len(uncovered_ops) == 0

    if uncovered_ops:
        # Do not invent operator-to-line attribution. Preserve the complete
        # location set and expose the uncertainty. For trusted optimization,
        # all mapped operator kinds must be represented by some source location.
        path_errors.append(
            "mapped operator kinds lack source-location evidence: "
            + ", ".join(uncovered_ops)
        )

    for e in path_errors:
        append_unique(fatal_errors, f"{global_id}: {e}")
    for w in path_warnings:
        append_unique(warnings, f"{global_id}: {w}")

    timing_status = "unknown"
    if slack is not None:
        timing_status = "violating" if slack < 0 else "critical_but_met"

    primary_source = source_span

    return {
        "path_id": global_id,
        "artifact_group": artifact_group,
        "original_path_id": original_id,
        "startpoint": normalize_text(raw.get("startpoint")),
        "endpoint": normalize_text(raw.get("endpoint")),
        "start_signal": normalize_text(raw.get("start_signal")),
        "end_signal": normalize_text(raw.get("end_signal")),
        "timing": {
            "slack_ns": slack,
            "arrival_ns": arrival,
            "required_ns": required,
            "combinational_delay_ns": comb_delay,
            "cell_delay_sum_ns": cell_delay,
            "net_delay_sum_ns": net_delay,
            "logic_depth": logic_depth,
            "max_fanout": max_fanout,
            "path_group": normalize_text(raw.get("path_group")),
            "path_type": normalize_text(raw.get("path_type")),
            "timing_status": timing_status,
        },
        "physical_evidence": {
            "cell_type_counts": (
                raw.get("cell_type_counts")
                if isinstance(raw.get("cell_type_counts"), dict)
                else {}
            ),
            "top_delay_cells": (
                raw.get("top_delay_cells")
                if isinstance(raw.get("top_delay_cells"), list)
                else []
            ),
            "launch_clock_arrival_ns": normalize_metric(
                raw.get("launch_clock_arrival_ns")
            ),
            "launch_clock_to_q_ns": normalize_metric(
                raw.get("launch_clock_to_q_ns")
            ),
            "launch_q_arrival_ns": normalize_metric(
                raw.get("launch_q_arrival_ns")
            ),
            "capture_clock_arrival_ns": normalize_metric(
                raw.get("capture_clock_arrival_ns")
            ),
        },
        "rtl_mapping": {
            "status": mapping_status,
            "method": normalize_text(rtl.get("method")),
            "has_user_rtl_source": has_user_source,
            "rtl_cone_cell_count": rtl.get("rtl_cone_cell_count"),
            "operator_counts": mapped_ops,
            "remaining_logical_reconvergence": bool(
                rtl.get("remaining_logical_reconvergence")
            ),
            "direct_wire_path": bool(rtl.get("direct_wire_path")),
            "source_location_provenance": source_location_provenance,
            "source_locations": [
                {
                    "file": loc["file"],
                    "sha256": loc["sha256"],
                    "module": loc["module"],
                    "line_start": loc["target_line_start"],
                    "line_end": loc["target_line_end"],
                    "text": loc["target_text"],
                    "operator_roles": loc.get("operator_roles", []),
                    "operator_role_evidence": loc.get(
                        "operator_role_evidence",
                        "unresolved",
                    ),
                    "integrity": loc["integrity"],
                }
                for loc in source_locations
            ],
            # Backward-compatible aggregate source cone. Unlike v2.1, this
            # spans ALL mapped source locations for the path.
            "source": primary_source,
        },
        "classification": {
            "status": class_status,
            "class": structural_class,
            "pattern": structural_pattern,
            "confidence": confidence,
            "primary_bottleneck": normalize_text(
                cls.get("primary_bottleneck")
            ),
            "operator_counts": classified_ops,
            "optimization_hints": (
                cls.get("optimization_hints")
                if isinstance(cls.get("optimization_hints"), list)
                else []
            ),
            "remaining_logical_reconvergence": cls.get(
                "remaining_logical_reconvergence"
            ),
            "complexity": (
                cls.get("complexity")
                if isinstance(cls.get("complexity"), dict)
                else {}
            ),
        },
        "consistency": {
            "timing_identity_ok": timing_identity_ok,
            "operator_counts_match": operator_counts_match,
            "source_resolved": primary_source is not None,
            "source_location_count": len(source_locations),
            "source_operator_roles": source_roles,
            "uncovered_mapped_operator_kinds": uncovered_ops,
            "source_operator_coverage_complete": (
                source_operator_coverage_complete
            ),
            "path_ready_for_regioning": len(path_errors) == 0,
            "errors": path_errors,
            "warnings": path_warnings,
        },
    }


def aggregate_integrity_records(
    integrity_records: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Aggregate source-freshness provenance across all analysis artifacts that
    contributed to a region/unit. Shared RTL lines must not inherit one
    arbitrary classifier artifact's timestamp.
    """
    records = [r for r in integrity_records if isinstance(r, dict)]
    if not records:
        return {
            "analysis_artifacts": [],
            "source_newer_than_any_analysis_artifact": None,
            "all_upstream_hashes_match_current_source": None,
            "freshness_assurance": "unknown",
        }

    analysis_artifacts = []
    seen = set()
    for r in records:
        key = (
            r.get("analysis_artifact_path"),
            r.get("analysis_artifact_mtime_epoch"),
        )
        if key not in seen:
            seen.add(key)
            analysis_artifacts.append({
                "path": r.get("analysis_artifact_path"),
                "mtime_epoch": r.get("analysis_artifact_mtime_epoch"),
                "source_newer_than_analysis_artifact": r.get(
                    "source_newer_than_analysis_artifact"
                ),
            })

    source_hashes = {
        r.get("upstream_source_sha256")
        for r in records
        if r.get("upstream_source_sha256")
    }
    hash_match_values = [
        r.get("upstream_hash_matches_current_source")
        for r in records
        if r.get("upstream_hash_matches_current_source") is not None
    ]

    all_hashes_match = (
        all(hash_match_values)
        if hash_match_values
        else None
    )

    freshness_modes = {
        r.get("freshness_assurance", "unknown")
        for r in records
    }
    freshness_assurance = (
        "cryptographic"
        if freshness_modes == {"cryptographic"}
        else "mtime_guard_only"
        if freshness_modes <= {"mtime_guard_only", "cryptographic"}
        else "mixed_or_unknown"
    )

    return {
        "source_mtime_epoch": records[0].get("source_mtime_epoch"),
        "analysis_artifacts": analysis_artifacts,
        "source_newer_than_any_analysis_artifact": any(
            bool(r.get("source_newer_than_analysis_artifact"))
            for r in records
        ),
        "upstream_source_sha256_values": sorted(source_hashes),
        "all_upstream_hashes_match_current_source": all_hashes_match,
        "freshness_assurance": freshness_assurance,
    }


# ---------------------------------------------------------------------------
# Region construction
# ---------------------------------------------------------------------------

def operator_signature(counts: Dict[str, int]) -> Tuple[Tuple[str, int], ...]:
    return tuple(sorted((str(k), int(v)) for k, v in counts.items()))


def normalized_path_source_location_keys(
    path: Dict[str, Any],
) -> Tuple[Tuple[str, str, int, int], ...]:
    keys = []
    for loc in path["rtl_mapping"].get("source_locations", []):
        keys.append(source_location_key(loc))
    return tuple(sorted(set(keys)))


def region_key(path: Dict[str, Any]) -> Optional[Tuple[Any, ...]]:
    source = path["rtl_mapping"].get("source")
    locations = normalized_path_source_location_keys(path)
    if not source or not locations:
        return None

    return (
        source["file"],
        source["module"],
        locations,
        path["classification"]["class"],
        path["classification"]["pattern"],
        operator_signature(path["classification"]["operator_counts"]),
    )


def allowed_transformation_families(
    structural_class: str,
    pattern: str,
    policy: Dict[str, Any],
) -> List[str]:
    out: List[str] = []

    if policy["allow_boolean_restructuring"]:
        out.append("boolean_restructuring")

    if policy["allow_mux_restructuring"] and "mux" in pattern.lower():
        out.append("mux_restructuring")

    if policy["allow_arithmetic_restructuring"] and (
        structural_class.lower() == "arithmetic"
        or any(
            tok in pattern.lower()
            for tok in ("adder", "add", "sub", "mul", "arithmetic")
        )
    ):
        out.append("arithmetic_restructuring")
        out.append("logic_factoring")

    if policy["allow_register_retiming"]:
        out.append("register_retiming")

    if policy["allow_pipeline"]:
        out.append("pipelining")

    return list(dict.fromkeys(out))


def build_regions(
    paths: Sequence[Dict[str, Any]],
    policy: Dict[str, Any],
    context_lines: int,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    Build diagnostic optimization regions.

    A region groups paths only when they share the SAME COMPLETE mapped source
    location set, structural class/pattern, and operator signature. This avoids
    the v2.1 error of collapsing a multi-operator cone to one primary line.
    """
    grouped: Dict[Tuple[Any, ...], List[Dict[str, Any]]] = defaultdict(list)
    blockers: List[str] = []

    for p in paths:
        if not p["consistency"]["path_ready_for_regioning"]:
            blockers.append(
                f"{p['path_id']} cannot be regioned because path validation failed"
            )
            continue

        key = region_key(p)
        if key is None:
            blockers.append(
                f"{p['path_id']} has no conservative complete-source region key"
            )
            continue
        grouped[key].append(p)

    regions: List[Dict[str, Any]] = []

    def worst_slack(group: Sequence[Dict[str, Any]]) -> float:
        vals = [
            p["timing"]["slack_ns"]
            for p in group
            if p["timing"]["slack_ns"] is not None
        ]
        return min(vals) if vals else float("inf")

    ordered_groups = sorted(grouped.values(), key=worst_slack)

    for idx, group in enumerate(ordered_groups):
        first = group[0]

        same_source = len({
            p["rtl_mapping"]["source"]["file"]
            for p in group
        }) == 1
        same_module = len({
            p["rtl_mapping"]["source"]["module"]
            for p in group
        }) == 1
        same_location_set = len({
            normalized_path_source_location_keys(p)
            for p in group
        }) == 1
        same_pattern = len({
            p["classification"]["pattern"]
            for p in group
        }) == 1
        same_class = len({
            p["classification"]["class"]
            for p in group
        }) == 1
        same_ops = len({
            operator_signature(p["classification"]["operator_counts"])
            for p in group
        }) == 1

        grouping_ok = all((
            same_source,
            same_module,
            same_location_set,
            same_pattern,
            same_class,
            same_ops,
        ))

        slacks = [
            p["timing"]["slack_ns"]
            for p in group
            if p["timing"]["slack_ns"] is not None
        ]
        combs = [
            p["timing"]["combinational_delay_ns"]
            for p in group
            if p["timing"]["combinational_delay_ns"] is not None
        ]
        depths = [
            p["timing"]["logic_depth"]
            for p in group
            if isinstance(p["timing"]["logic_depth"], (int, float))
        ]
        fanouts = [
            p["timing"]["max_fanout"]
            for p in group
            if isinstance(p["timing"]["max_fanout"], (int, float))
        ]

        region_blockers: List[str] = []
        if not grouping_ok:
            region_blockers.append(
                "diagnostic region grouping evidence is inconsistent"
            )

        transformations = allowed_transformation_families(
            first["classification"]["class"],
            first["classification"]["pattern"],
            policy,
        )
        if not transformations:
            region_blockers.append(
                "no legal transformation family is enabled by policy"
            )

        # All paths in the group have the same complete source-location set.
        location_records = first["rtl_mapping"]["source_locations"]

        # Reconstruct full source snapshots for aggregate span from the
        # backward-compatible source cone, which already spans all locations.
        src = first["rtl_mapping"]["source"]

        region_integrity_records = [
            loc["integrity"]
            for p in group
            for loc in p["rtl_mapping"].get("source_locations", [])
            if isinstance(loc.get("integrity"), dict)
        ]
        region_integrity = aggregate_integrity_records(
            region_integrity_records
        )

        region = {
            "region_id": f"region_{idx:03d}",
            "role": "diagnostic_subregion",
            "eligibility": {
                # Regions are evidence; the optimizer must act on units.
                "diagnostic_ready": len(region_blockers) == 0,
                "direct_optimization_allowed": False,
                "blockers": region_blockers,
            },
            "source": {
                "file": src["file"],
                "sha256": src["sha256"],
                "module": src["module"],
                "module_line_start": src["module_line_start"],
                "module_line_end": src["module_line_end"],
                "source_locations": location_records,
                "target_line_start": src["target_line_start"],
                "target_line_end": src["target_line_end"],
                "target_text": src["target_text"],
                "context_line_start": src["context_line_start"],
                "context_line_end": src["context_line_end"],
                "context_text": src["context_text"],
                "module_text": src["module_text"],
                "integrity": region_integrity,
            },
            "affected_paths": [p["path_id"] for p in group],
            "artifact_groups": sorted({
                p["artifact_group"]
                for p in group
            }),
            "timing_summary": {
                "path_count": len(group),
                "violating_path_count": sum(
                    1
                    for p in group
                    if p["timing"]["slack_ns"] is not None
                    and p["timing"]["slack_ns"] < 0
                ),
                "worst_slack_ns": min(slacks) if slacks else None,
                "best_slack_ns": max(slacks) if slacks else None,
                "max_combinational_delay_ns": (
                    max(combs) if combs else None
                ),
                "max_logic_depth": max(depths) if depths else None,
                "max_fanout": max(fanouts) if fanouts else None,
                "path_groups": sorted({
                    p["timing"]["path_group"]
                    for p in group
                    if p["timing"]["path_group"]
                }),
            },
            "structural_summary": {
                "class": first["classification"]["class"],
                "pattern": first["classification"]["pattern"],
                "confidence": min(
                    (
                        p["classification"]["confidence"]
                        for p in group
                    ),
                    key=lambda x: CONFIDENCE_ORDER.get(x, 0),
                ),
                "operator_counts": first["classification"]["operator_counts"],
                "primary_bottlenecks": sorted({
                    p["classification"]["primary_bottleneck"]
                    for p in group
                    if p["classification"]["primary_bottleneck"]
                }),
            },
            "grouping_evidence": {
                "same_source_file": same_source,
                "same_module": same_module,
                "same_complete_source_location_set": same_location_set,
                "same_structural_pattern": same_pattern,
                "same_structural_class": same_class,
                "same_operator_signature": same_ops,
                "grouping_confidence": (
                    "high" if grouping_ok else "low"
                ),
            },
            "allowed_transformation_families": transformations,
            "optimization_unit_id": None,
        }
        regions.append(region)

    return regions, blockers


def region_source_locations(region: Dict[str, Any]) -> List[Dict[str, Any]]:
    return list(region.get("source", {}).get("source_locations", []))


def build_optimization_units(
    regions: List[Dict[str, Any]],
    paths_by_id: Dict[str, Dict[str, Any]],
    policy: Dict[str, Any],
    context_lines: int,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    Connect diagnostic regions into optimizer action units whenever they share
    any mapped RTL source line/interval. Connected components are the atomic
    optimizer targets.

    Example:
        ADD->MUX region  : lines {19,22}
        MUX-only region  : line  {22}
        XOR->MUX region  : lines {20,22}

    All become one optimization unit because line 22 is shared.
    """
    blockers: List[str] = []
    n = len(regions)

    # Undirected overlap graph.
    adjacency: Dict[int, set[int]] = {
        i: set()
        for i in range(n)
    }
    overlap_evidence: Dict[Tuple[int, int], List[Dict[str, Any]]] = {}

    for i in range(n):
        for j in range(i + 1, n):
            shared = source_location_sets_overlap(
                region_source_locations(regions[i]),
                region_source_locations(regions[j]),
            )
            if shared:
                adjacency[i].add(j)
                adjacency[j].add(i)
                overlap_evidence[(i, j)] = shared

    # Connected components.
    components: List[List[int]] = []
    seen: set[int] = set()
    for i in range(n):
        if i in seen:
            continue
        stack = [i]
        component: List[int] = []
        seen.add(i)
        while stack:
            cur = stack.pop()
            component.append(cur)
            for nxt in sorted(adjacency[cur]):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        components.append(sorted(component))

    def component_worst(indices: Sequence[int]) -> float:
        vals = [
            regions[i]["timing_summary"]["worst_slack_ns"]
            for i in indices
            if regions[i]["timing_summary"]["worst_slack_ns"] is not None
        ]
        return min(vals) if vals else float("inf")

    components.sort(key=component_worst)

    units: List[Dict[str, Any]] = []

    for unit_idx, indices in enumerate(components):
        member_regions = [regions[i] for i in indices]
        unit_id = f"unit_{unit_idx:03d}"

        unit_blockers: List[str] = []
        if any(
            not r["eligibility"]["diagnostic_ready"]
            for r in member_regions
        ):
            unit_blockers.append(
                "one or more member diagnostic regions failed validation"
            )

        files = {
            r["source"]["file"]
            for r in member_regions
        }
        modules = {
            r["source"]["module"]
            for r in member_regions
        }
        hashes = {
            r["source"]["sha256"]
            for r in member_regions
        }
        if len(files) != 1 or len(modules) != 1 or len(hashes) != 1:
            unit_blockers.append(
                "overlapping member regions do not share one source file/module/hash"
            )

        # Union exact source locations.
        location_map: Dict[
            Tuple[str, str, int, int],
            Dict[str, Any],
        ] = {}
        for r in member_regions:
            for loc in region_source_locations(r):
                key = source_location_key(loc)
                if key not in location_map:
                    location_map[key] = dict(loc)
                else:
                    roles = sorted(set(
                        location_map[key].get("operator_roles", [])
                        + loc.get("operator_roles", [])
                    ))
                    location_map[key]["operator_roles"] = roles

        locations = sorted(
            location_map.values(),
            key=lambda loc: (
                loc["file"],
                loc["module"],
                loc["line_start"],
                loc["line_end"],
            ),
        )

        # Build unit source span from the module text already embedded in the
        # first region. We do not reopen or rediscover source here.
        unit_source = None
        if locations and len(files) == len(modules) == len(hashes) == 1:
            first_region_source = member_regions[0]["source"]
            module_start = int(first_region_source["module_line_start"])
            module_end = int(first_region_source["module_line_end"])
            module_lines = first_region_source["module_text"].splitlines()

            start = min(int(loc["line_start"]) for loc in locations)
            end = max(int(loc["line_end"]) for loc in locations)
            context_start = max(module_start, start - context_lines)
            context_end = min(module_end, end + context_lines)

            def slice_abs(abs_start: int, abs_end: int) -> str:
                rel_start = abs_start - module_start
                rel_end = abs_end - module_start + 1
                return "\n".join(module_lines[rel_start:rel_end])

            unit_source = {
                "file": next(iter(files)),
                "sha256": next(iter(hashes)),
                "module": next(iter(modules)),
                "module_line_start": module_start,
                "module_line_end": module_end,
                "source_locations": locations,
                "target_line_start": start,
                "target_line_end": end,
                "target_text": slice_abs(start, end),
                "context_line_start": context_start,
                "context_line_end": context_end,
                "context_text": slice_abs(context_start, context_end),
                "module_text": first_region_source["module_text"],
                "integrity": first_region_source["integrity"],
            }
        else:
            unit_blockers.append(
                "could not form deterministic aggregate source span for unit"
            )

        affected_paths = sorted(
            {
                path_id
                for r in member_regions
                for path_id in r["affected_paths"]
            },
            key=lambda pid: (
                paths_by_id[pid].get("rank", 10**9),
                pid,
            ),
        )

        unit_integrity_records = [
            loc["integrity"]
            for pid in affected_paths
            for loc in paths_by_id[pid]["rtl_mapping"].get(
                "source_locations", []
            )
            if isinstance(loc.get("integrity"), dict)
        ]
        if unit_source is not None:
            unit_source["integrity"] = aggregate_integrity_records(
                unit_integrity_records
            )

        slacks = [
            paths_by_id[pid]["timing"]["slack_ns"]
            for pid in affected_paths
            if paths_by_id[pid]["timing"]["slack_ns"] is not None
        ]
        combs = [
            paths_by_id[pid]["timing"]["combinational_delay_ns"]
            for pid in affected_paths
            if paths_by_id[pid]["timing"]["combinational_delay_ns"] is not None
        ]
        depths = [
            paths_by_id[pid]["timing"]["logic_depth"]
            for pid in affected_paths
            if isinstance(
                paths_by_id[pid]["timing"]["logic_depth"],
                (int, float),
            )
        ]
        fanouts = [
            paths_by_id[pid]["timing"]["max_fanout"]
            for pid in affected_paths
            if isinstance(
                paths_by_id[pid]["timing"]["max_fanout"],
                (int, float),
            )
        ]

        transformation_union: List[str] = []
        for r in member_regions:
            for family in r["allowed_transformation_families"]:
                if family not in transformation_union:
                    transformation_union.append(family)

        shared_edges: List[Dict[str, Any]] = []
        for a_pos, i in enumerate(indices):
            for j in indices[a_pos + 1:]:
                key = (min(i, j), max(i, j))
                if key in overlap_evidence:
                    shared_edges.append({
                        "region_a": regions[i]["region_id"],
                        "region_b": regions[j]["region_id"],
                        "shared_source_intervals": overlap_evidence[key],
                    })

        patterns = sorted({
            r["structural_summary"]["pattern"]
            for r in member_regions
        })
        classes = sorted({
            r["structural_summary"]["class"]
            for r in member_regions
        })

        unit = {
            "unit_id": unit_id,
            "role": "atomic_optimizer_target",
            "eligibility": {
                "optimization_ready": len(unit_blockers) == 0,
                "blockers": unit_blockers,
            },
            "member_regions": [
                r["region_id"]
                for r in member_regions
            ],
            "coupling": {
                "is_multi_region": len(member_regions) > 1,
                "reason": (
                    "shared_or_overlapping_mapped_rtl_source"
                    if len(member_regions) > 1
                    else "single_independent_region"
                ),
                "overlap_edges": shared_edges,
            },
            "source": unit_source,
            "affected_paths": affected_paths,
            "artifact_groups": sorted({
                group
                for r in member_regions
                for group in r["artifact_groups"]
            }),
            "timing_summary": {
                "path_count": len(affected_paths),
                "violating_path_count": sum(
                    1
                    for pid in affected_paths
                    if paths_by_id[pid]["timing"]["slack_ns"] is not None
                    and paths_by_id[pid]["timing"]["slack_ns"] < 0
                ),
                "worst_slack_ns": min(slacks) if slacks else None,
                "best_slack_ns": max(slacks) if slacks else None,
                "max_combinational_delay_ns": (
                    max(combs) if combs else None
                ),
                "max_logic_depth": max(depths) if depths else None,
                "max_fanout": max(fanouts) if fanouts else None,
            },
            "structural_summary": {
                "classes": classes,
                "patterns": patterns,
                "member_region_count": len(member_regions),
            },
            "allowed_transformation_families": transformation_union,
        }
        units.append(unit)

        for r in member_regions:
            r["optimization_unit_id"] = unit_id

    # Any diagnostic region without a unit is a hard structural error.
    for r in regions:
        if not r.get("optimization_unit_id"):
            blockers.append(
                f"{r['region_id']} was not assigned to an optimization unit"
            )

    return units, blockers


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_identity(
    variant: str,
    manifest: Dict[str, Any],
    loaded_artifacts: Sequence[Tuple[Path, Dict[str, Any]]],
    fatal_errors: List[str],
) -> Dict[str, Optional[str]]:
    base = identity_of(manifest)

    if base["flow_variant"] and base["flow_variant"] != variant:
        append_unique(
            fatal_errors,
            f"Manifest flow_variant '{base['flow_variant']}' does not match requested '{variant}'",
        )

    for path, obj in loaded_artifacts:
        ident = identity_of(obj)

        if ident["flow_variant"] and ident["flow_variant"] != variant:
            append_unique(
                fatal_errors,
                f"{path.name}: flow_variant '{ident['flow_variant']}' != '{variant}'",
            )

        for field in ("design", "platform"):
            if base[field] and ident[field] and base[field] != ident[field]:
                append_unique(
                    fatal_errors,
                    f"{path.name}: {field} '{ident[field]}' != manifest '{base[field]}'",
                )

    return base


def validate_global_timing(
    baseline: Dict[str, Optional[float]],
    paths: Sequence[Dict[str, Any]],
    tolerance_ns: float,
    fatal_errors: List[str],
    warnings: List[str],
) -> Dict[str, Any]:
    slacks = [p["timing"]["slack_ns"] for p in paths if p["timing"]["slack_ns"] is not None]
    observed_worst = min(slacks) if slacks else None
    wns = baseline.get("wns_ns")
    tns = baseline.get("tns_ns")

    wns_matches = None
    if wns is not None and observed_worst is not None:
        wns_matches = math.isclose(wns, observed_worst, abs_tol=tolerance_ns)
        if not wns_matches:
            append_unique(
                fatal_errors,
                f"Global WNS ({wns} ns) does not match worst discovered/classified path "
                f"({observed_worst} ns) within {tolerance_ns} ns. "
                "Critical-path coverage may be incomplete or artifacts may be inconsistent.",
            )

    tns_sign_ok = None
    if wns is not None and tns is not None:
        if wns >= 0:
            tns_sign_ok = math.isclose(tns, 0.0, abs_tol=1e-12)
            if not tns_sign_ok:
                append_unique(
                    fatal_errors,
                    f"WNS is non-negative ({wns}) but TNS is non-zero ({tns}).",
                )
        else:
            tns_sign_ok = (tns <= 0)
            if not tns_sign_ok:
                append_unique(
                    fatal_errors,
                    f"WNS is negative ({wns}) but TNS is positive ({tns}).",
                )

    return {
        "observed_worst_slack_ns": observed_worst,
        "global_wns_matches_observed_worst_path": wns_matches,
        "tns_sign_consistent_with_wns": tns_sign_ok,
    }


# ---------------------------------------------------------------------------
# Prompt-context architecture helpers
# ---------------------------------------------------------------------------

def estimate_tokens_from_text(text: str, chars_per_token: float) -> int:
    """Conservative, deterministic token estimate used for preflight planning."""
    if chars_per_token <= 0:
        fatal("chars_per_token must be > 0")
    if not text:
        return 0
    return int(math.ceil(len(text) / chars_per_token))


def estimate_tokens_from_json(obj: Any, chars_per_token: float) -> int:
    """Estimate tokens from compact JSON serialization, not pretty-printing."""
    rendered = json.dumps(obj, separators=(",", ":"), sort_keys=True)
    return estimate_tokens_from_text(rendered, chars_per_token)


def discover_fixed_context_files(
    fixed_context_dir: Optional[Path],
    chars_per_token: float,
) -> Dict[str, Any]:
    """
    Discover immutable/fixed optimizer JSON inputs without copying their
    contents into every optimizer run artifact.

    The builder records only references, SHA-256 values and size estimates.
    The downstream prompt renderer may load the required files at LLM-call
    time.  No finished prompt is constructed here.
    """
    if fixed_context_dir is None:
        return {
            "configured": False,
            "directory": None,
            "files": [],
            "estimated_tokens": 0,
        }

    directory = Path(fixed_context_dir).expanduser().resolve()
    if not directory.is_dir():
        fatal(f"Fixed-context directory not found: {directory}")

    files: List[Dict[str, Any]] = []
    total_tokens = 0

    for path in sorted(directory.glob("*.json")):
        # Validate that every fixed context artifact is actually JSON.
        load_json(path)
        raw = path.read_text(encoding="utf-8")
        estimate = estimate_tokens_from_text(raw, chars_per_token)
        total_tokens += estimate
        files.append({
            "name": path.stem,
            "path": str(path),
            "sha256": sha256_file(path),
            "size_bytes": path.stat().st_size,
            "estimated_tokens": estimate,
        })

    return {
        "configured": True,
        "directory": str(directory),
        "files": files,
        "estimated_tokens": total_tokens,
    }


def compact_source_snapshot(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """Keep one authoritative module snapshot for design-fixed LLM context."""
    return {
        "source_id": snapshot.get("source_id"),
        "file": snapshot.get("file"),
        "sha256": snapshot.get("sha256"),
        "module": snapshot.get("module"),
        "module_line_start": snapshot.get("module_line_start"),
        "module_line_end": snapshot.get("module_line_end"),
        "module_text": snapshot.get("module_text"),
    }


def compact_region_evidence(region: Dict[str, Any]) -> Dict[str, Any]:
    """Distill raw region evidence into decision-relevant optimizer evidence."""
    source = region.get("source") or {}
    return {
        "region_id": region.get("region_id"),
        "optimization_unit_id": region.get("optimization_unit_id"),
        "diagnostic_ready": (
            region.get("eligibility", {}).get("diagnostic_ready")
        ),
        "affected_path_count": len(region.get("affected_paths") or []),
        "timing": {
            "worst_slack_ns": region.get("timing_summary", {}).get("worst_slack_ns"),
            "max_combinational_delay_ns": region.get("timing_summary", {}).get("max_combinational_delay_ns"),
            "max_logic_depth": region.get("timing_summary", {}).get("max_logic_depth"),
            "max_fanout": region.get("timing_summary", {}).get("max_fanout"),
        },
        "structure": region.get("structural_summary"),
        "source_locations": [
            {
                "file": loc.get("file"),
                "module": loc.get("module"),
                "line_start": loc.get("line_start"),
                "line_end": loc.get("line_end"),
                "operator_roles": loc.get("operator_roles", []),
            }
            for loc in (source.get("source_locations") or [])
        ],
        "allowed_transformation_families": region.get(
            "allowed_transformation_families", []
        ),
    }


def compact_unit_evidence(unit: Dict[str, Any]) -> Dict[str, Any]:
    """
    Produce the compact unit view intended for downstream prompt rendering.
    Raw path objects remain in optimizer_input.json and are not copied here.
    """
    source = unit.get("source") or {}
    locations = source.get("source_locations") or []

    editable_ranges: List[List[int]] = []
    seen_ranges = set()
    for loc in locations:
        lo = loc.get("line_start")
        hi = loc.get("line_end")
        if not isinstance(lo, int) or not isinstance(hi, int):
            continue
        key = (lo, hi)
        if key in seen_ranges:
            continue
        seen_ranges.add(key)
        editable_ranges.append([lo, hi])
    editable_ranges.sort()

    return {
        "unit_id": unit.get("unit_id"),
        "optimization_ready": (
            unit.get("eligibility", {}).get("optimization_ready")
        ),
        "blockers": unit.get("eligibility", {}).get("blockers", []),
        "target": {
            "file": source.get("file"),
            "module": source.get("module"),
            "original_sha256": source.get("sha256"),
            "module_line_start": source.get("module_line_start"),
            "module_line_end": source.get("module_line_end"),
        },
        "editable_line_ranges": editable_ranges,
        "member_regions": unit.get("member_regions", []),
        "critical_evidence": {
            "affected_path_count": len(unit.get("affected_paths") or []),
            "worst_slack_ns": unit.get("timing_summary", {}).get("worst_slack_ns"),
            "max_combinational_delay_ns": unit.get("timing_summary", {}).get("max_combinational_delay_ns"),
            "max_logic_depth": unit.get("timing_summary", {}).get("max_logic_depth"),
            "max_fanout": unit.get("timing_summary", {}).get("max_fanout"),
            "classes": unit.get("structural_summary", {}).get("classes", []),
            "patterns": unit.get("structural_summary", {}).get("patterns", []),
        },
        "allowed_transformation_families": unit.get(
            "allowed_transformation_families", []
        ),
    }


def build_design_context(optimizer_input: Dict[str, Any]) -> Dict[str, Any]:
    """
    Build the design-fixed context artifact.

    This is intentionally much smaller than optimizer_input.json.  It contains
    the baseline, source snapshots and distilled unit/region evidence required
    to prepare future LLM iterations while leaving full raw analysis in the
    trusted optimizer-input artifact.
    """
    provenance = optimizer_input.get("provenance") or {}
    return {
        "schema": DESIGN_CONTEXT_SCHEMA,
        "source_optimizer_input_schema": optimizer_input.get("schema_version"),
        "design": optimizer_input.get("design"),
        "objective": optimizer_input.get("objective"),
        "baseline_metrics": optimizer_input.get("baseline_metrics"),
        "constraints": optimizer_input.get("constraints"),
        "sources": [
            compact_source_snapshot(s)
            for s in (provenance.get("source_snapshots") or [])
            if isinstance(s, dict)
        ],
        "optimization_regions": [
            compact_region_evidence(r)
            for r in (optimizer_input.get("optimization_regions") or [])
            if isinstance(r, dict)
        ],
        "optimization_units": [
            compact_unit_evidence(u)
            for u in (optimizer_input.get("optimization_units") or [])
            if isinstance(u, dict)
        ],
    }


def build_context_budget_policy(args: argparse.Namespace) -> Dict[str, Any]:
    """Describe the exact overflow policy that the later renderer must enforce."""
    return {
        "service_model_token_budget": args.service_model_token_budget,
        "soft_input_token_target": args.soft_input_token_target,
        "hard_input_token_limit": args.hard_input_token_limit,
        "completion_reserve_tokens": args.completion_reserve_tokens,
        "estimator": {
            "method": "ceil(characters/chars_per_token)",
            "chars_per_token": args.estimated_chars_per_token,
        },
        "sections": [
            "fixed_instructions",
            "design_context",
            "current_iteration",
            "history_summary",
            "response_contract",
        ],
        "priority_policy": {
            "never_remove": [
                "target_identity",
                "target_rtl_required_for_modification",
                "source_sha256",
                "editable_ranges",
                "current_objective",
                "required_or_eligible_pattern",
                "response_contract",
            ],
            "compress_if_needed": [
                "critical_path_evidence",
                "previous_candidate_results",
                "detailed_structural_evidence",
                "multiple_similar_timing_paths",
            ],
            "omit_when_unnecessary": [
                "raw_timing_reports",
                "full_dependency_graphs",
                "complete_old_iteration_records",
                "repeated_mappings",
                "verbose_diagnostics",
            ],
        },
        "overflow_sequence": [
            "render_current_decision_relevant_view",
            "measure_sections",
            "if_above_soft_limit_compact_optional_evidence_deterministically",
            "measure_again",
            "reject_locally_if_above_hard_limit_or_total_budget",
        ],
        "hard_preflight_rule": (
            "estimated_input_tokens + reserved_completion_tokens "
            "<= configured_service_model_token_budget"
        ),
        "cumulative_token_tracking": {
            "enabled": True,
            "diagnostic_only": True,
            "must_not_be_treated_as_current_context_window_usage": True,
        },
    }


def attach_context_architecture(
    *,
    optimizer_input: Dict[str, Any],
    design_context_path: Path,
    fixed_context: Dict[str, Any],
    history_path: Path,
    budget_policy: Dict[str, Any],
    chars_per_token: float,
) -> Dict[str, Any]:
    """Attach references only; do not embed a rendered LLM prompt."""
    design_context = load_json(design_context_path)
    design_context_tokens = estimate_tokens_from_json(
        design_context,
        chars_per_token,
    )

    optimizer_input["context_architecture"] = {
        "version": CONTEXT_ARCHITECTURE_VERSION,
        "fixed_context": fixed_context,
        "design_context": {
            "path": str(design_context_path),
            "sha256": sha256_file(design_context_path),
            "estimated_tokens": design_context_tokens,
        },
        "iteration_history": {
            "path": str(history_path),
            "format": "jsonl",
            "append_only": True,
            "full_history_is_not_sent_to_llm": True,
            "llm_history_policy": (
                "send only current iteration state plus a compact summary "
                "of directly relevant previous results"
            ),
        },
        "prompt_rendering": {
            "render_immediately_before_llm_call": True,
            "persist_finished_prompt_in_optimizer_input": False,
            "raw_analysis_remains_in_optimizer_input": True,
            "use_design_context_for_distilled_llm_evidence": True,
            "avoid_duplicate_facts_in_rendered_prompt": True,
        },
        "context_budget_policy": budget_policy,
        "current_known_section_estimates": {
            "fixed_instructions_tokens": fixed_context.get(
                "estimated_tokens", 0
            ),
            "design_context_tokens": design_context_tokens,
            "current_iteration_tokens": None,
            "history_summary_tokens": None,
            "response_contract_tokens": None,
            "final_rendered_input_tokens": None,
        },
    }
    return optimizer_input


# ---------------------------------------------------------------------------
# Main builder
# ---------------------------------------------------------------------------

def build(args: argparse.Namespace) -> Tuple[Dict[str, Any], int]:
    variant = args.flow_variant
    ai_runs_dir = args.ai_runs_dir.resolve()
    script_dir = Path(__file__).resolve().parent

    fatal_errors: List[str] = []
    warnings: List[str] = []

    artifacts = discover_artifacts(ai_runs_dir, variant)

    # Optional metrics override.
    if args.metrics_json is not None:
        artifacts["metrics"] = args.metrics_json.resolve()

    # Basic required artifacts.
    if not artifacts["manifest"].exists():
        append_unique(fatal_errors, f"Run manifest not found: {artifacts['manifest']}")
    if not artifacts["metrics"].exists():
        append_unique(fatal_errors, f"Metrics JSON not found: {artifacts['metrics']}")
    if not artifacts["rtl_graph"].exists():
        append_unique(fatal_errors, f"RTL graph not found: {artifacts['rtl_graph']}")
    if not artifacts["timing"]:
        append_unique(fatal_errors, f"No timing path JSONs found for variant {variant}")
    if not artifacts["mapped"]:
        append_unique(fatal_errors, f"No RTL-mapped path JSONs found for variant {variant}")

    auto_classify_missing(
        artifacts,
        script_dir=script_dir,
        auto_classify=args.auto_classify,
        fatal_errors=fatal_errors,
        warnings=warnings,
    )

    timing_groups = set(artifacts["timing"])
    mapped_groups = set(artifacts["mapped"])
    classified_groups = set(artifacts["classified"])

    missing_mapped = sorted(timing_groups - mapped_groups)
    missing_classified = sorted(mapped_groups - classified_groups)
    orphan_mapped = sorted(mapped_groups - timing_groups)
    orphan_classified = sorted(classified_groups - mapped_groups)

    if missing_mapped:
        append_unique(
            fatal_errors,
            "Timing path groups without RTL mapping: " + ", ".join(missing_mapped),
        )
    if missing_classified:
        append_unique(
            fatal_errors,
            "RTL-mapped groups without classification: " + ", ".join(missing_classified),
        )
    if orphan_mapped:
        append_unique(
            fatal_errors,
            "RTL-mapped groups without timing-path source artifact: " + ", ".join(orphan_mapped),
        )
    if orphan_classified:
        append_unique(
            fatal_errors,
            "Classified groups without RTL-mapped source artifact: " + ", ".join(orphan_classified),
        )

    # Load what exists so a diagnostic JSON can still be produced.
    manifest = load_json(artifacts["manifest"]) if artifacts["manifest"].exists() else {}
    metrics = load_json(artifacts["metrics"]) if artifacts["metrics"].exists() else {}
    rtl_graph = load_json(artifacts["rtl_graph"]) if artifacts["rtl_graph"].exists() else {}

    loaded_for_identity: List[Tuple[Path, Dict[str, Any]]] = []
    if manifest:
        loaded_for_identity.append((artifacts["manifest"], manifest))
    if metrics:
        loaded_for_identity.append((artifacts["metrics"], metrics))
    if rtl_graph:
        loaded_for_identity.append((artifacts["rtl_graph"], rtl_graph))

    classified_objs: Dict[str, Dict[str, Any]] = {}
    mapped_objs: Dict[str, Dict[str, Any]] = {}
    timing_objs: Dict[str, Dict[str, Any]] = {}

    for group, p in sorted(artifacts["timing"].items()):
        try:
            obj = load_json(p)
            timing_objs[group] = obj
            loaded_for_identity.append((p, obj))
        except Exception as e:
            append_unique(fatal_errors, str(e))

    for group, p in sorted(artifacts["mapped"].items()):
        try:
            obj = load_json(p)
            mapped_objs[group] = obj
            loaded_for_identity.append((p, obj))
        except Exception as e:
            append_unique(fatal_errors, str(e))

    for group, p in sorted(artifacts["classified"].items()):
        try:
            obj = load_json(p)
            classified_objs[group] = obj
            loaded_for_identity.append((p, obj))
        except Exception as e:
            append_unique(fatal_errors, str(e))

    design_ident = validate_identity(
        variant,
        manifest,
        loaded_for_identity,
        fatal_errors,
    )
    design = design_ident.get("design")
    platform = design_ident.get("platform")

    baseline = extract_baseline_metrics(metrics)
    for key, value in baseline.items():
        if value is None:
            append_unique(fatal_errors, f"Required baseline metric is missing: {key}")

    # Normalize all classified paths.
    source_cache: Dict[Tuple[str, int, int, str, str], Dict[str, Any]] = {}
    paths: List[Dict[str, Any]] = []

    for group in sorted(classified_objs):
        obj = classified_objs[group]
        raw_paths = obj.get("paths")
        if not isinstance(raw_paths, list) or not raw_paths:
            append_unique(fatal_errors, f"{group}: classified JSON contains no paths")
            continue

        for raw in raw_paths:
            if not isinstance(raw, dict):
                append_unique(fatal_errors, f"{group}: classified path entry is not an object")
                continue
            paths.append(
                normalize_path(
                    raw=raw,
                    artifact_group=group,
                    classified_path=artifacts["classified"][group],
                    design=design,
                    context_lines=args.context_lines,
                    minimum_confidence=args.minimum_confidence,
                    fatal_errors=fatal_errors,
                    warnings=warnings,
                    source_cache=source_cache,
                )
            )

    # Stable global rank: worst slack first, then path ID.
    def rank_key(p: Dict[str, Any]) -> Tuple[float, str]:
        s = p["timing"]["slack_ns"]
        return (float("inf") if s is None else s, p["path_id"])

    paths.sort(key=rank_key)
    for i, p in enumerate(paths, start=1):
        p["rank"] = i

    # Ensure classifier path counts match mapped path counts group by group.
    for group in sorted(timing_groups | mapped_groups | classified_groups):
        t_count = len(timing_objs.get(group, {}).get("paths", [])) if isinstance(timing_objs.get(group, {}).get("paths", []), list) else None
        m_count = len(mapped_objs.get(group, {}).get("paths", [])) if isinstance(mapped_objs.get(group, {}).get("paths", []), list) else None
        c_count = len(classified_objs.get(group, {}).get("paths", [])) if isinstance(classified_objs.get(group, {}).get("paths", []), list) else None

        if t_count is not None and m_count is not None and t_count != m_count:
            append_unique(
                fatal_errors,
                f"{group}: timing path count ({t_count}) != mapped path count ({m_count})",
            )
        if m_count is not None and c_count is not None and m_count != c_count:
            append_unique(
                fatal_errors,
                f"{group}: mapped path count ({m_count}) != classified path count ({c_count})",
            )

    clock_names = sorted({
        p["timing"]["path_group"]
        for p in paths
        if p["timing"]["path_group"]
    })
    clocks, clock_errors, clock_warnings = resolve_clocks(
        metrics=metrics,
        manifest=manifest,
        manifest_path=artifacts["manifest"],
        clock_names=clock_names,
        design=design,
        platform=platform,
    )
    for e in clock_errors:
        append_unique(fatal_errors, e)
    for w in clock_warnings:
        append_unique(warnings, w)

    policy = {
        "preserve_functionality": True,
        "preserve_cycle_latency": not args.allow_latency_change,
        "allow_pipeline": args.allow_pipeline,
        "allow_latency_change": args.allow_latency_change,
        "allow_register_retiming": args.allow_retiming,
        "allow_boolean_restructuring": True,
        "allow_mux_restructuring": True,
        "allow_arithmetic_restructuring": True,
        "source": "build_optimizer_input.py_v2_2_1_policy",
    }

    if policy["allow_pipeline"] and not policy["allow_latency_change"]:
        append_unique(
            fatal_errors,
            "Invalid optimization policy: pipelining is enabled while latency changes are forbidden.",
        )

    regions, region_blockers = build_regions(paths, policy, args.context_lines)
    for b in region_blockers:
        append_unique(fatal_errors, b)

    paths_by_id = {p["path_id"]: p for p in paths}
    optimization_units, unit_blockers = build_optimization_units(
        regions=regions,
        paths_by_id=paths_by_id,
        policy=policy,
        context_lines=args.context_lines,
    )
    for b in unit_blockers:
        append_unique(fatal_errors, b)

    global_timing_validation = validate_global_timing(
        baseline=baseline,
        paths=paths,
        tolerance_ns=args.wns_tolerance_ns,
        fatal_errors=fatal_errors,
        warnings=warnings,
    )

    # Coverage.
    timing_path_total = sum(
        len(obj.get("paths", []))
        for obj in timing_objs.values()
        if isinstance(obj.get("paths"), list)
    )
    mapped_path_total = sum(
        len(obj.get("paths", []))
        for obj in mapped_objs.values()
        if isinstance(obj.get("paths"), list)
    )
    classified_path_total = sum(
        len(obj.get("paths", []))
        for obj in classified_objs.values()
        if isinstance(obj.get("paths"), list)
    )

    coverage = {
        "timing_path_groups_discovered": len(timing_groups),
        "mapped_path_groups": len(mapped_groups),
        "classified_path_groups": len(classified_groups),
        "timing_path_group_names": sorted(timing_groups),
        "mapped_path_group_names": sorted(mapped_groups),
        "classified_path_group_names": sorted(classified_groups),
        "timing_paths_total": timing_path_total,
        "mapped_paths_total": mapped_path_total,
        "classified_paths_total": classified_path_total,
        "mapping_coverage": (
            mapped_path_total / timing_path_total
            if timing_path_total else 0.0
        ),
        "classification_coverage": (
            classified_path_total / mapped_path_total
            if mapped_path_total else 0.0
        ),
    }

    # Validation check booleans.
    checks = {
        "artifact_completeness": (
            bool(timing_groups)
            and timing_groups == mapped_groups == classified_groups
            and artifacts["manifest"].exists()
            and artifacts["metrics"].exists()
            and artifacts["rtl_graph"].exists()
        ),
        "variant_and_design_consistency": not any(
            "flow_variant" in e or ": design " in e or ": platform " in e
            for e in fatal_errors
        ),
        "metrics_complete": all(v is not None for v in baseline.values()),
        "clock_constraints_complete": (
            bool(clock_names)
            and {c["name"] for c in clocks} == set(clock_names)
            and all(c.get("period_ns") is not None for c in clocks)
        ),
        "path_counts_consistent": (
            timing_path_total == mapped_path_total == classified_path_total
            and timing_path_total > 0
        ),
        "path_timing_complete": all(
            p["timing"]["slack_ns"] is not None
            and p["timing"]["arrival_ns"] is not None
            and p["timing"]["required_ns"] is not None
            and p["timing"]["combinational_delay_ns"] is not None
            and p["timing"]["logic_depth"] is not None
            and p["timing"]["max_fanout"] is not None
            for p in paths
        ) if paths else False,
        "mapping_complete": all(
            p["rtl_mapping"]["status"] == "mapped"
            and p["rtl_mapping"]["has_user_rtl_source"]
            for p in paths
        ) if paths else False,
        "classification_complete": all(
            p["classification"]["status"] == "classified"
            and p["classification"]["class"] != "unknown"
            and p["classification"]["pattern"] != "unknown"
            and min_confidence_ok(p["classification"]["confidence"], args.minimum_confidence)
            for p in paths
        ) if paths else False,
        "source_resolved": all(
            p["rtl_mapping"]["source"] is not None
            and bool(p["rtl_mapping"].get("source_locations"))
            for p in paths
        ) if paths else False,
        "source_operator_coverage_complete": all(
            p["consistency"]["source_operator_coverage_complete"]
            for p in paths
        ) if paths else False,
        "source_not_modified_after_analysis": all(
            bool(p["rtl_mapping"].get("source_locations"))
            and all(
                not loc["integrity"]["source_newer_than_analysis_artifact"]
                for loc in p["rtl_mapping"]["source_locations"]
            )
            for p in paths
        ) if paths else False,
        "operator_consistency": all(
            p["consistency"]["operator_counts_match"]
            for p in paths
        ) if paths else False,
        "timing_arithmetic_consistency": all(
            p["consistency"]["timing_identity_ok"] is True
            for p in paths
        ) if paths else False,
        "global_wns_consistency": (
            global_timing_validation["global_wns_matches_observed_worst_path"] is True
        ),
        "region_grouping_consistent": all(
            r["grouping_evidence"]["grouping_confidence"] == "high"
            and r["eligibility"]["diagnostic_ready"]
            for r in regions
        ) if regions else False,
        "all_regions_assigned_to_optimization_units": all(
            bool(r.get("optimization_unit_id"))
            for r in regions
        ) if regions else False,
        "optimization_units_consistent": all(
            u["eligibility"]["optimization_ready"]
            and u.get("source") is not None
            and not u["source"]["integrity"].get(
                "source_newer_than_any_analysis_artifact", False
            )
            for u in optimization_units
        ) if optimization_units else False,
        "at_least_one_optimization_ready_unit": any(
            u["eligibility"]["optimization_ready"]
            for u in optimization_units
        ),
    }

    for name, ok in checks.items():
        if not ok:
            append_unique(fatal_errors, f"Validation check failed: {name}")

    optimizer_ready = len(fatal_errors) == 0 and all(checks.values())

    # Artifact provenance list.
    provenance_artifacts: List[Dict[str, Any]] = []
    provenance_artifacts.append(artifact_record(artifacts["manifest"], "run_manifest"))
    provenance_artifacts.append(artifact_record(artifacts["metrics"], "metrics"))
    provenance_artifacts.append(artifact_record(artifacts["rtl_graph"], "rtl_graph"))
    for g, p in sorted(artifacts["timing"].items()):
        provenance_artifacts.append(artifact_record(p, "timing_paths", g))
    for g, p in sorted(artifacts["mapped"].items()):
        provenance_artifacts.append(artifact_record(p, "rtl_mapped", g))
    for g, p in sorted(artifacts["classified"].items()):
        provenance_artifacts.append(artifact_record(p, "classified", g))

    # Deduplicated source snapshots.
    source_snapshots: List[Dict[str, Any]] = []
    source_seen = set()
    for p in paths:
        for loc in p["rtl_mapping"].get("source_locations", []):
            src = p["rtl_mapping"]["source"]
            if not src:
                continue
            key = (src["file"], src["sha256"], src["module"])
            if key in source_seen:
                continue
            source_seen.add(key)
            source_snapshots.append({
                "source_id": f"source_{len(source_snapshots):03d}",
                "file": src["file"],
                "sha256": src["sha256"],
                "module": src["module"],
                "module_line_start": src["module_line_start"],
                "module_line_end": src["module_line_end"],
                "module_text": src["module_text"],
                "integrity": src["integrity"],
            })

    wns = baseline.get("wns_ns")
    objective_goal = (
        "fix_setup_violations"
        if wns is not None and wns < 0
        else "improve_timing_margin"
    )

    output = {
        "schema_version": SCHEMA_VERSION,
        "validation": {
            "optimizer_ready": optimizer_ready,
            "fatal_errors": fatal_errors,
            "warnings": warnings,
            "checks": checks,
            "global_timing_consistency": global_timing_validation,
        },
        "design": {
            "name": design,
            "platform": platform,
            "flow_variant": variant,
        },
        "provenance": {
            "artifacts": provenance_artifacts,
            "source_snapshots": source_snapshots,
            "source_freshness_note": (
                "When an upstream source SHA-256 exists, it is checked cryptographically. "
                "Otherwise v2.2.1 fails if the RTL source is newer than each contributing classification artifact, "
                "then embeds a build-time SHA-256 and exact source snapshot for downstream use."
            ),
        },
        "objective": {
            "primary": "timing",
            "goal": objective_goal,
            "preserve_functionality": True,
            "secondary_objectives": [
                "minimize_area_regression",
                "minimize_power_regression",
            ],
        },
        "baseline_metrics": baseline,
        "constraints": {
            "clocks": clocks,
            "optimization_policy": policy,
        },
        "coverage": coverage,
        "critical_paths": paths,
        "optimization_regions": regions,
        "optimization_units": optimization_units,
        "summary": {
            "critical_path_count": len(paths),
            "optimization_region_count": len(regions),
            "diagnostic_region_count": len(regions),
            "optimization_unit_count": len(optimization_units),
            "optimization_ready_unit_count": sum(
                1
                for u in optimization_units
                if u["eligibility"]["optimization_ready"]
            ),
            "violating_path_count": sum(
                1 for p in paths
                if p["timing"]["slack_ns"] is not None
                and p["timing"]["slack_ns"] < 0
            ),
            "mapped_path_count": sum(
                1 for p in paths if p["rtl_mapping"]["status"] == "mapped"
            ),
            "classified_path_count": sum(
                1 for p in paths if p["classification"]["status"] == "classified"
            ),
        },
    }

    return output, (0 if optimizer_ready else 2)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Build validated optimizer_input.json v2.2.1 for one ORFS FLOW_VARIANT."
    )
    p.add_argument(
        "--flow-variant",
        required=True,
        help="Exact FLOW_VARIANT, e.g. run_20260905_234646_9cbc56",
    )
    p.add_argument(
        "--ai-runs-dir",
        type=Path,
        default=Path("ai_runs"),
        help="Directory containing run artifacts (default: ai_runs)",
    )
    p.add_argument(
        "--metrics-json",
        type=Path,
        default=None,
        help="Optional explicit metrics JSON. Default: ai_runs/<variant>_metrics.json",
    )
    p.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output path. Default: ai_runs/<variant>_optimizer_input.json",
    )
    p.add_argument(
        "--context-lines",
        type=int,
        default=12,
        help="RTL source context lines on each side of mapped target (default: 12)",
    )
    p.add_argument(
        "--minimum-confidence",
        choices=("low", "medium", "high"),
        default="high",
        help="Minimum classification confidence for optimizer readiness (default: high)",
    )
    p.add_argument(
        "--wns-tolerance-ns",
        type=float,
        default=NS_TOLERANCE_DEFAULT,
        help="Allowed global-WNS vs observed-worst-path mismatch in ns (default: 0.005)",
    )
    p.add_argument(
        "--no-auto-classify",
        dest="auto_classify",
        action="store_false",
        help="Do not invoke classify_path.py for mapped groups missing classification.",
    )
    p.set_defaults(auto_classify=True)

    # Safe default policy: combinational/cycle-equivalent rewrites only.
    p.add_argument(
        "--allow-latency-change",
        action="store_true",
        help="Permit transformations that change cycle latency.",
    )
    p.add_argument(
        "--allow-pipeline",
        action="store_true",
        help="Permit pipelining. Requires --allow-latency-change.",
    )
    p.add_argument(
        "--allow-retiming",
        action="store_true",
        help="Permit register retiming.",
    )

    # Prompt-context architecture.  These options do not construct a prompt;
    # they prepare the stable design context and the budget policy consumed by
    # the downstream prompt renderer/orchestrator.
    p.add_argument(
        "--fixed-context-dir",
        type=Path,
        default=None,
        help=(
            "Optional directory containing fixed optimizer JSON files. "
            "Only references/SHA-256 values are stored in each run."
        ),
    )
    p.add_argument(
        "--design-context-output",
        type=Path,
        default=None,
        help=(
            "Compact design-fixed context output. Default: "
            "ai_runs/<variant>_design_context.json"
        ),
    )
    p.add_argument(
        "--history-file",
        type=Path,
        default=None,
        help=(
            "Append-only optimizer history JSONL path. The builder records "
            "the path but does not append iteration results."
        ),
    )
    p.add_argument(
        "--service-model-token-budget",
        type=int,
        default=DEFAULT_SERVICE_MODEL_TOKEN_BUDGET,
        help="Configured total service/model token budget (default: 8000).",
    )
    p.add_argument(
        "--soft-input-token-target",
        type=int,
        default=DEFAULT_SOFT_INPUT_TOKEN_TARGET,
        help="Soft input target before deterministic compaction (default: 5000).",
    )
    p.add_argument(
        "--hard-input-token-limit",
        type=int,
        default=DEFAULT_HARD_INPUT_TOKEN_LIMIT,
        help="Hard rendered-input limit (default: 6000).",
    )
    p.add_argument(
        "--completion-reserve-tokens",
        type=int,
        default=DEFAULT_COMPLETION_RESERVE_TOKENS,
        help="Reserved completion budget (default: 2000).",
    )
    p.add_argument(
        "--estimated-chars-per-token",
        type=float,
        default=DEFAULT_ESTIMATED_CHARS_PER_TOKEN,
        help="Deterministic token-estimation ratio (default: 4.0 chars/token).",
    )

    return p.parse_args()


def main() -> int:
    args = parse_args()
    if args.context_lines < 0:
        print("ERROR: --context-lines must be >= 0", file=sys.stderr)
        return 2
    if args.wns_tolerance_ns < 0:
        print("ERROR: --wns-tolerance-ns must be >= 0", file=sys.stderr)
        return 2
    if args.estimated_chars_per_token <= 0:
        print("ERROR: --estimated-chars-per-token must be > 0", file=sys.stderr)
        return 2
    if args.service_model_token_budget <= 0:
        print("ERROR: --service-model-token-budget must be > 0", file=sys.stderr)
        return 2
    if args.soft_input_token_target <= 0:
        print("ERROR: --soft-input-token-target must be > 0", file=sys.stderr)
        return 2
    if args.hard_input_token_limit <= 0:
        print("ERROR: --hard-input-token-limit must be > 0", file=sys.stderr)
        return 2
    if args.completion_reserve_tokens < 0:
        print("ERROR: --completion-reserve-tokens must be >= 0", file=sys.stderr)
        return 2
    if args.soft_input_token_target > args.hard_input_token_limit:
        print(
            "ERROR: soft input token target cannot exceed hard input limit",
            file=sys.stderr,
        )
        return 2
    if (
        args.hard_input_token_limit + args.completion_reserve_tokens
        > args.service_model_token_budget
    ):
        print(
            "ERROR: hard input limit + completion reserve exceeds "
            "configured service/model token budget",
            file=sys.stderr,
        )
        return 2

    if args.output is None:
        args.output = (
            args.ai_runs_dir / f"{args.flow_variant}_optimizer_input.json"
        )
    args.output = args.output.resolve()

    if args.design_context_output is None:
        args.design_context_output = (
            args.ai_runs_dir / f"{args.flow_variant}_design_context.json"
        )
    args.design_context_output = args.design_context_output.resolve()

    if args.history_file is None:
        args.history_file = (
            args.ai_runs_dir / f"{args.flow_variant}_optimizer_history.jsonl"
        )
    args.history_file = args.history_file.resolve()

    try:
        output, rc = build(args)

        # Separate stable design evidence from the full trusted analysis.
        design_context = build_design_context(output)
        write_json(args.design_context_output, design_context)

        # Fixed context is referenced, never copied into each run artifact.
        fixed_context = discover_fixed_context_files(
            args.fixed_context_dir,
            args.estimated_chars_per_token,
        )
        budget_policy = build_context_budget_policy(args)

        attach_context_architecture(
            optimizer_input=output,
            design_context_path=args.design_context_output,
            fixed_context=fixed_context,
            history_path=args.history_file,
            budget_policy=budget_policy,
            chars_per_token=args.estimated_chars_per_token,
        )

        write_json(args.output, output)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    validation = output["validation"]
    summary = output["summary"]
    baseline = output["baseline_metrics"]
    coverage = output["coverage"]

    print()
    print("=" * 72)
    print("Optimizer Input Builder v2.2.1")
    print("=" * 72)
    print(f"Design       : {output['design']['name']}")
    print(f"Platform     : {output['design']['platform']}")
    print(f"Flow Variant : {output['design']['flow_variant']}")
    print(f"Schema       : {output['schema_version']}")
    print()
    print("Coverage")
    print("-" * 72)
    print(f"Path groups  : timing={coverage['timing_path_groups_discovered']} "
          f"mapped={coverage['mapped_path_groups']} "
          f"classified={coverage['classified_path_groups']}")
    print(f"Paths        : timing={coverage['timing_paths_total']} "
          f"mapped={coverage['mapped_paths_total']} "
          f"classified={coverage['classified_paths_total']}")
    print()
    print("Baseline")
    print("-" * 72)
    print(f"WNS          : {baseline.get('wns_ns')} ns")
    print(f"TNS          : {baseline.get('tns_ns')} ns")
    print(f"Area         : {baseline.get('area_um2')} um^2")
    print(f"Power        : {baseline.get('power_mw')} mW")
    print()
    print("Optimization")
    print("-" * 72)
    print(f"Diagnostic regions : {summary['diagnostic_region_count']}")
    print(f"Optimization units : {summary['optimization_unit_count']}")
    print(f"Ready units        : {summary['optimization_ready_unit_count']}")
    for region in output["optimization_regions"]:
        print(
            f"{region['region_id']}: "
            f"{region['structural_summary']['pattern']} | "
            f"paths={region['timing_summary']['path_count']} | "
            f"unit={region['optimization_unit_id']} | "
            f"diagnostic_ready={region['eligibility']['diagnostic_ready']}"
        )
    print("Units")
    for unit in output["optimization_units"]:
        print(
            f"{unit['unit_id']}: "
            f"regions={len(unit['member_regions'])} | "
            f"paths={unit['timing_summary']['path_count']} | "
            f"worst_slack={unit['timing_summary']['worst_slack_ns']} | "
            f"ready={unit['eligibility']['optimization_ready']}"
        )
    print()
    print("Validation")
    print("-" * 72)
    print(f"OPTIMIZER READY: {validation['optimizer_ready']}")
    if validation["fatal_errors"]:
        print("Fatal errors:")
        for e in validation["fatal_errors"]:
            print(f"  - {e}")
    if validation["warnings"]:
        print("Warnings:")
        for w in validation["warnings"]:
            print(f"  - {w}")
    print()
    print("Context architecture")
    print("-" * 72)
    context_arch = output["context_architecture"]
    estimates = context_arch["current_known_section_estimates"]
    budget = context_arch["context_budget_policy"]
    print(f"Design context : {args.design_context_output}")
    print(f"History file   : {args.history_file} (append-only; not created here)")
    print(f"Fixed context  : {len(context_arch['fixed_context']['files'])} JSON file(s)")
    print(f"Design est.    : {estimates['design_context_tokens']} tokens")
    print(
        f"Token policy   : soft={budget['soft_input_token_target']} "
        f"hard={budget['hard_input_token_limit']} "
        f"reserve={budget['completion_reserve_tokens']} "
        f"total={budget['service_model_token_budget']}"
    )
    print()
    print(f"JSON written to:\n{args.output}")
    print("=" * 72)

    return rc


if __name__ == "__main__":
    raise SystemExit(main())
