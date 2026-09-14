#!/usr/bin/env python3
"""V3 RTL-OPT calibration: classification, baseline timing grid and Pareto evidence.

V2 remains immutable as a fixed-0.60-ns stress test. V3 chooses a timing point
from the baseline only, then freezes a physical recipe at that point before the
baseline/reference comparison. RTL-OPT is only read and hash-checked.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import math
from pathlib import Path

try:
    from equivalence_terms import EquivalenceVerdict
except ImportError:
    from timing_automation.equivalence_terms import EquivalenceVerdict
import re
try:
    from sta_netlist_compat import preserve_raw_and_sanitize, StaNetlistCompatError, orfs_yosys_netlist_target, orfs_make_target
except ImportError:
    from timing_automation.sta_netlist_compat import preserve_raw_and_sanitize, StaNetlistCompatError, orfs_yosys_netlist_target, orfs_make_target

import calibrate_pairs as v2

ROOT = v2.ROOT
CLOCK_GRID_NS = (0.60, 0.80, 1.00, 1.25, 1.50, 2.00)
LATENCY_CHANGING_IDS = frozenset({"mac"})
DIVERSE_COHORT_ONE = ("adder", "fsm", "decoder_6bit", "sub_16bit", "calculation", "mac")
LATENCY_CONTRACTS = {
    "mac": {
        "schema": "nebula_latency_contract/1",
        "reference_delay_cycles": 2,
        "reset_port": "reset",
        "reset_active_level": 1,
        "initial_reset_cycles": 2,
        "data_during_reset": "forced_zero_by_contract_harness",
        "comparison_starts_after_cycle": 5,
        "observable_outputs": ["z"],
        "meaning": "baseline z at cycle t equals reference z delayed by two cycles after a two-cycle initialization reset",
    }
}
POLICY = {
    **v2.POLICY,
    "schema": "nebula_calibration_policy/2",
    "timing_grid_ns": list(CLOCK_GRID_NS),
    "timing_point_selection": "first baseline setup-and-hold-clean point in ascending predefined grid",
    "promotion_requires_baseline_feasible": True,
    "pareto_policy": "record all comparable deltas independently of promotion",
}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def pair_class(pair):
    if pair["id"] in LATENCY_CHANGING_IDS:
        return "latency_changing"
    edges = set(pair["baseline"]["edge_signals"]) | set(pair["reference"]["edge_signals"])
    if not edges:
        return "combinational"
    reset_names = {"reset", "rst", "rst_n", "clear"}
    ports = set()
    for side in ("baseline", "reference"):
        text = Path(pair[side]["source"]).read_text()
        # ANSI headers can declare a comma-separated reset after the single
        # input keyword; token scan avoids treating only the first name as a port.
        ports.update(re.findall(r"\b(?:reset|rst|rst_n|clear)\b", text, flags=re.I))
    normalized_ports = {name.lower() for name in ports}
    return "reset_sensitive_sequential" if reset_names & normalized_ports or reset_names & edges else "same_cycle_sequential"


def prepare(out):
    manifest = v2.prepare(out)
    for pair in manifest["pairs"]:
        pair["pair_class"] = pair_class(pair)
        pair["latency_contract"] = LATENCY_CONTRACTS.get(pair["id"])
        pair["timing_grid"] = {
            "units": "ns",
            "periods_ns": list(CLOCK_GRID_NS),
            "selection_authority": "baseline only",
            "selection_rule": POLICY["timing_point_selection"],
        }
        pair["pareto_policy"] = POLICY["pareto_policy"]
        pair["calibration_policy"] = POLICY
        pair["runner_sha256"] = digest(__file__)
    manifest.update(schema="nebula_pair_calibration/2", policy=POLICY,
                    v2_stress_experiment=str(ROOT / "ai_runs/rtl_opt_calibration_v2"))
    v2.write_json(out / "pair_manifest.json", manifest)
    return manifest


def freeze(pair, directory, module, period_ns, purpose):
    directory.mkdir(parents=True, exist_ok=True)
    profile = dict(pair["timing_profile"])
    profile["period_ns"] = period_ns
    profile["name"] = ("single_clock" if profile["clock_port"] else "virtual_clock") + f"_{period_ns:g}ns"
    clk = profile["clock_port"]
    sdc = directory / "constraint.sdc"
    lines = [
        "# Calibration profile: ns; all I/O timed; no timing exceptions.",
        f"create_clock -name core_clock -period {period_ns:g}" + (" [get_ports {clk}]" if clk else ""),
        "set_clock_uncertainty 0.02 [get_clocks core_clock]",
    ]
    for name, port in module["ports"].items():
        if name == clk:
            continue
        direction = port["direction"]
        if direction not in ("input", "output"):
            raise ValueError("inout port unsupported")
        delay = profile["late_inputs"].get(name, .1) if direction == "input" else .1
        lines += [
            f"set_{direction}_delay -min 0 -clock core_clock [get_ports {{{name}}}]",
            f"set_{direction}_delay -max {delay} -clock core_clock [get_ports {{{name}}}]",
        ]
    sdc.write_text("\n".join(lines) + "\n")
    spec = json.loads(v2.SEED_RECIPE.read_text())
    # Replace machine-local historical hook paths with project-local frozen copies.
    for entry in spec["files"]:
        if entry["role"] == "pdn":
            entry["path"] = str(v2.PORTABLE_PDN)
        if entry["role"] == "timing_repair_hook":
            entry["path"] = str(v2.PORTABLE_REPAIR_BUDGET)
    spec["flow_variables"].update(PDN_TCL=str(v2.PORTABLE_PDN),
                                  PRE_CTS_TCL=str(v2.PORTABLE_REPAIR_BUDGET))
    top = pair["baseline"]["top"]
    nickname = "rtlopt_" + pair["id"]
    spec["recipe_id"] = f"{nickname}_{purpose}_{period_ns:g}ns"
    spec["design"] = {"design_id": nickname, "top": top, "platform": "nangate45"}
    variables = spec["flow_variables"]
    variables.update(DIE_AREA="0 0 120 120", CORE_AREA="2.09 2.8 117.8 117.2", NUM_CORES=2)
    config = directory / "config.mk"
    settings = {"PLATFORM": "nangate45", "DESIGN_NAME": top, "DESIGN_NICKNAME": nickname,
                "SDC_FILE": str(sdc), **variables}
    config.write_text("\n".join(f"export {k} = {v}" for k, v in settings.items()) + "\n")
    for entry in spec["files"]:
        if entry["role"] == "design_config":
            entry["path"] = str(config)
        if entry["role"] == "constraints":
            entry["path"] = str(sdc)
    spec["toolchain"] = {
        "orfs_revision": v2.subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "yosys_revision": v2.subprocess.check_output([v2.YOSYS, "-V"], text=True).strip(),
        "openroad_revision": v2.subprocess.check_output([v2.OPENROAD, "-version"], text=True).strip(),
    }
    for folder in ("scripts", "util", "platforms/nangate45"):
        for path in sorted((ROOT / folder).rglob("*")):
            if path.is_file() and path.suffix in {".tcl", ".mk", ".lef", ".lib", ".v", ".py"}:
                spec["files"].append({"role": "flow_dependency", "path": str(path)})
    for path in (ROOT / "Makefile", ROOT / "scripts/variables.yaml"):
        spec["files"].append({"role": "flow_dependency", "path": str(path)})
    spec["metadata"] = {
        "timing_profile": profile,
        "geometry_origin": "a priori 120um calibration die, identical for both sides",
        "purpose": purpose,
        "policy": POLICY,
    }
    recipe = v2.build_physical_recipe(spec, base_dir=ROOT)
    v2.frozen_physical_variables(recipe)
    recipe.write_json(directory / "physical_recipe.json")
    reference = Path(pair["reference"]["source"]).read_text()
    reference, count = re.subn(r"(\bmodule\s+)" + re.escape(pair["reference"]["top"]) + r"\b",
                               lambda m: m[1] + top, reference, count=1)
    if count != 1:
        raise ValueError("Top normalization failed")
    normalized = directory / "reference_physical.v"
    normalized.write_text(reference)
    return recipe, normalized, profile


def physical(pair, directory, side, recipe, source, label, repeat=0, stage="grt", timeout_s=900):
    """Run one frozen physical arm with an auditable Yosys->STA handoff.

    For stages after synthesis we first establish the synthesis checkpoint,
    preserve the raw Yosys netlist, sanitize only safe structural ``signed``
    declarations for OpenSTA compatibility, then continue the same FLOW_VARIANT.
    The raw artifact remains authoritative for synthesis hashing.
    """
    if not recipe.verify_frozen_files()["ok"]:
        raise ValueError("Frozen recipe mutated")
    expected = digest(source)
    namespace = hashlib.sha256(str(directory.resolve()).encode()).hexdigest()[:16]
    variant = f"calibration_v3_{pair['id']}_{namespace}_{label}_{side}_{repeat}"
    log_prefix = directory / f"{side}.{label}.{repeat}"
    base_command = [
        "make", f"DESIGN_CONFIG={directory / 'config.mk'}", f"FLOW_VARIANT={variant}",
        f"VERILOG_FILES={source}",
    ]
    design_results = ROOT / "results/nangate45" / ("rtlopt_" + pair["id"]) / variant
    netlist = orfs_yosys_netlist_target(ROOT, "nangate45", "rtlopt_" + pair["id"], variant)
    raw_netlist = design_results / "1_2_yosys.raw.v"
    compat_meta = design_results / "1_2_yosys_sta_compat.json"

    # IMPORTANT: ORFS's phony ``synth`` target includes OpenROAD synth_odb.
    # We need an interception point *before* OpenSTA reads the Yosys netlist,
    # so request the exact Yosys file target first for every stage.
    yosys_target = orfs_make_target(ROOT, netlist)
    preparation = v2.run(
        base_command + [yosys_target],
        directory / f"{side}.{label}.{repeat}.yosys_netlist.log",
        timeout_s,
    )
    if preparation["exit_code"] != 0 or not netlist.is_file():
        result = dict(preparation)
        prep_log = Path(preparation.get("log", "")) if preparation.get("log") else None
        prep_text = ""
        if prep_log is not None and prep_log.is_file():
            try:
                prep_text = prep_log.read_text(errors="replace")
            except OSError:
                prep_text = ""
        if preparation["exit_code"] != 0 and "No rule to make target" in prep_text:
            failure_reason = "make_target_resolution_failed"
        elif preparation["exit_code"] != 0:
            failure_reason = "yosys_target_failed"
        else:
            failure_reason = "synthesis_netlist_not_found"
        result.update(
            recipe_fingerprint_sha256=recipe.fingerprint_sha256,
            source_sha256=expected,
            status="failed",
            failure_reason=failure_reason,
            synthesis_netlist=str(netlist),
            synthesis_netlist_raw=str(raw_netlist if raw_netlist.is_file() else netlist),
            synthesis_netlist_sta=str(netlist),
            synthesis_preparation=preparation,
            yosys_netlist_target=yosys_target,
        )
        return result

    try:
        compat = preserve_raw_and_sanitize(netlist, raw_path=raw_netlist, metadata_path=compat_meta)
    except Exception as exc:
        return {
            **preparation,
            "recipe_fingerprint_sha256": recipe.fingerprint_sha256,
            "source_sha256": expected,
            "status": "failed",
            "failure_reason": "sta_netlist_compat_failed",
            "sta_netlist_compat": {"status": "failed", "reason": f"{type(exc).__name__}: {exc}"},
            "synthesis_netlist": str(netlist),
            "synthesis_netlist_raw": str(raw_netlist if raw_netlist.is_file() else netlist),
            "synthesis_netlist_sta": str(netlist),
            "synthesis_preparation": preparation,
            "yosys_netlist_target": yosys_target,
        }

    # Resume the originally requested ORFS stage.  Since the sanitized
    # 1_2_yosys.v is newer than its prerequisites, Make should not rerun Yosys.
    result = v2.run(base_command + [stage], directory / f"{side}.{label}.{repeat}.{stage}.log", timeout_s)
    result.update(recipe_fingerprint_sha256=recipe.fingerprint_sha256, source_sha256=expected)
    metrics_path = ROOT / "logs/nangate45" / ("rtlopt_" + pair["id"]) / variant / "5_1_grt.json"
    result["metrics_path"] = str(metrics_path)
    result["synthesis_netlist"] = str(netlist)
    result["synthesis_netlist_raw"] = str(raw_netlist if raw_netlist.is_file() else netlist)
    result["synthesis_netlist_sta"] = str(netlist)
    result["synthesis_preparation"] = preparation
    result["yosys_netlist_target"] = yosys_target
    result["sta_netlist_compat"] = compat
    if stage == "synth":
        netlist_exists = netlist.is_file()
        if result["exit_code"] != 0:
            result["failure_reason"] = "synth_odb_failed"
        elif not netlist_exists:
            result["failure_reason"] = "synthesis_netlist_not_found"
        else:
            result["failure_reason"] = None
        result["status"] = "passed" if result["exit_code"] == 0 and netlist_exists else "failed"
        return result
    if result["exit_code"] == 0 and metrics_path.exists():
        raw = json.loads(metrics_path.read_text())
        names = {"area_um2": "design__instance__area", "power_w": "power__total",
                 "setup_ws_ns": "timing__setup__ws", "hold_ws_ns": "timing__hold__ws",
                 "setup_tns_ns": "timing__setup__tns", "hold_tns_ns": "timing__hold__tns"}
        metrics = {key: raw.get("globalroute__" + name) for key, name in names.items()}
        if all(isinstance(value, (int, float)) and math.isfinite(value) for value in metrics.values()):
            result["metrics"] = metrics
            result["metrics_sha256"] = digest(metrics_path)
    result["status"] = "passed" if "metrics" in result else "failed"
    if result["status"] != "passed" and not result.get("failure_reason"):
        result["failure_reason"] = "physical_stage_failed"
    return result


def timing_clean(run):
    if run.get("status") != "passed":
        return False
    m = run["metrics"]
    return all(m[key] >= 0 for key in ("setup_ws_ns", "hold_ws_ns", "setup_tns_ns", "hold_tns_ns"))


def pareto(baseline, reference, proof):
    result = {"comparable": False}
    if proof["status"] != "passed":
        return {**result, "reason": EquivalenceVerdict.NOT_PROVEN.value}
    if any(run.get("status") != "passed" for run in (baseline, reference)):
        return {**result, "reason": "physical_flow_failed"}
    if baseline["recipe_fingerprint_sha256"] != reference["recipe_fingerprint_sha256"]:
        return {**result, "reason": "recipe_mismatch"}
    b, r = baseline["metrics"], reference["metrics"]
    area = 100 * (b["area_um2"] - r["area_um2"]) / b["area_um2"]
    power = 100 * (b["power_w"] - r["power_w"]) / b["power_w"]
    setup = r["setup_ws_ns"] - b["setup_ws_ns"]
    hold = r["hold_ws_ns"] - b["hold_ws_ns"]
    values = (area, power, setup, hold)
    positive, negative = any(v > 0 for v in values), any(v < 0 for v in values)
    kind = "dominates" if positive and not negative else "tradeoff" if positive and negative else "regresses" if negative else "unchanged"
    return {"comparable": True, "classification": kind, "area_gain_pct": area,
            "power_gain_pct": power, "setup_slack_gain_ns": setup, "hold_slack_gain_ns": hold,
            "baseline_timing_clean": timing_clean(baseline), "reference_timing_clean": timing_clean(reference)}



def interpret_proof(result):
    log = Path(result["log"]).read_text(errors="replace") if Path(result["log"]).exists() else ""
    if result["status"] == "passed":
        return {"category": "proven", "optimization_conclusion": "eligible_for_physical_comparison"}
    if result["status"] == "timeout":
        return {"category": "proof_capacity_limited", "optimization_conclusion": "unknown",
                "reason": "formal_timeout_is_not_a_functional_failure"}
    if "model found" in log or "proof did fail" in log:
        return {"category": "counterexample", "optimization_conclusion": "not_equivalent_under_contract"}
    return {"category": "inconclusive_tool_failure", "optimization_conclusion": "unknown"}


def verify_latency(pair, directory):
    contract = LATENCY_CONTRACTS.get(pair["id"])
    if contract is None:
        return {"status": "unsupported_contract", "contract": None,
                "interpretation": {"category": "contract_missing", "optimization_conclusion": "unknown"}}
    if pair["id"] != "mac":
        raise ValueError(f"latency contract generator missing for {pair['id']}")
    harness = directory / "latency_contract.v"
    harness.write_text("""
module nebula_mac_latency_harness(
    input clk,
    input signed [7:0] a,
    input signed [7:0] b
);
    reg [3:0] phase = 0;
    wire contract_reset = phase < 2;
    wire signed [7:0] contract_a = contract_reset ? 0 : a;
    wire signed [7:0] contract_b = contract_reset ? 0 : b;
    wire signed [15:0] baseline_z;
    wire signed [15:0] reference_z;
    reg signed [15:0] reference_delay_0 = 0;
    reg signed [15:0] reference_delay_1 = 0;

    mac baseline(.clk(clk), .a(contract_a), .b(contract_b),
                 .reset(contract_reset), .z(baseline_z));
    mac_ref reference(.clk(clk), .a(contract_a), .b(contract_b),
                      .reset(contract_reset), .z(reference_z));

    always @(posedge clk) begin
        if (phase != 15)
            phase <= phase + 1;
        reference_delay_0 <= reference_z;
        reference_delay_1 <= reference_delay_0;
        if (phase >= 5)
            assert(baseline_z == reference_delay_1);
    end
endmodule
""")
    script = directory / "latency_proof.ys"
    script.write_text("\n".join([
        f"read_verilog -formal -sv {pair['baseline']['source']}",
        f"read_verilog -formal -sv {pair['reference']['source']}",
        f"read_verilog -formal -sv {harness}",
        "prep -flatten -top nebula_mac_latency_harness",
        "async2sync",
        "opt -full",
        "sat -verify -prove-asserts -seq 8 -tempinduct -maxsteps 24 -show-inputs -show-outputs nebula_mac_latency_harness",
    ]) + "\n")
    result = v2.run([v2.YOSYS, "-s", script], directory / "latency_proof.log", 120)
    result["status"] = ("passed" if result["exit_code"] == 0 else
                        "timeout" if result["exit_code"] == 124 else "unproven")
    result["script_sha256"] = digest(script)
    result["harness_sha256"] = digest(harness)
    result["contract"] = contract
    result["interpretation"] = interpret_proof(result)
    return result


def verify_pair(pair, directory):
    result = verify_latency(pair, directory) if pair["pair_class"] == "latency_changing" else v2.verify(pair, directory)
    if "interpretation" not in result:
        result["interpretation"] = interpret_proof(result)
    return result

def calibrate(pair, out):
    directory = out / pair["id"]
    checkpoint = directory / "result.json"
    if checkpoint.exists():
        prior = json.loads(checkpoint.read_text())
        if prior.get("runner_sha256") != pair["runner_sha256"]:
            raise ValueError("Runner changed; use a new --output experiment directory")
        return prior
    try:
        module = v2.elaborate(pair, directory)
        if module is None:
            pair.update(status="unsupported", promotion={"keep": False, "reason": pair.get("qualification_reason", "elaboration_failed")})
        else:
            pair["verification"] = verify_pair(pair, directory)
            proof_category = pair["verification"]["interpretation"]["category"]
            if proof_category != "proven":
                pair["status"] = ("proof_capacity_limited" if proof_category in
                                  {"proof_capacity_limited", "inconclusive_tool_failure"}
                                  else "contract_failed")
                pair["promotion"] = {"keep": False, "reason": proof_category}
                pair["pareto"] = {"comparable": False, "reason": proof_category}
                v2.write_json(checkpoint, pair)
                return pair
            sweep = []
            selected = None
            for period in CLOCK_GRID_NS:
                sweep_dir = directory / "timing_sweep" / f"{period:g}ns"
                recipe, _, profile = freeze(pair, sweep_dir, module, period, "baseline_timing_sweep")
                run = physical(pair, sweep_dir, "baseline", recipe, Path(pair["baseline"]["source"]), f"baseline_{period:g}ns")
                record = {"period_ns": period, "timing_profile": profile, "recipe_fingerprint_sha256": recipe.fingerprint_sha256, "baseline": run, "feasible": timing_clean(run)}
                sweep.append(record)
                if record["feasible"]:
                    selected = record
                    break
            pair["baseline_timing_sweep"] = sweep
            if selected is None:
                pair.update(status="not_selected", promotion={"keep": False, "reason": "no_baseline_feasible_period"})
            else:
                period = selected["period_ns"]
                pair_dir = directory / "paired_recipe"
                recipe, normalized, profile = freeze(pair, pair_dir, module, period, "paired_comparison")
                pair["selected_timing_point"] = {
                    "period_ns": period, "chosen_from": "baseline only",
                    "timing_profile": profile, "recipe_path": str(pair_dir / "physical_recipe.json"),
                    "recipe_fingerprint_sha256": recipe.fingerprint_sha256,
                }
                pair["ppa"] = {
                    "baseline": physical(pair, pair_dir, "baseline", recipe, Path(pair["baseline"]["source"]), "paired"),
                    "reference": physical(pair, pair_dir, "reference", recipe, normalized, "paired"),
                }
                pair["pareto"] = pareto(pair["ppa"]["baseline"], pair["ppa"]["reference"], pair["verification"])
                pair["promotion"] = v2.assess(pair["ppa"]["baseline"], pair["ppa"]["reference"], pair["verification"])
                if pair["promotion"]["keep"]:
                    pair["repeat_ppa"] = {
                        "baseline": physical(pair, pair_dir, "baseline", recipe, Path(pair["baseline"]["source"]), "paired", 1),
                        "reference": physical(pair, pair_dir, "reference", recipe, normalized, "paired", 1),
                    }
                    pair["repeat_promotion"] = v2.assess(pair["repeat_ppa"]["baseline"], pair["repeat_ppa"]["reference"], pair["verification"])
                    if not pair["repeat_promotion"]["keep"]:
                        pair["promotion"] = {"keep": False, "reason": "repeat_did_not_confirm"}
                pair["status"] = "promoted" if pair["promotion"]["keep"] else "analyzed"
    except Exception as exc:
        pair.update(status="error", promotion={"keep": False, "reason": f"{type(exc).__name__}: {exc}"})
    v2.write_json(checkpoint, pair)
    return pair


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "ai_runs/rtl_opt_calibration_v3")
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--pairs", nargs="*")
    parser.add_argument("--cohort-one", action="store_true",
                        help="run the declared six-pair diverse calibration cohort")
    parser.add_argument("--manifest-only", action="store_true")
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    manifest = prepare(out)
    if args.manifest_only:
        return
    if args.cohort_one and args.pairs:
        parser.error("--cohort-one and --pairs are mutually exclusive")
    selected_ids = set(DIVERSE_COHORT_ONE if args.cohort_one else (args.pairs or ()))
    manifest["selected_cohort"] = {
        "name": "diverse_cohort_one" if args.cohort_one else "explicit_or_all",
        "pair_ids": list(DIVERSE_COHORT_ONE) if args.cohort_one else sorted(selected_ids),
        "coverage": {
            "adder": "same-cycle sequential; previously promoted",
            "fsm": "reset-sensitive sequential and state encoding",
            "decoder_6bit": "combinational PPA tradeoff",
            "sub_16bit": "combinational reference timing violation",
            "calculation": "combinational proof-capacity limit",
            "mac": "latency-changing explicit contract",
        } if args.cohort_one else {},
    }
    chosen = [p for p in manifest["pairs"] if not selected_ids or p["id"] in selected_ids]
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = [pool.submit(calibrate, pair, out) for pair in chosen]
        completed = {}
        for future in as_completed(futures):
            result = future.result()
            completed[result["id"]] = result
            print(f"{result['id']}: {result['status']}: {result['promotion']['reason']}", flush=True)
            manifest["pairs"] = [completed.get(p["id"], p) for p in manifest["pairs"]]
            v2.write_json(out / "pair_manifest.json", manifest)
    counts = {state: sum(p["status"] == state for p in manifest["pairs"])
              for state in ("promoted", "analyzed", "not_selected", "proof_capacity_limited",
                            "contract_failed", "unsupported", "error", "pending")}
    v2.write_json(out / "summary.json", counts)
    print(json.dumps(counts), flush=True)


if __name__ == "__main__":
    main()
