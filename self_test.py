#!/usr/bin/env python3
"""Pure-Python contract tests. No Verilog files, EDA tools, or API calls."""
import copy
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "timing_automation"))
from extract_candidate import extract_candidate, extract_optimizer_response, StopRecommendation, CandidateSchemaError, make_self_checks
from lint_candidate import validate_candidate_against_request
from build_request import canonicalize_primary_focus, BuildRequestError
from run_optimizer import call_groq, OptimizerError
from jsonschema import Draft202012Validator


def main():
    schema = json.loads((ROOT / "timing_automation/response_schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    assert schema["properties"]["schema_version"]["const"] == "3.0"
    assert "source_files" in schema["required"] and "patches" not in schema["properties"]
    prefreeze_schema = json.loads((ROOT / "timing_automation/response_schema_prefreeze.json").read_text())
    Draft202012Validator.check_schema(prefreeze_schema)
    assert len(prefreeze_schema["oneOf"]) == 2
    # Opaque in-memory strings exercise byte preservation without reading/parsing RTL.
    contents = {"alpha.v": b"header\nbody\nfooter\n", "beta.v": b"unchanged\n"}
    sources = {name: SimpleNamespace(content=data, sha256=hashlib.sha256(data).hexdigest())
               for name, data in contents.items()}
    bundle = SimpleNamespace(source_names=lambda: list(sources), get_source=lambda name: sources[name])
    payload = {"schema_version": "3.0", "chosen_pattern_id": None, "target_unit_id": "unit_000",
               "rationale": "Self-test", "predicted_wns_gain_ns": None,
               "predicted_area_change_pct": None, "predicted_power_change_pct": None,
               "self_checks": make_self_checks(), "source_files": [
                   {"target_file": name, "original_sha256": source.sha256,
                    "verilog": source.content.decode()} for name, source in sources.items()]}
    candidate = extract_candidate(json.dumps(payload), bundle=bundle)
    assert candidate.complete_sources == contents and candidate.patches == ()
    print("PASS: schema v3 and complete multi-file byte preservation")
    stop = extract_optimizer_response(json.dumps({
        "schema_version": "3.1", "decision": "no_further_optimization",
        "rationale": "fixture convergence recommendation"
    }))
    assert isinstance(stop, StopRecommendation)
    print("PASS: pre-freeze schema 3.1 advisory stop branch")
    mutations = [lambda p: p["source_files"].pop(),
                 lambda p: p["source_files"].append(copy.deepcopy(p["source_files"][0])),
                 lambda p: p["source_files"][0].update(target_file="../outside.v"),
                 lambda p: p["source_files"][0].update(original_sha256="0" * 64),
                 lambda p: p.update(patches=[])]
    for mutate in mutations:
        bad = copy.deepcopy(payload)
        mutate(bad)
        try:
            extract_candidate(json.dumps(bad), bundle=bundle)
        except CandidateSchemaError:
            pass
        else:
            raise AssertionError("invalid response accepted")
    print("PASS: missing/duplicate/outside files, stale hashes, and patch-shaped responses rejected")
    scope = SimpleNamespace(unit_id="unit_000", recommended_pattern_ids=(),
                            primary_file="alpha.v", recommended_focus_ranges=(),
                            source_hashes={n: s.sha256 for n, s in sources.items()}, module_to_file={})
    changed = copy.deepcopy(payload)
    changed["source_files"][0]["verilog"] = "header\nnew body\nfooter\n"
    candidate = extract_candidate(json.dumps(changed), bundle=bundle)
    checks, _ = validate_candidate_against_request(candidate, scope, [(1, 1), (3, 3)])
    assert checks["frozen_ranges_untouched"] and checks["source_bundle_targets_valid"]
    checks, _ = validate_candidate_against_request(candidate, scope, [(2, 2)])
    assert not checks["frozen_ranges_untouched"]
    candidate.payload["target_unit_id"] = "wrong"
    checks, _ = validate_candidate_against_request(candidate, scope, [])
    assert not checks["target_unit_matches_request"]
    print("PASS: protected-line edits and wrong optimization units rejected")
    focus = {"file": "/example/alpha.v", "module": "alpha", "original_sha256": sources["alpha.v"].sha256}
    context = {"module_to_file": {"alpha": "alpha.v"}, "sources": [{"path": "alpha.v", "sha256": sources["alpha.v"].sha256}]}
    canonicalize_primary_focus(focus, context)
    assert focus["file"] == "alpha.v"
    print("PASS: canonical source path identity")
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs: SimpleNamespace(choices=[]))))
    with patch.dict("os.environ", {"GROQ_API_KEY": "self-test-only"}), patch.dict(sys.modules, {"groq": SimpleNamespace(Groq=lambda: client)}):
        try:
            call_groq(system_prompt="", user_prompt="", model="test", max_tokens=1, reasoning_effort="low")
        except OptimizerError as exc:
            assert "no completion choices" in str(exc)
        else:
            raise AssertionError("empty provider response accepted")
    print("PASS: empty provider response handling (mock only)")
    from test_guided_rtl_opt import run_tests
    run_tests()
    print("SELF-TEST PASSED: no RTL files read, no EDA tools started, no API calls")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
