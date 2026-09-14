from __future__ import annotations

import json
from pathlib import Path

from design_manifest import DesignManifest, VerificationPolicy
from latency_contract import build_latency_contract, latency_policy_for_prompt
from latency_verifier import cycle_exact_result, verify_fixed_latency
from extract_candidate import extract_optimizer_response
from source_bundle import SourceBundle


def _manifest(root: Path, text: str, *, top: str = "top") -> DesignManifest:
    root.mkdir(parents=True, exist_ok=True)
    (root / "top.v").write_text(text, encoding="utf-8")
    m = DesignManifest(
        dataset="fixture", design_id="fixture", top=top,
        source_files=["top.v"], language="verilog",
        verification=VerificationPolicy(backend="yosys", mode="sequential_equivalence", options={"timeout_s": 120}),
        source_hashes={},
    )
    m.refresh_source_hashes(root)
    return m


def _module_ports():
    return {
        "ports": {
            "clk": {"direction": "input", "bits": [1]},
            "a": {"direction": "input", "bits": [2, 3, 4, 5]},
            "y": {"direction": "output", "bits": [6, 7, 8, 9]},
        }
    }


def _pair(clock=True):
    return {
        "baseline": {"edge_signals": ["clk"] if clock else []},
        "timing_profile": {"clock_port": "clk" if clock else None},
    }


def test_budget_zero_preserves_cycle_exact_contract(tmp_path):
    m = _manifest(tmp_path, "module top(input clk,input [3:0] a,output reg [3:0] y); always @(posedge clk) y<=a; endmodule\n")
    c = build_latency_contract(pair=_pair(), manifest=m, base_dir=tmp_path,
                               elaborated_module=_module_ports(), max_latency_regression_cycles=0)
    assert c.supported is True
    assert c.verification_mode == "cycle_exact_existing"
    assert c.max_latency_regression_cycles == 0


def test_positive_latency_budget_requires_existing_clock(tmp_path):
    m = _manifest(tmp_path, "module top(input [3:0] a,output [3:0] y); assign y=a; endmodule\n")
    ports = {"ports": {"a": {"direction": "input", "bits": [1,2,3,4]}, "y": {"direction": "output", "bits": [5,6,7,8]}}}
    c = build_latency_contract(pair=_pair(clock=False), manifest=m, base_dir=tmp_path,
                               elaborated_module=ports, max_latency_regression_cycles=1)
    assert c.supported is False
    assert "no backend-declared reference clock" in (c.reason or "")


def test_latency_policy_is_golden_relative(tmp_path):
    m = _manifest(tmp_path, "module top(input clk,input [3:0] a,output reg [3:0] y); always @(posedge clk) y<=a; endmodule\n")
    c = build_latency_contract(pair=_pair(), manifest=m, base_dir=tmp_path,
                               elaborated_module=_module_ports(), max_latency_regression_cycles=2)
    p = latency_policy_for_prompt(c, current_best_total_latency_regression_cycles=1)
    assert p["reference"] == "frozen_golden"
    assert p["max_latency_regression_cycles"] == 2
    assert p["current_best_total_latency_regression_cycles"] == 1
    assert p["remaining_latency_headroom_cycles"] == 1


def test_temporal_verifier_uses_prediction_only_as_search_hint(tmp_path):
    gold_dir = tmp_path / "gold"
    cand_dir = tmp_path / "cand"
    text = "module top(input clk,input [3:0] a,output reg [3:0] y); always @(posedge clk) y<=a; endmodule\n"
    gm = _manifest(gold_dir, text)
    cm = _manifest(cand_dir, text)
    contract = build_latency_contract(pair=_pair(), manifest=gm, base_dir=gold_dir,
                                      elaborated_module=_module_ports(), max_latency_regression_cycles=2)

    calls = []
    def fake_run(command, log, timeout):
        shift = int(Path(log).parent.name.split("_")[-1])
        calls.append(shift)
        Path(log).write_text("proof did fail\n" if shift != 1 else "SUCCESS\n", encoding="utf-8")
        return {"exit_code": 0 if shift == 1 else 1, "status": "passed" if shift == 1 else "failed", "log": str(log)}

    result = verify_fixed_latency(
        golden_manifest=gm, golden_base_dir=gold_dir,
        candidate_manifest=cm, candidate_base_dir=cand_dir,
        elaborated_golden_module=_module_ports(), contract=contract,
        output_dir=tmp_path / "proof", yosys_exe="yosys", run_command=fake_run,
        current_best_total_latency_regression_cycles=0,
        predicted_latency_change_cycles=2,
    )
    # Prediction 2 is tried first, but backend continues and discovers the true +1 relation.
    assert calls[:2] == [2, 0] or calls[:2] == [2, 0]
    assert 1 in calls
    assert result["status"] == "verified"
    assert result["verified_total_latency_regression_cycles"] == 1
    assert result["prediction_error_cycles"] == -1


def test_temporal_verifier_distinguishes_over_budget_equivalence(tmp_path):
    gold_dir = tmp_path / "gold"
    cand_dir = tmp_path / "cand"
    text = "module top(input clk,input [3:0] a,output reg [3:0] y); always @(posedge clk) y<=a; endmodule\n"
    gm = _manifest(gold_dir, text)
    cm = _manifest(cand_dir, text)
    contract = build_latency_contract(pair=_pair(), manifest=gm, base_dir=gold_dir,
                                      elaborated_module=_module_ports(), max_latency_regression_cycles=1)

    def fake_run(command, log, timeout):
        shift = int(Path(log).parent.name.split("_")[-1])
        Path(log).write_text("SUCCESS\n" if shift == 2 else "proof did fail\n", encoding="utf-8")
        return {"exit_code": 0 if shift == 2 else 1, "status": "passed" if shift == 2 else "failed", "log": str(log)}

    result = verify_fixed_latency(
        golden_manifest=gm, golden_base_dir=gold_dir,
        candidate_manifest=cm, candidate_base_dir=cand_dir,
        elaborated_golden_module=_module_ports(), contract=contract,
        output_dir=tmp_path / "proof", yosys_exe="yosys", run_command=fake_run,
        current_best_total_latency_regression_cycles=0,
        predicted_latency_change_cycles=2,
    )
    assert result["status"] == "latency_budget_exceeded"
    assert result["passed"] is False
    assert result["verified_total_latency_regression_cycles"] == 2


def test_cycle_exact_result_taxonomy():
    assert cycle_exact_result({"status": "passed"})["status"] == "verified"
    assert cycle_exact_result({"status": "timeout"})["status"] == "inconclusive_timeout"
    # A bare historical "unproven" result is ambiguous; do not blame the RTL.
    assert cycle_exact_result({"status": "unproven"})["status"] == "tool_failure"


def test_positive_latency_contract_tracks_negedge_clock(tmp_path):
    text = "module top(input clk,input [3:0] a,output reg [3:0] y); always @(negedge clk) y<=a; endmodule\n"
    m = _manifest(tmp_path, text)
    c = build_latency_contract(pair=_pair(), manifest=m, base_dir=tmp_path,
                               elaborated_module=_module_ports(), max_latency_regression_cycles=1)
    assert c.supported is True
    assert c.clock_edge == "negedge"


def test_positive_latency_contract_rejects_mixed_clock_edges(tmp_path):
    text = (
        "module top(input clk,input [3:0] a,output reg [3:0] y); "
        "always @(posedge clk) y<=a; always @(negedge clk) y<=a; endmodule\n"
    )
    m = _manifest(tmp_path, text)
    c = build_latency_contract(pair=_pair(), manifest=m, base_dir=tmp_path,
                               elaborated_module=_module_ports(), max_latency_regression_cycles=1)
    assert c.supported is False
    assert "multiple active edges" in (c.reason or "")


def test_schema_32_candidate_parses_without_backend_identity_echo(tmp_path):
    m = _manifest(tmp_path, "module top(input clk,input [3:0] a,output reg [3:0] y); always @(posedge clk) y<=a; endmodule\n")
    bundle = SourceBundle.from_manifest(m, tmp_path, verify_manifest_hashes=True)
    payload = {
        "schema_version": "3.3",
        "decision": "propose_candidate",
        "editing_base": "current_best",
        "primary_transformation": "pipeline_insertion",
        "proposed_transformations": ["pipeline_insertion"],
        "chosen_pattern_id": "pipeline.insert_stage",
        "target_unit_id": "whole_design",
        "rationale": "split critical logic with an existing clock",
        "predicted_wns_gain_ns": 0.1,
        "predicted_area_change_pct": 1.0,
        "predicted_power_change_pct": 1.0,
        "predicted_latency_change_cycles": 1,
        "sequential_changes": [{"kind": "pipeline_register", "clock_domain": "clk", "rationale": "timing"}],
        "self_checks": {
            "interface_unchanged": True,
            "parameters_unchanged": True,
            "latency_policy_considered": True,
            "clock_reset_behavior_unchanged": True,
            "uses_only_permitted_clock_reset_domains": True,
            "intentional_sequential_changes_declared": True,
            "source_bundle_targets_valid": True,
            "no_new_latches": True,
            "no_new_clock_domains": True
        },
        "source_files": [{"target_file": "top.v", "verilog": (tmp_path / "top.v").read_text()}]
    }
    parsed = extract_optimizer_response(json.dumps(payload), bundle=bundle)
    assert parsed.editing_base == "current_best"
    assert parsed.payload["predicted_latency_change_cycles"] == 1
    assert "editing_base_identity" not in parsed.payload


def test_ppa_gate_rejects_unpassed_latency_before_metrics():
    from evaluate_candidate import assess_guided_candidate
    out = assess_guided_candidate(
        {}, {}, {"status": "passed"},
        {
            "max_area_regression_pct": 0.5,
            "max_power_regression_pct": 0.5,
            "max_setup_slack_regression_ns": 0.01,
            "max_hold_slack_regression_ns": 0.01,
        },
        comparison_reference="current_best.epoch_000",
        latency_result={"status": "latency_budget_exceeded", "passed": False},
    )
    assert out["keep"] is False
    assert out["comparable"] is False
    assert out["reason"] == "latency_policy_failed"


def test_unsupported_latency_contract_advertises_zero_effective_headroom(tmp_path):
    m = _manifest(tmp_path, "module top(input [3:0] a,output [3:0] y); assign y=a; endmodule\n")
    ports = {"ports": {"a": {"direction": "input", "bits": [1,2,3,4]}, "y": {"direction": "output", "bits": [5,6,7,8]}}}
    c = build_latency_contract(pair=_pair(clock=False), manifest=m, base_dir=tmp_path,
                               elaborated_module=ports, max_latency_regression_cycles=2)
    p = latency_policy_for_prompt(c, current_best_total_latency_regression_cycles=0)
    assert c.supported is False
    assert p["max_latency_regression_cycles"] == 2
    assert p["effective_max_latency_regression_cycles"] == 0
    assert p["remaining_latency_headroom_cycles"] == 0


def test_no_bounded_shift_proven_is_not_mislabeled_as_global_semantic_failure(tmp_path):
    gold_dir = tmp_path / "gold"
    cand_dir = tmp_path / "cand"
    text = "module top(input clk,input [3:0] a,output reg [3:0] y); always @(posedge clk) y<=a; endmodule\n"
    gm = _manifest(gold_dir, text)
    cm = _manifest(cand_dir, text)
    contract = build_latency_contract(pair=_pair(), manifest=gm, base_dir=gold_dir,
                                      elaborated_module=_module_ports(), max_latency_regression_cycles=1)

    def fake_run(command, log, timeout):
        Path(log).write_text("proof did fail\n", encoding="utf-8")
        return {"exit_code": 1, "status": "failed", "log": str(log)}

    result = verify_fixed_latency(
        golden_manifest=gm, golden_base_dir=gold_dir,
        candidate_manifest=cm, candidate_base_dir=cand_dir,
        elaborated_golden_module=_module_ports(), contract=contract,
        output_dir=tmp_path / "proof", yosys_exe="yosys", run_command=fake_run,
    )
    assert result["status"] == "no_allowed_latency_relation"
    assert result["formal_status"] == "no_allowed_latency_relation"


def test_temporal_proof_flattens_completed_harness(tmp_path):
    from latency_verifier import _proof_script
    gold_dir = tmp_path / "gold"
    cand_dir = tmp_path / "cand"
    text = "module top(input clk,input [3:0] a,output reg [3:0] y); always @(posedge clk) y<=a; endmodule\n"
    gm = _manifest(gold_dir, text)
    cm = _manifest(cand_dir, text)
    wrapper = tmp_path / "latency_harness.v"
    wrapper.write_text("module nebula_latency_harness; endmodule\n", encoding="utf-8")
    script = _proof_script(
        golden_manifest=gm, golden_base_dir=gold_dir,
        candidate_manifest=cm, candidate_base_dir=cand_dir,
        wrapper=wrapper, maxsteps=24, seq_depth=5,
    )
    assert "prep -flatten -top nebula_latency_harness" in script
    assert "prep -top nebula_latency_harness\n" not in script


def test_prompt_requires_cycle_alignment_for_pipeline_boundaries():
    from prompt_renderer import render_groq_prompt
    req = {
        "unit_id": "whole_design",
        "golden": {"source_bundle": {"top": "top", "files": []}},
        "current_best": {"source_bundle": {"top": "top", "files": []}},
        "editing_base_policy": {},
        "candidate_contract": {},
        "current_best_evidence": {},
        "previous_attempts": [],
        "latency_policy": {
            "reference": "frozen_golden",
            "max_latency_regression_cycles": 1,
            "current_best_total_latency_regression_cycles": 0,
            "remaining_latency_headroom_cycles": 1,
            "model_prediction_authority": "advisory_only",
        },
        "guidance": {
            "schema": "guidance_packet/1",
            "mode_used": "exploratory",
            "groups": [],
            "recommendations": [],
            "prior_attempt_outcomes": [],
        },
    }
    system, _user, _packet, _budget = render_groq_prompt(req, schema={})
    assert "preserve transaction-cycle alignment" in system
    assert "mux/select controls" in system
    assert "Do not pipeline only one branch" in system
