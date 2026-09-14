import copy
import json
from pathlib import Path

import pytest

from design_manifest import DesignManifest, VerificationPolicy
from extract_candidate import CandidateSchemaError, StopRecommendation, extract_optimizer_response
from source_bundle import SourceBundle


def fixture_bundle(tmp_path: Path):
    (tmp_path / "top.v").write_text("module top(input a, output y); assign y=a; endmodule\n")
    manifest = DesignManifest(dataset="fixture", design_id="fixture", top="top", source_files=["top.v"],
                              language="verilog", verification=VerificationPolicy(), source_hashes={})
    manifest.refresh_source_hashes(tmp_path)
    return SourceBundle.from_manifest(manifest, tmp_path, verify_manifest_hashes=True)


def candidate(bundle):
    source = bundle.get_source("top.v")
    return {
        "schema_version": "3.1", "decision": "propose_candidate",
        "editing_base": "current_best",
        "chosen_pattern_id": None, "target_unit_id": "whole_design",
        "rationale": "fixture", "predicted_wns_gain_ns": None,
        "predicted_area_change_pct": None, "predicted_power_change_pct": None,
        "self_checks": {"interface_unchanged": True, "parameters_unchanged": True,
                        "latency_unchanged": True, "clock_reset_behavior_unchanged": True,
                        "source_bundle_targets_valid": True, "no_new_latches": True,
                        "no_new_clock_domains": True},
        "source_files": [{"target_file": "top.v", "verilog": source.text()}],
    }


def test_schema31_complete_source_and_stop(tmp_path):
    bundle = fixture_bundle(tmp_path)
    parsed = extract_optimizer_response(json.dumps(candidate(bundle)), bundle=bundle)
    assert parsed.complete_sources is not None
    stop = extract_optimizer_response(json.dumps({"schema_version": "3.1", "decision": "no_further_optimization",
                                                   "rationale": "no promising unexplored implementation"}))
    assert isinstance(stop, StopRecommendation)


def test_schema31_rejects_file_outside_selected_editing_base(tmp_path):
    bundle = fixture_bundle(tmp_path)
    payload = candidate(bundle)
    payload["source_files"][0]["target_file"] = "other.v"
    with pytest.raises(CandidateSchemaError):
        extract_optimizer_response(json.dumps(payload), bundle=bundle)
