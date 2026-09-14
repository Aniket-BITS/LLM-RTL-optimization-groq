"""Focused pure-Python tests for Golden / Current-Best pre-freeze semantics."""
from __future__ import annotations

import json
from pathlib import Path

from design_manifest import DesignManifest, VerificationPolicy
from extract_candidate import (
    StopRecommendation, extract_optimizer_response, CandidateSourceBundleFileSetError,
)
from source_bundle import SourceBundle
import run_rtl_opt_groq as runner
import guidance_packet


def make_manifest(root: Path) -> DesignManifest:
    (root / "top.v").write_text("module top(input a, output y); assign y = a; endmodule\n", encoding="utf-8")
    m = DesignManifest(
        dataset="fixture", design_id="fixture", top="top", source_files=["top.v"],
        language="verilog", verification=VerificationPolicy(), source_hashes={},
    )
    m.refresh_source_hashes(root)
    m.write_json(root / "design_manifest.json")
    return m


def test_source_bundle_identity_ignores_comments_and_layout(tmp_path):
    manifest = make_manifest(tmp_path)
    original = SourceBundle.from_manifest(manifest, tmp_path, verify_manifest_hashes=True)
    executable = original.executable_identity_sha256()
    (tmp_path / "top.v").write_text("// comment\nmodule top ( input a , output y ) ; assign y = a ; endmodule\n", encoding="utf-8")
    manifest.refresh_source_hashes(tmp_path)
    modified = SourceBundle.from_manifest(manifest, tmp_path, verify_manifest_hashes=True)
    assert original.bundle_sha256() != modified.bundle_sha256()
    assert executable == modified.executable_identity_sha256()


def test_stop_response_is_advisory_type():
    raw = json.dumps({"schema_version": "3.1", "decision": "no_further_optimization", "rationale": "explored implementations converged"})
    result = extract_optimizer_response(raw)
    assert isinstance(result, StopRecommendation)


def test_candidate_uses_backend_selected_editing_base(tmp_path):
    manifest = make_manifest(tmp_path)
    bundle = SourceBundle.from_manifest(manifest, tmp_path, verify_manifest_hashes=True)
    source = bundle.get_source("top.v")
    payload = {
        "schema_version": "3.1",
        "decision": "propose_candidate",
        "editing_base": "golden",
        "chosen_pattern_id": "logic_restructuring",
        "target_unit_id": "whole_design",
        "rationale": "real executable rewrite",
        "predicted_wns_gain_ns": None,
        "predicted_area_change_pct": None,
        "predicted_power_change_pct": None,
        "self_checks": {
            "interface_unchanged": True,
            "parameters_unchanged": True,
            "latency_unchanged": True,
            "clock_reset_behavior_unchanged": True,
            "source_bundle_targets_valid": True,
            "no_new_latches": True,
            "no_new_clock_domains": True,
        },
        "source_files": [{
            "target_file": "top.v",
            "verilog": "module top(input a, output y); wire n = a; assign y = n; endmodule\n",
        }],
    }
    parsed = extract_optimizer_response(json.dumps(payload), bundle=bundle)
    assert parsed.editing_base == "golden"
    assert set(parsed.complete_sources or {}) == {"top.v"}
    # Backend-selected bundle remains authoritative: an invalid returned file set
    # is rejected even though the model no longer echoes trusted hashes.
    payload["source_files"][0]["target_file"] = "other.v"
    try:
        extract_optimizer_response(json.dumps(payload), bundle=bundle)
    except CandidateSourceBundleFileSetError as exc:
        assert exc.expected_files == ("top.v",)
        assert exc.returned_files == ("other.v",)
        assert exc.missing_files == ("top.v",)
        assert exc.unexpected_files == ("other.v",)
    else:
        raise AssertionError("candidate file set outside selected editing base was accepted")


def test_model_stop_backend_evidence_is_epoch_scoped():
    attempts = [
        {"attempt": 1, "current_best_epoch": 0, "synthesis_duplicate": False},
        {"attempt": 2, "current_best_epoch": 0, "synthesis_duplicate": True},
    ]
    assert runner.model_stop_convergence_evidence(
        attempts, {"A": {}, "B": {}}, epoch=0,
        min_unique_rejected_syntheses=2, duplicate_limit=2,
    ) is not None
    assert runner.model_stop_convergence_evidence(
        attempts, {}, epoch=1,
        min_unique_rejected_syntheses=2, duplicate_limit=2,
    ) is None


def test_guidance_uses_matching_physical_run_source_path(tmp_path):
    snapshot_dir = tmp_path / "state" / "current_best" / "epoch_001"
    physical_dir = tmp_path / "attempt_01" / "paired_recipe"
    snapshot_dir.mkdir(parents=True)
    physical_dir.mkdir(parents=True)
    snapshot = snapshot_dir / "top.v"
    physical = physical_dir / "reference_physical.v"
    text = "module top(input a, output y); assign y = a; endmodule\n"
    snapshot.write_text(text, encoding="utf-8")
    physical.write_text(text, encoding="utf-8")
    import hashlib
    digest = hashlib.sha256(text.encode()).hexdigest()
    run = {
        "source_sha256": digest,
        "command": ["make", "VERILOG_FILES=" + str(physical), "grt"],
    }
    resolved = guidance_packet._physical_source_for_run(run, snapshot)
    assert resolved.resolve() == physical.resolve()


def test_guidance_rejects_physical_source_path_with_wrong_bytes(tmp_path):
    snapshot = tmp_path / "top.v"
    physical = tmp_path / "reference_physical.v"
    snapshot.write_text("module top; endmodule\n", encoding="utf-8")
    physical.write_text("module other; endmodule\n", encoding="utf-8")
    import hashlib
    digest = hashlib.sha256(snapshot.read_bytes()).hexdigest()
    run = {
        "source_sha256": digest,
        "command": ["VERILOG_FILES=" + str(physical)],
    }
    resolved = guidance_packet._physical_source_for_run(run, snapshot)
    assert resolved.resolve() == snapshot.resolve()
