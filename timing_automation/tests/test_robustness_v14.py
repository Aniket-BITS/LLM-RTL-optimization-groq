import json
from pathlib import Path

from aggregate_results import collect
from design_capabilities import analyze_design_capabilities
from design_manifest import DesignManifest, VerificationPolicy
from extract_candidate import ExtractedCandidate
from lint_candidate import RequestScope, validate_candidate_against_request
from run_rtl_opt_batch import summary_status


def test_modern_model_self_checks_are_advisory():
    source = b"module top(input a, output y); assign y=a; endmodule\n"
    import hashlib
    digest = hashlib.sha256(source).hexdigest()
    candidate = ExtractedCandidate(
        payload={"schema_version": "3.3", "decision": "propose_candidate", "target_unit_id": "whole_design", "self_checks": {"interface_unchanged": False}},
        patches=(), patch_types=(), bundle_validated=True,
        complete_sources={"top.v": source}, original_sources={"top.v": source},
    )
    scope = RequestScope(
        unit_id="whole_design", optimizer_mode="guided", source_hashes={"top.v": digest},
        module_to_file={"top": "top.v"}, primary_file="top.v", primary_module="top",
        recommended_pattern_ids=(), recommended_focus_ranges=(),
    )
    checks, reasons = validate_candidate_against_request(candidate, scope, ())
    assert checks["model_self_checks_advisory"] is True
    assert checks["model_self_checks_all_true"] is False
    assert not any("self_checks" in reason for reason in reasons)


def test_design_capabilities_flags_signed_and_memory(tmp_path: Path):
    src = tmp_path / "top.v"
    src.write_text("module top(input clk,input signed [7:0] a, output [7:0] y); reg [7:0] mem [0:31]; assign y=mem[a[4:0]]; endmodule\n")
    manifest = DesignManifest(
        dataset="fixture", design_id="top", top="top", source_files=["top.v"], language="verilog",
        verification=VerificationPolicy(backend="yosys", mode="sequential_equivalence", options={}),
        source_hashes={"top.v": __import__('hashlib').sha256(src.read_bytes()).hexdigest()},
    )
    result = analyze_design_capabilities(
        pair={"id": "top", "baseline": {"edge_signals": ["clk"]}, "timing_profile": {"clock_port": "clk"}},
        manifest=manifest, base_dir=tmp_path, elaborated_module={},
        latency_contract={"supported": True, "reason": None},
    )
    assert result["signed_top_ports_detected"] is True
    assert result["contains_memory_or_register_array"] is True
    assert result["expected_formal_complexity"] == "high"


def test_aggregate_preserves_required_fields(tmp_path: Path):
    d = tmp_path / "guided" / "repetition_01" / "demo"
    d.mkdir(parents=True)
    (d / "result.json").write_text(json.dumps({
        "id": "demo",
        "attempts": [{
            "attempt_number": 1, "status": "promoted", "optimization_mode": "guided",
            "groq_usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
            "guidance": {"path_groups_sent_to_llm": 2},
            "candidate_bundle_sha256": "abc", "formal": {"status": "passed"},
            "latency_result": {"status": "verified", "verified_total_latency_regression_cycles": 1},
            "ppa": {"candidate": {
                "failure_reason": "physical_stage_failed",
                "sta_netlist_compat": {"applied": True},
            }}, "primary_transformation": "pipeline_insertion",
            "proposed_transformations": ["pipeline_insertion"],
        }]
    }))
    rows = collect(tmp_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["design"] == "demo"
    assert row["prompt_tokens"] == 10
    assert row["path_groups_considered"] == 2
    assert row["formal_equivalence_result"] == "passed"
    assert row["verified_latency_delta_cycles"] == 1
    assert row["sta_netlist_compat_applied"] is True
    assert row["physical_failure_reason"] == "physical_stage_failed"


def test_batch_summary_status(tmp_path: Path):
    assert summary_status(tmp_path) == (False, None)
    (tmp_path / "groq_summary.json").write_text(json.dumps({"results": [{"status": "converged"}]}))
    assert summary_status(tmp_path) == (True, "converged")


def test_calibration_physical_sanitizes_between_synth_and_grt(tmp_path: Path, monkeypatch):
    import calibrate_pairs_v3 as cal

    flow = tmp_path / "flow"
    flow.mkdir()
    monkeypatch.setattr(cal, "ROOT", flow)
    src = tmp_path / "demo.v"
    src.write_text("module demo(input signed [7:0] a, output [7:0] y); assign y=a; endmodule\n")
    work = tmp_path / "work"
    work.mkdir()
    (work / "config.mk").write_text("# fixture\n")

    class Recipe:
        fingerprint_sha256 = "a" * 64
        def verify_frozen_files(self):
            return {"ok": True}

    calls = []
    def fake_run(cmd, log, timeout):
        calls.append(list(cmd))
        variant = next(x.split("=", 1)[1] for x in cmd if x.startswith("FLOW_VARIANT="))
        result_dir = flow / "results" / "nangate45" / "rtlopt_demo" / variant
        result_dir.mkdir(parents=True, exist_ok=True)
        net = result_dir / "1_2_yosys.v"
        if str(cmd[-1]).endswith("/1_2_yosys.v"):
            net.write_text("module demo(a,y); input signed [7:0] a; output [7:0] y; BUF_X1 u(.A(a[0]),.Z(y[0])); endmodule\n")
            return {"exit_code": 0, "status": "passed", "log": str(log)}
        assert cmd[-1] == "grt"
        assert " signed " not in net.read_text()
        metrics_dir = flow / "logs" / "nangate45" / "rtlopt_demo" / variant
        metrics_dir.mkdir(parents=True, exist_ok=True)
        (metrics_dir / "5_1_grt.json").write_text(json.dumps({
            "globalroute__design__instance__area": 1.0,
            "globalroute__power__total": 0.1,
            "globalroute__timing__setup__ws": 0.01,
            "globalroute__timing__hold__ws": 0.01,
            "globalroute__timing__setup__tns": 0.0,
            "globalroute__timing__hold__tns": 0.0,
        }))
        return {"exit_code": 0, "status": "passed", "log": str(log)}

    monkeypatch.setattr(cal.v2, "run", fake_run)
    result = cal.physical(
        {"id": "demo"}, work, "baseline", Recipe(), src,
        "fixture", stage="grt", timeout_s=10,
    )
    assert result["status"] == "passed"
    assert result["sta_netlist_compat"]["applied"] is True
    raw = Path(result["synthesis_netlist_raw"])
    sta = Path(result["synthesis_netlist_sta"])
    assert "signed" in raw.read_text()
    assert " signed " not in sta.read_text()
    assert calls[0][-1].endswith("/1_2_yosys.v")
    assert calls[0][-1].startswith("results/")
    assert not calls[0][-1].startswith("/")
    assert calls[0][-1] != "synth"
    assert calls[1][-1] == "grt"


def test_candidate_flow_uses_exact_yosys_target_not_phony_synth(tmp_path: Path):
    import run_candidate_flow as rcf

    class Recipe:
        stage = "global_route"
        platform = "nangate45"
        design_id = "rtlopt_demo"

    original = [
        "make",
        "DESIGN_CONFIG=/tmp/config.mk",
        "FLOW_VARIANT=demo_variant",
        "VERILOG_FILES=/tmp/demo.v",
        "grt",
    ]
    cmd, target = rcf.build_yosys_checkpoint_command(
        command=original, recipe=Recipe(), flow_dir=tmp_path, flow_variant="demo_variant"
    )
    assert cmd[-1] == "results/nangate45/rtlopt_demo/demo_variant/1_2_yosys.v"
    assert not cmd[-1].startswith("/")
    assert target.name == "1_2_yosys.v"
    assert cmd[-1] != "synth"
    assert "grt" not in cmd


def test_candidate_flow_synth_stage_still_intercepts_before_synth_odb(tmp_path: Path):
    import run_candidate_flow as rcf

    class Recipe:
        stage = "synth"
        platform = "nangate45"
        design_id = "rtlopt_demo"

    original = [
        "make",
        "DESIGN_CONFIG=/tmp/config.mk",
        "FLOW_VARIANT=demo_variant",
        "VERILOG_FILES=/tmp/demo.v",
        "synth",
    ]
    cmd, target = rcf.build_yosys_checkpoint_command(
        command=original, recipe=Recipe(), flow_dir=tmp_path, flow_variant="demo_variant"
    )
    assert cmd[-1] == "results/nangate45/rtlopt_demo/demo_variant/1_2_yosys.v"
    assert not cmd[-1].startswith("/")
    assert target.name == "1_2_yosys.v"
    assert cmd[-1] != "synth"
    assert "synth" not in cmd[1:]
