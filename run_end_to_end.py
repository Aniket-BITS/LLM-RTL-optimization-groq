#!/usr/bin/env python3
"""Run the isolated, fixed-constraint mapper benchmark through Groq and PPA."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent
SCRIPTS = ROOT / "timing_automation"
sys.path.insert(0, str(SCRIPTS))
from physical_recipe import build_physical_recipe


def run(script, *args):
    subprocess.run([sys.executable, str(SCRIPTS / script), *map(str, args)], cwd=ROOT, check=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--resume-baseline", help="Existing qualified baseline variant; provenance is checked")
    ap.add_argument("--max-attempts", type=int, default=3)  # intentionally 3 here; run_rtl_opt_groq.py uses 5
    args = ap.parse_args()
    ai = ROOT / "ai_runs"
    recipe_path = ai / "physical_recipe.json"
    spec = json.loads((ai / "recipe_spec.json").read_text())
    build_physical_recipe(spec, base_dir=ROOT).write_json(recipe_path)
    before = set(ai.glob("*_pipeline.json"))
    analysis_args = ["--design", "mapper_reconvergent_test", "--liberty",
                     ROOT / "platforms/nangate45/lib/NangateOpenCellLibrary_typical.lib"]
    if args.resume_baseline:
        analysis_args += ["--variant", args.resume_baseline, "--resume", "--force-stage", "optimizer_input"]
    else:
        analysis_args += ["--prefix", "groq_e2e"]
    run("run_analysis.py", *analysis_args)
    if args.resume_baseline:
        variant = args.resume_baseline
    else:
        created = set(ai.glob("*_pipeline.json")) - before
        if len(created) != 1:
            raise RuntimeError("Cannot identify unique new baseline")
        variant = json.loads(created.pop().read_text())["flow_variant"]
    run("collect_metrics.py", "--variant", variant, "--design", "mapper_reconvergent_test",
        "--flow-dir", ROOT, "--physical-recipe", recipe_path, "--output", ai / "baseline_metrics.json")
    if not os.environ.get("GROQ_API_KEY"):
        print("Baseline complete. Set GROQ_API_KEY in .env.local, then resume with --resume-baseline " + variant, file=sys.stderr)
        return 2
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    attempts = ai / ("groq_attempts_" + stamp)
    baseline = json.loads((ai / "baseline_metrics.json").read_text())["metrics"]
    # Ask for an improvement even when the benchmark already meets setup timing.
    run("run_closure_loop.py", "--optimizer-input", ai / (variant + "_optimizer_input.json"),
        "--golden-manifest", ai / "golden_manifest.json", "--golden-base-dir", ROOT,
        "--physical-recipe", recipe_path, "--baseline-metrics", ai / "baseline_metrics.json",
        "--flow-dir", ROOT, "--ai-runs", attempts, "--max-attempts", args.max_attempts,
        "--max-flows", args.max_attempts, "--target-wns", baseline["wns_ns"] + 0.005,
        "--model", os.environ.get("OPTIMIZER_MODEL", "openai/gpt-oss-120b"),
        "--max-tokens", 4096, "--chain", "baseline", "--yes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
