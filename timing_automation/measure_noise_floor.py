#!/usr/bin/env python3
"""Measure ORFS run-to-run PPA noise for one frozen design/recipe pair."""
from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from pathlib import Path
from statistics import mean
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

try:
    from physical_recipe import PhysicalRecipe, frozen_physical_variables
    from collect_metrics import collect_metrics
    from run_flow import generate_flow_variant, FLOW_DIR as RUN_FLOW_DIR
    from run_candidate_flow import STAGE_TARGETS
except ImportError:
    from timing_automation.physical_recipe import PhysicalRecipe, frozen_physical_variables
    from timing_automation.collect_metrics import collect_metrics
    from timing_automation.run_flow import generate_flow_variant, FLOW_DIR as RUN_FLOW_DIR
    from timing_automation.run_candidate_flow import STAGE_TARGETS

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_FLOW_DIR = SCRIPT_DIR.parent
SCHEMA = "nebula_noise_floor/1"
METRIC_KEYS = ("wns_ns", "area_um2", "power_mw")

class NoiseFloorError(RuntimeError):
    pass


def _load_json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise NoiseFloorError(f"could not read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise NoiseFloorError(f"JSON root must be an object: {path}")
    return value


def _metric_value(doc: Mapping[str, Any], key: str) -> float:
    metrics = doc.get("metrics") or {}
    value = metrics.get(key) if isinstance(metrics, Mapping) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise NoiseFloorError(f"metric {key} missing/non-finite")
    return float(value)


def _spread(values: Sequence[float]) -> Dict[str, float]:
    if not values:
        raise NoiseFloorError("cannot compute spread from no values")
    return {
        "min": min(values),
        "max": max(values),
        "mean": mean(values),
        "spread": max(values) - min(values),
    }


def _percent_spread(values: Sequence[float]) -> float:
    stats = _spread(values)
    denom = abs(stats["mean"])
    if denom == 0.0:
        return 0.0 if stats["spread"] == 0.0 else float("inf")
    return 100.0 * stats["spread"] / denom


def calculate_noise_floor(
    docs: Sequence[Mapping[str, Any]],
    *,
    design: str,
    platform: str,
) -> Dict[str, Any]:
    if len(docs) < 2:
        raise NoiseFloorError("at least two repeated metric samples are required")

    stages = set()
    fingerprints = set()
    recipe_ids = set()
    variants: List[str] = []
    observations: Dict[str, List[float]] = {key: [] for key in METRIC_KEYS}

    for idx, doc in enumerate(docs):
        d = doc.get("design") or {}
        doc_design = d.get("design_id") or d.get("design") or doc.get("design")
        doc_platform = d.get("platform") or doc.get("platform")
        if doc_design not in (None, design):
            raise NoiseFloorError(f"sample {idx}: design mismatch {doc_design!r} != {design!r}")
        if doc_platform not in (None, platform):
            raise NoiseFloorError(f"sample {idx}: platform mismatch {doc_platform!r} != {platform!r}")

        evaluation = doc.get("evaluation") or {}
        stage = evaluation.get("stage")
        fingerprint = evaluation.get("recipe_fingerprint_sha256")
        recipe_id = evaluation.get("recipe_id")
        if not isinstance(stage, str) or not stage:
            raise NoiseFloorError(f"sample {idx}: evaluation.stage is required")
        if not isinstance(fingerprint, str) or not fingerprint:
            raise NoiseFloorError(f"sample {idx}: recipe fingerprint is required")
        stages.add(stage)
        fingerprints.add(fingerprint.lower())
        if isinstance(recipe_id, str) and recipe_id:
            recipe_ids.add(recipe_id)
        variants.append(str(doc.get("flow_variant") or f"sample_{idx}"))
        for key in METRIC_KEYS:
            observations[key].append(_metric_value(doc, key))

    if len(stages) != 1:
        raise NoiseFloorError(f"mixed physical stages are not comparable: {sorted(stages)}")
    if len(fingerprints) != 1:
        raise NoiseFloorError("mixed physical recipe fingerprints are not comparable")
    if len(recipe_ids) > 1:
        raise NoiseFloorError(f"mixed recipe ids are not comparable: {sorted(recipe_ids)}")

    observed = {key: _spread(values) for key, values in observations.items()}
    return {
        "schema": SCHEMA,
        "design": design,
        "platform": platform,
        "stage": next(iter(stages)),
        "recipe_fingerprint_sha256": next(iter(fingerprints)),
        "recipe_id": next(iter(recipe_ids)) if recipe_ids else None,
        "repeats": len(docs),
        "flow_variants": variants,
        "observed": observed,
        "noise_floor": {
            "wns_ns": observed["wns_ns"]["spread"],
            "area_pct": _percent_spread(observations["area_um2"]),
            "power_pct": _percent_spread(observations["power_mw"]),
        },
    }


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path = Path(path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _frozen_role_path(recipe: PhysicalRecipe, role: str) -> Path:
    matches = [item for item in recipe.files if item.role == role]
    if len(matches) != 1:
        raise NoiseFloorError(f"physical recipe must contain exactly one {role!r} file")
    raw = Path(matches[0].logical_path).expanduser()
    root = Path(recipe.source_base_dir).expanduser().resolve()
    return raw.resolve() if raw.is_absolute() else (root / raw).resolve()


def _baseline_make_command(
    *, recipe: PhysicalRecipe, flow_dir: Path, flow_variant: str
) -> List[str]:
    config = _frozen_role_path(recipe, "design_config")
    if not config.is_file():
        raise NoiseFloorError(f"frozen design config not found: {config}")
    command = [
        "make",
        f"DESIGN_CONFIG={config}",
        f"FLOW_VARIANT={flow_variant}",
    ]
    report_tcl = flow_dir / "timing_automation" / "report_checks.tcl"
    if report_tcl.is_file():
        command.append(f"POST_FINAL_REPORT_TCL={report_tcl.resolve()}")
    command.extend(
        f"{key}={value}" for key, value in sorted(frozen_physical_variables(recipe).items())
    )
    command.extend(STAGE_TARGETS[recipe.stage])
    return command


def run_repeated_baseline(
    *,
    design: str,
    platform: str,
    recipe_path: Path,
    repeats: int,
    flow_dir: Path,
    output_dir: Path,
) -> List[Dict[str, Any]]:
    flow_dir = Path(flow_dir).expanduser().resolve()
    if flow_dir != Path(RUN_FLOW_DIR).resolve():
        raise NoiseFloorError(
            "run_flow.py currently owns its ORFS root from its installed location; "
            f"requested --flow-dir {flow_dir} != {Path(RUN_FLOW_DIR).resolve()}"
        )
    recipe = PhysicalRecipe.read_json(recipe_path)
    recipe.validate()
    verification = recipe.verify_frozen_files()
    if verification.get("ok") is not True:
        raise NoiseFloorError("physical recipe frozen-file verification failed")
    if recipe.design_id != design or recipe.platform != platform:
        raise NoiseFloorError("design/platform do not match physical recipe")

    docs: List[Dict[str, Any]] = []
    output_dir.mkdir(parents=True, exist_ok=True)
    for index in range(repeats):
        variant = generate_flow_variant(prefix=f"noise{index+1}")
        command = _baseline_make_command(recipe=recipe, flow_dir=flow_dir, flow_variant=variant)
        proc = subprocess.run(command, cwd=str(flow_dir), text=True)
        if proc.returncode != 0:
            raise NoiseFloorError(
                f"baseline ORFS repeat {index + 1} failed with return code {proc.returncode}"
            )
        metrics = collect_metrics(
            variant=variant,
            design=design,
            platform=platform,
            flow_dir=flow_dir,
            physical_recipe=recipe,
        )
        metrics_path = output_dir / f"{variant}_metrics.json"
        write_json(metrics_path, metrics)
        docs.append(metrics)
    return docs


def run_self_test() -> None:
    base = {
        "design": {"design_id": "demo", "platform": "nangate45"},
        "evaluation": {"stage": "global_route", "recipe_fingerprint_sha256": "abc", "recipe_id": "r1"},
    }
    docs = []
    for i, (w, a, p) in enumerate(((-0.183, 100.0, 0.428), (-0.181, 100.0, 0.432), (-0.182, 100.0, 0.430))):
        d = json.loads(json.dumps(base))
        d["flow_variant"] = f"v{i}"
        d["metrics"] = {"wns_ns": w, "area_um2": a, "power_mw": p}
        docs.append(d)
    out = calculate_noise_floor(docs, design="demo", platform="nangate45")
    assert abs(out["noise_floor"]["wns_ns"] - 0.002) < 1e-12
    assert out["noise_floor"]["area_pct"] == 0.0
    assert 0.92 < out["noise_floor"]["power_pct"] < 0.94
    with tempfile.TemporaryDirectory(prefix="nebula_noise_cmd_") as tmp:
        root = Path(tmp)
        cfg = root / "config.mk"
        cfg.write_text("# cfg\n", encoding="utf-8")
        fake_recipe = SimpleNamespace(
            files=(SimpleNamespace(role="design_config", logical_path="config.mk"),),
            source_base_dir=str(root),
            stage="global_route",
            flow_variables={"DIE_AREA": "0 0 100 100", "CORE_AREA": "2 2 98 98", "PLACE_DENSITY": 0.55},
        )
        cmd = _baseline_make_command(recipe=fake_recipe, flow_dir=root, flow_variant="noise_test")
        assert f"DESIGN_CONFIG={cfg.resolve()}" in cmd
        assert "DIE_AREA=0 0 100 100" in cmd
        assert "CORE_AREA=2 2 98 98" in cmd
        assert "PLACE_DENSITY=0.55" in cmd
        assert cmd[-1] == "grt"

    bad = json.loads(json.dumps(docs))
    bad[1]["evaluation"]["recipe_fingerprint_sha256"] = "different"
    try:
        calculate_noise_floor(bad, design="demo", platform="nangate45")
    except NoiseFloorError:
        pass
    else:
        raise AssertionError("mixed recipe fingerprints were accepted")
    print("PASS: measure_noise_floor self-test")
    print("PASS: WNS/area/power spread computed")
    print("PASS: mismatched recipe fingerprint rejected")
    print("PASS: repeated baseline command applies frozen recipe geometry/flow variables and stage")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Measure frozen-recipe ORFS PPA noise floor.")
    p.add_argument("--design")
    p.add_argument("--platform", default="nangate45")
    p.add_argument("--physical-recipe")
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument("--flow-dir", default=str(DEFAULT_FLOW_DIR))
    p.add_argument("--metrics", action="append", default=[], help="Existing canonical metrics JSON; repeat to calculate without running ORFS.")
    p.add_argument("--output", default=None)
    p.add_argument("--self-test", action="store_true")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if args.self_test:
        try:
            run_self_test()
            return 0
        except Exception as exc:
            print(f"SELF-TEST FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
    if not args.design:
        print("ERROR: --design is required", file=sys.stderr)
        return 2
    if args.repeats < 2:
        print("ERROR: --repeats must be >= 2", file=sys.stderr)
        return 2

    try:
        flow_dir = Path(args.flow_dir).expanduser().resolve()
        if args.metrics:
            docs = [_load_json(Path(p).expanduser().resolve()) for p in args.metrics]
        else:
            if not args.physical_recipe:
                raise NoiseFloorError("--physical-recipe is required unless --metrics is supplied")
            out_dir = flow_dir / "ai_runs" / f"{args.design}_noise_floor_samples"
            docs = run_repeated_baseline(
                design=args.design,
                platform=args.platform,
                recipe_path=Path(args.physical_recipe).expanduser().resolve(),
                repeats=args.repeats,
                flow_dir=flow_dir,
                output_dir=out_dir,
            )
        result = calculate_noise_floor(docs, design=args.design, platform=args.platform)
        output = Path(args.output).expanduser().resolve() if args.output else (flow_dir / "ai_runs" / f"{args.design}_noise_floor.json")
        write_json(output, result)
        print(json.dumps({"ok": True, "output": str(output), "noise_floor": result["noise_floor"], "repeats": result["repeats"]}, indent=2))
        return 0
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
