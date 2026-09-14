#!/usr/bin/env python3
"""Auditable compatibility transforms for post-synthesis STA netlists.

This module never edits source RTL.  It only operates on Yosys-emitted
post-synthesis structural Verilog immediately before OpenSTA/OpenROAD consumes
that netlist.  The original synthesis artifact is preserved separately and
remains authoritative for normalized-synthesis hashing.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

SCHEMA = "sta_netlist_compat/1"
TRANSFORM_VERSION = "v1.4.2"
SIGNED_DECL_RE = re.compile(r"\b(input|output|inout|wire|reg)(\s+)signed\b")
BEHAVIORAL_RE = re.compile(r"\b(always|always_ff|always_comb|always_latch|initial|case|casex|casez|function|task)\b")
ASSIGN_RE = re.compile(r"\bassign\b(?P<expr>.*?);", re.S)
# Operators whose interpretation can depend on signedness, width extension, or
# expression semantics.  A mapped structural netlist should not need them.
UNSAFE_ASSIGN_RE = re.compile(r"(?<![<>=!])(?:\*|/|\+|-(?!>)|>>>|<<<|>>|<<|<=|>=|<|>|\$signed\s*\(|\$unsigned\s*\()")


class StaNetlistCompatError(RuntimeError):
    pass


def orfs_yosys_netlist_target(flow_dir: Path, platform: str, design_id: str, flow_variant: str) -> Path:
    """Return the exact ORFS file target that is produced by Yosys only.

    The phony ``synth`` target is intentionally not used because it continues
    into OpenROAD ``synth_odb`` and therefore gives no pre-OpenSTA interception
    point for compatibility transforms.
    """
    return (
        Path(flow_dir).resolve()
        / "results"
        / str(platform)
        / str(design_id)
        / str(flow_variant)
        / "1_2_yosys.v"
    )


def orfs_make_target(flow_dir: Path, artifact: Path) -> str:
    """Return an ORFS Make target relative to ``flow_dir``.

    ORFS file rules are declared as paths such as
    ``results/<platform>/<design>/<variant>/1_2_yosys.v``.  Passing the
    absolute filesystem path to GNU Make does not match those rules and causes
    ``No rule to make target ...``.  Python still uses the absolute ``artifact``
    path for preservation, hashing, and compatibility transforms.
    """
    flow = Path(flow_dir).expanduser().resolve()
    target = Path(artifact).expanduser().resolve()
    try:
        return target.relative_to(flow).as_posix()
    except ValueError as exc:
        raise StaNetlistCompatError(
            f"ORFS artifact is outside flow directory: artifact={target} flow={flow}"
        ) from exc


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _strip_comments(text: str) -> str:
    # Preserve line structure enough for diagnostics while ignoring attributes
    # and comments during the conservative behavioral-safety scan.
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"//[^\n]*", "", text)
    return text


def structural_safety_check(text: str) -> dict[str, Any]:
    cleaned = _strip_comments(text)
    behavioral = sorted(set(m.group(1) for m in BEHAVIORAL_RE.finditer(cleaned)))
    unsafe_assigns: list[str] = []
    for match in ASSIGN_RE.finditer(cleaned):
        expr = match.group("expr")
        if UNSAFE_ASSIGN_RE.search(expr):
            compact = " ".join(expr.split())
            unsafe_assigns.append(compact[:240])
            if len(unsafe_assigns) >= 20:
                break
    return {
        "safe": not behavioral and not unsafe_assigns,
        "behavioral_constructs": behavioral,
        "unsafe_assign_expressions": unsafe_assigns,
    }


def sanitize_text(text: str) -> tuple[str, dict[str, Any]]:
    safety = structural_safety_check(text)
    signed_count = len(SIGNED_DECL_RE.findall(text))
    if signed_count and not safety["safe"]:
        raise StaNetlistCompatError(
            "refusing to remove signed declaration qualifiers from a netlist "
            "that still contains behavioral/signed-sensitive expressions"
        )
    transformed, count = SIGNED_DECL_RE.subn(lambda m: f"{m.group(1)}{m.group(2)}", text)
    return transformed, {
        "safe_structural_netlist": bool(safety["safe"]),
        "signed_declaration_qualifiers_removed": count,
        "behavioral_constructs": safety["behavioral_constructs"],
        "unsafe_assign_expressions": safety["unsafe_assign_expressions"],
        "transformations": ["remove_signed_declaration_qualifiers"] if count else [],
    }


def sanitize_netlist(
    input_path: Path,
    output_path: Path,
    *,
    metadata_path: Path | None = None,
) -> dict[str, Any]:
    input_path = Path(input_path).resolve()
    output_path = Path(output_path).resolve()
    if not input_path.is_file():
        raise FileNotFoundError(input_path)
    original = input_path.read_text(encoding="utf-8", errors="strict")
    transformed, details = sanitize_text(original)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic replace so an interruption cannot leave OpenSTA with a partial file.
    tmp = output_path.with_name(output_path.name + ".tmp")
    tmp.write_text(transformed, encoding="utf-8")
    tmp.replace(output_path)
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "transform_version": TRANSFORM_VERSION,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "status": "passed",
        "input_path": str(input_path),
        "output_path": str(output_path),
        "input_sha256": hashlib.sha256(original.encode("utf-8")).hexdigest(),
        "output_sha256": sha256_file(output_path),
        "applied": bool(details["transformations"]),
        **details,
    }
    if metadata_path is not None:
        metadata_path = Path(metadata_path)
        metadata_path.parent.mkdir(parents=True, exist_ok=True)
        metadata_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def preserve_raw_and_sanitize(
    synthesis_netlist: Path,
    *,
    raw_path: Path | None = None,
    metadata_path: Path | None = None,
) -> dict[str, Any]:
    synthesis_netlist = Path(synthesis_netlist).resolve()
    if raw_path is None:
        raw_path = synthesis_netlist.with_name(synthesis_netlist.stem + ".raw" + synthesis_netlist.suffix)
    raw_path = Path(raw_path).resolve()
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    # Idempotence matters because the same FLOW_VARIANT can first be evaluated
    # at synth for normalized-hash dedupe and later resumed to grt.  Once the
    # raw Yosys artifact exists, never overwrite it with the already-sanitized
    # STA-facing copy.
    raw_preserved_now = False
    raw_reused = False
    current_sha = sha256_file(synthesis_netlist)
    prior_output_sha = None
    if metadata_path is not None and Path(metadata_path).is_file():
        try:
            prior = json.loads(Path(metadata_path).read_text(encoding="utf-8"))
            prior_output_sha = prior.get("output_sha256") if isinstance(prior, Mapping) else None
        except Exception:
            prior_output_sha = None

    existing_raw_matches_working_sanitized = False
    if raw_path.is_file():
        if prior_output_sha == current_sha:
            existing_raw_matches_working_sanitized = True
        elif prior_output_sha is None:
            try:
                raw_text = raw_path.read_text(encoding="utf-8", errors="strict")
                expected_sta_text, _ = sanitize_text(raw_text)
                expected_sta_sha = hashlib.sha256(expected_sta_text.encode("utf-8")).hexdigest()
                existing_raw_matches_working_sanitized = expected_sta_sha == current_sha
            except Exception:
                existing_raw_matches_working_sanitized = False

    if raw_path.is_file() and existing_raw_matches_working_sanitized:
        # The working file is the previously sanitized STA copy; reuse the
        # preserved raw implementation rather than overwriting it.
        raw_reused = True
    else:
        # Either this is the first interception or Yosys genuinely regenerated
        # the target.  Refresh raw authority from the current Yosys output.
        shutil.copy2(synthesis_netlist, raw_path)
        raw_preserved_now = True

    result = sanitize_netlist(raw_path, synthesis_netlist, metadata_path=metadata_path)
    result["raw_path"] = str(raw_path)
    result["sta_path"] = str(synthesis_netlist)
    result["raw_preserved_now"] = raw_preserved_now
    result["raw_reused"] = raw_reused
    if metadata_path is not None:
        Path(metadata_path).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input", type=Path)
    ap.add_argument("output", type=Path)
    ap.add_argument("--metadata", type=Path, default=None)
    args = ap.parse_args()
    try:
        result = sanitize_netlist(args.input, args.output, metadata_path=args.metadata)
    except Exception as exc:
        print(json.dumps({"schema": SCHEMA, "status": "failed", "reason": f"{type(exc).__name__}: {exc}"}, indent=2))
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
