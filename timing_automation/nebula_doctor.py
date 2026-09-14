#!/usr/bin/env python3
"""Validate the nEBULA/ORFS environment before an analysis or closure run."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    from nebula_env import (
        NebulaEnvError,
        flow_dir as resolve_flow_dir,
        openroad as resolve_openroad,
        orfs_root as resolve_orfs_root,
        yosys as resolve_yosys,
    )
except ImportError:
    from timing_automation.nebula_env import (
        NebulaEnvError,
        flow_dir as resolve_flow_dir,
        openroad as resolve_openroad,
        orfs_root as resolve_orfs_root,
        yosys as resolve_yosys,
    )

SCHEMA = "nebula_doctor/2"
# Warning threshold only. Older builds are not rejected solely by version.
VALIDATED_YOSYS_MIN = (0, 40)


def _tool_version(path: Path, args: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            [str(path), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=20,
            check=False,
        )
        text = (proc.stdout or "").strip()
        return proc.returncode, (text.splitlines()[0] if text else "no version output")
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, f"unavailable ({exc})"


def _yosys_version_tuple(text: str) -> tuple[int, int] | None:
    m = re.search(r"Yosys\s+(\d+)\.(\d+)", text, flags=re.I)
    return (int(m.group(1)), int(m.group(2))) if m else None


def _record_file(checks: list[dict[str, Any]], path: Path, label: str, required: bool = True) -> bool:
    ok = path.is_file()
    checks.append({"kind": "file", "label": label, "path": str(path), "ok": ok, "required": required})
    return ok or not required


def _find_liberty(platform_dir: Path) -> list[Path]:
    return sorted(p.resolve() for p in platform_dir.rglob("*.lib") if p.is_file()) if platform_dir.is_dir() else []


def run_doctor(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    checks: list[dict[str, Any]] = []
    warnings: list[str] = []
    failures: list[str] = []

    try:
        flow = resolve_flow_dir(args.flow_dir)
    except NebulaEnvError as exc:
        flow = Path(args.flow_dir or Path.cwd()).resolve()
        failures.append(str(exc))

    orfs: Path | None = None
    try:
        if args.orfs_root:
            orfs = resolve_orfs_root(args.orfs_root)
        elif flow.name == "flow":
            orfs = flow.parent
        else:
            orfs = resolve_orfs_root()
    except NebulaEnvError as exc:
        warnings.append(str(exc))

    # Structural project/ORFS checks.
    structural = [
        (flow / "Makefile", "ORFS flow Makefile"),
        (flow / "timing_automation/physical_recipes/calibration_seed.json", "calibration seed recipe"),
        (flow / "timing_automation/rtl_opt_corpus.json", "RTL-OPT corpus manifest"),
    ]
    for path, label in structural:
        if not _record_file(checks, path, label):
            failures.append(f"missing required file: {path}")

    makefile = flow / "Makefile"
    yosys_target_rule_ok = False
    yosys_target_error = None
    try:
        makefile_text = makefile.read_text(encoding="utf-8")
        yosys_target_rule_ok = "$(RESULTS_DIR)/1_2_yosys.v:" in makefile_text
    except Exception as exc:
        yosys_target_error = f"{type(exc).__name__}: {exc}"
    checks.append({
        "kind": "orfs_target",
        "label": "pre-OpenSTA Yosys netlist target",
        "path": str(makefile),
        "ok": yosys_target_rule_ok,
        "required": True,
        "target": "$(RESULTS_DIR)/1_2_yosys.v",
        "error": yosys_target_error,
    })
    if not yosys_target_rule_ok:
        failures.append(
            "ORFS Makefile does not expose the expected $(RESULTS_DIR)/1_2_yosys.v target; "
            "STA compatibility interception cannot be guaranteed"
        )

    platform_dir = flow / "platforms" / args.platform
    platform_cfg = platform_dir / "config.mk"
    if not _record_file(checks, platform_cfg, f"platform config ({args.platform})"):
        failures.append(f"missing platform config: {platform_cfg}")
    liberty = _find_liberty(platform_dir)
    checks.append({"kind": "liberty", "label": args.platform, "path": str(platform_dir), "ok": bool(liberty), "required": True, "files": [str(p) for p in liberty[:20]]})
    if not liberty:
        failures.append(f"no Liberty .lib found under {platform_dir}")

    # Design checks only when a design is actually requested.
    if args.design:
        ddir = flow / "designs" / args.platform / args.design
        config = ddir / "config.mk"
        if not _record_file(checks, config, f"design config ({args.design})"):
            failures.append(f"missing design config: {config}")

        # Common ORFS layout checks are informative rather than hardcoded
        # requirements: config.mk is authoritative and may point elsewhere.
        sdc_files = sorted(ddir.glob("*.sdc")) if ddir.is_dir() else []
        checks.append({"kind": "design_constraints", "label": args.design, "path": str(ddir), "ok": bool(sdc_files), "required": False, "files": [str(p) for p in sdc_files]})
        if not sdc_files:
            warnings.append(f"no .sdc found directly under {ddir}; config.mk may reference constraints elsewhere")

        source_dirs = [flow / "designs" / "src" / args.design, ddir]
        source_files = []
        for source_dir in source_dirs:
            if source_dir.is_dir():
                source_files.extend(sorted([*source_dir.glob("*.v"), *source_dir.glob("*.sv")]))
        checks.append({"kind": "design_sources", "label": args.design, "path": ";".join(str(p) for p in source_dirs), "ok": bool(source_files), "required": False, "files": [str(p) for p in source_files]})
        if not source_files:
            warnings.append("no RTL found in common design source locations; config.mk may reference sources elsewhere")

    # Centralized executable resolution.
    tool_specs = [
        ("yosys", lambda: resolve_yosys(args.yosys, orfs=orfs, flow=flow), ["-V"]),
        ("openroad", lambda: resolve_openroad(args.openroad, orfs=orfs, flow=flow), ["-version"]),
    ]
    versions: dict[str, str] = {}
    for name, resolver, version_args in tool_specs:
        try:
            path = resolver()
            rc, version = _tool_version(path, version_args)
            ok = rc == 0
            checks.append({"kind": "tool", "label": name, "path": str(path), "ok": ok, "required": True, "version": version})
            versions[name] = version
            if not ok:
                failures.append(f"{name} executable probe failed: {path} (exit {rc})")
        except NebulaEnvError as exc:
            checks.append({"kind": "tool", "label": name, "path": None, "ok": False, "required": True, "error": str(exc)})
            failures.append(str(exc))

    for name in ("make", "git"):
        found = shutil.which(name)
        ok = bool(found)
        checks.append({"kind": "tool", "label": name, "path": found, "ok": ok, "required": True})
        if not ok:
            failures.append(f"required executable not found on PATH: {name}")

    yv = _yosys_version_tuple(versions.get("yosys", ""))
    if yv is not None and yv < VALIDATED_YOSYS_MIN:
        warnings.append(
            f"Yosys {yv[0]}.{yv[1]} is older than nEBULA's validated warning threshold "
            f"{VALIDATED_YOSYS_MIN[0]}.{VALIDATED_YOSYS_MIN[1]}; verify that ORFS still emits "
            "1_1_yosys_canonicalize.rtlil before relying on RTL mapping."
        )

    # Storage/token-provider/schema checks catch common long-run failures before
    # expensive calibration or Groq calls start.
    usage = shutil.disk_usage(flow)
    free_gb = usage.free / (1024 ** 3)
    disk_ok = free_gb >= 1.0
    checks.append({"kind": "storage", "label": "free_disk_gb", "path": str(flow), "ok": disk_ok, "required": True, "free_gb": round(free_gb, 2)})
    if not disk_ok:
        failures.append(f"less than 1 GiB free under {flow}: {free_gb:.2f} GiB")
    elif free_gb < 5.0:
        warnings.append(f"low free disk space for multi-design ORFS runs: {free_gb:.2f} GiB")

    groq_present = bool(os.environ.get("GROQ_API_KEY"))
    checks.append({"kind": "credential", "label": "GROQ_API_KEY", "ok": groq_present, "required": bool(args.groq)})
    if args.groq and not groq_present:
        failures.append("GROQ_API_KEY is required under --groq")
    elif not groq_present:
        warnings.append("GROQ_API_KEY is not set; rtl-opt-groq cannot run until credentials are loaded")

    for schema_name in ("response_schema_latency.json", "response_schema_prefreeze.json"):
        schema_path = flow / "timing_automation" / schema_name
        ok = False
        error = None
        try:
            value = json.loads(schema_path.read_text(encoding="utf-8"))
            ok = isinstance(value, dict) and value.get("$schema") == "https://json-schema.org/draft/2020-12/schema"
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        checks.append({"kind": "schema", "label": schema_name, "path": str(schema_path), "ok": ok, "required": True, "error": error})
        if not ok:
            failures.append(f"invalid required response schema: {schema_path}" + (f" ({error})" if error else ""))

    try:
        try:
            from sta_netlist_compat import sanitize_text
        except ImportError:
            from timing_automation.sta_netlist_compat import sanitize_text
        sample = "module m(a,y); input signed [0:0] a; output [0:0] y; BUF_X1 u(.A(a),.Z(y)); endmodule\n"
        transformed, compat = sanitize_text(sample)
        compat_ok = " signed " not in transformed and compat.get("signed_declaration_qualifiers_removed") == 1
        checks.append({"kind": "compat", "label": "sta_netlist_compat", "ok": compat_ok, "required": True})
        if not compat_ok:
            failures.append("STA netlist compatibility self-check failed")
    except Exception as exc:
        checks.append({"kind": "compat", "label": "sta_netlist_compat", "ok": False, "required": True, "error": str(exc)})
        failures.append(f"STA netlist compatibility self-check failed: {exc}")

    pyarrow_available = importlib.util.find_spec("pyarrow") is not None
    checks.append({"kind": "python", "label": "pyarrow", "ok": pyarrow_available, "required": bool(args.calibration)})
    if args.calibration and not pyarrow_available:
        failures.append("PyArrow is required under --calibration")

    corpus_path = flow / "timing_automation/rtl_opt_corpus.json"
    if args.calibration:
        if not corpus_path.is_file():
            failures.append(f"calibration corpus manifest missing: {corpus_path}")
        else:
            try:
                corpus = json.loads(corpus_path.read_text(encoding="utf-8"))
                source = corpus.get("source") or {}
                payload_rel = source.get("payload")
                expected = source.get("payload_sha256")
                if not payload_rel or not expected:
                    raise ValueError("source.payload/source.payload_sha256 missing")
                payload = flow / str(payload_rel)
                actual = hashlib.sha256(payload.read_bytes()).hexdigest() if payload.is_file() else None
                ok = actual == expected
                checks.append({"kind": "dataset", "label": "RTL-OPT payload", "path": str(payload), "ok": ok, "required": True, "expected_sha256": expected, "actual_sha256": actual})
                if not ok:
                    failures.append("RTL-OPT calibration payload missing or SHA-256 mismatch")
            except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
                failures.append(f"invalid calibration corpus manifest: {exc}")

    result = {
        "schema": SCHEMA,
        "ok": not failures,
        "flow_dir": str(flow),
        "orfs_root": str(orfs) if orfs else None,
        "platform": args.platform,
        "design": args.design,
        "checks": checks,
        "warnings": warnings,
        "failures": failures,
    }
    return result, (0 if result["ok"] else 1)


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--flow-dir", default=None)
    ap.add_argument("--orfs-root", default=None)
    ap.add_argument("--platform", default="nangate45")
    ap.add_argument("--design", default=None)
    ap.add_argument("--yosys", default=None)
    ap.add_argument("--openroad", default=None)
    ap.add_argument("--json", dest="json_output", default=None, help="Write structured doctor result JSON.")
    ap.add_argument("--calibration", action="store_true", help="Require PyArrow and a hash-valid RTL-OPT payload.")
    ap.add_argument("--groq", action="store_true", help="Require GROQ_API_KEY for optimizer execution.")
    ap.add_argument("--verbose", action="store_true")
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    result, rc = run_doctor(args)
    if args.json_output:
        out = Path(args.json_output).expanduser().resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")

    print(f"NEBULA flow: {result['flow_dir']}")
    print(f"Platform   : {result['platform']}")
    if result["design"]:
        print(f"Design     : {result['design']}")
    for check in result["checks"]:
        if args.verbose or not check.get("ok"):
            state = "OK" if check.get("ok") else ("WARNING" if not check.get("required") else "MISSING")
            print(f"{state:8} {check.get('kind'):8} {check.get('label')}: {check.get('path', '')}")
    for warning in result["warnings"]:
        print(f"WARNING  {warning}")
    if result["failures"]:
        for failure in result["failures"]:
            print(f"FAIL     {failure}")
        return rc
    print("Doctor passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
