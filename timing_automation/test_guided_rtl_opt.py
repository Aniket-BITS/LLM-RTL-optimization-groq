"""Pure-Python pre-freeze RTL optimizer fixtures.

These tests intentionally avoid Groq and EDA execution.  They cover prompt/history
semantics, PPA budgets, synthesis identities and convergence helpers.
"""
from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from evaluate_candidate import assess_guided_candidate
from guidance_packet import build_packet
from optimizer_history import compact_attempt_for_model, model_history_context, guided_attempt_record, retry_feedback
from prompt_renderer import render_groq_prompt
from report_guided_experiment import aggregate
from yosys_structure import normalized_synthesis_hash
import run_rtl_opt_groq as runner


BUDGETS = {
    "max_area_regression_pct": 0.5,
    "max_power_regression_pct": 0.5,
    "max_setup_slack_regression_ns": 0.010,
    "max_hold_slack_regression_ns": 0.010,
}


def metrics(**changes):
    return {
        "status": "passed",
        "recipe_fingerprint_sha256": "same",
        "metrics": {
            "area_um2": 100.0,
            "power_w": 1.0,
            "setup_ws_ns": 0.2,
            "hold_ws_ns": 0.1,
            "setup_tns_ns": 0.0,
            "hold_tns_ns": 0.0,
            **changes,
        },
    }


def fixture_packet(mode="guided"):
    return build_packet([], None, None, {}, mode=mode)


def prompt_request():
    source = {
        "schema": "nebula_source_bundle_identity/2",
        "top": "top",
        "bundle_sha256": "a" * 64,
        "executable_identity_sha256": "b" * 64,
        "sources": [{"path": "top.v", "sha256": "c" * 64, "text": "module top(input a, output y); assign y=a; endmodule\n"}],
        "module_to_file": {"top": "top.v"},
    }
    return {
        "unit_id": "whole_design",
        "guidance": fixture_packet("exploratory"),
        "golden": {"role": "golden", "epoch": 0, "design_identity": "golden.0", "source_bundle": source, "metrics": {}},
        "current_best": {"role": "current_best", "epoch": 0, "design_identity": "cb.0", "same_as_golden": True,
                         "source_bundle": {**source, "sources": [{"path": "top.v", "sha256": "c" * 64}]}, "metrics": {}},
        "previous_attempts": [],
        "editing_base_policy": {"allowed": ["golden", "current_best"]},
        "candidate_contract": {"cycle_visible_latency_unchanged": True},
        "user_ppa_budgets": BUDGETS,
        "current_best_evidence": {},
    }


class PromptAndHistoryTests(unittest.TestCase):
    def test_prompt_roles_and_authority(self):
        req = prompt_request()
        system, user, _packet, _budget = render_groq_prompt(req, {"oneOf": []})
        payload = json.loads(user)
        self.assertIn("GOLDEN RTL", system)
        self.assertIn("CURRENT BEST RTL", system)
        self.assertIn("no_further_optimization", system)
        self.assertEqual(payload["golden"]["design_identity"], "golden.0")
        self.assertEqual(payload["current_best"]["design_identity"], "cb.0")
        self.assertEqual(payload["editing_base_policy"]["allowed"], ["golden", "current_best"])


    def test_prompt_omits_duplicate_current_best_rtl_text(self):
        req = prompt_request()
        _system, user, _packet, _budget = render_groq_prompt(req, {"oneOf": []})
        payload = json.loads(user)
        self.assertTrue(payload["current_best"]["same_as_golden"])
        self.assertNotIn("text", payload["current_best"]["source_bundle"]["sources"][0])
        self.assertIn("text", payload["golden"]["source_bundle"]["sources"][0])

    def test_model_history_is_bounded_and_aggregated(self):
        rows = []
        for attempt in range(1, 8):
            rows.append({
                "attempt": attempt,
                "status": "analyzed",
                "meaningful_iteration": True,
                "meaningful_iteration_number": attempt,
                "primary_transformation": "pipeline_insertion" if attempt == 7 else "logic_restructuring",
                "proposed_transformations": ["pipeline_insertion"] if attempt == 7 else ["logic_restructuring"],
                "latency_result": {"iteration_latency_delta_cycles": 1 if attempt == 7 else 0,
                                   "verified_total_latency_regression_cycles": 1 if attempt == 7 else 0,
                                   "status": "verified"},
                "ppa_deltas": {"setup_slack_delta_ns": attempt / 1000.0, "area_delta_pct": -0.1},
            })
        recent, summary = model_history_context(rows, recent_limit=3)
        self.assertEqual([r["attempt"] for r in recent], [5, 6, 7])
        self.assertEqual(summary["meaningful_attempts"], 7)
        self.assertEqual(summary["pipeline_insertion_attempts"], 1)
        self.assertIn("logic_restructuring", summary["explored_transformations"])
        self.assertNotIn("normalized_synthesis_hash", json.dumps(recent))

    def test_compact_history_hides_raw_hash(self):
        row = {
            "attempt": 2,
            "status": "duplicate_after_synthesis",
            "editing_base": "current_best",
            "synthesis_duplicate": True,
            "synthesis_implementation_id": "synth_impl_001",
            "synthesis": {"candidate_hash": {"sha256": "f" * 64}},
            "duplicate_of_attempt": 1,
            "ppa_deltas": {},
        }
        compact = compact_attempt_for_model(row)
        self.assertEqual(compact["synthesis_implementation"], "synth_impl_001")
        self.assertNotIn("normalized_synthesis_hash", compact)
        self.assertNotIn("f" * 64, json.dumps(compact))
        self.assertIn("materially different", retry_feedback(row)["instruction"])

    def test_infrastructure_does_not_reject_family(self):
        row = {"attempt": 1, "status": "infrastructure_failed", "attempted_transformation_family": "logic"}
        self.assertFalse(guided_attempt_record(row)["rejected_transformation_family"])


class PolicyTests(unittest.TestCase):
    def assess(self, candidate, **budgets):
        return assess_guided_candidate(
            metrics(), candidate, {"status": "passed"}, {**BUDGETS, **budgets},
            comparison_reference="current_best.epoch_000",
        )

    def test_bounded_slack_regression(self):
        self.assertTrue(self.assess(metrics(area_um2=98.0, setup_ws_ns=0.195, hold_ws_ns=0.095))["keep"])
        self.assertFalse(self.assess(metrics(area_um2=98.0, setup_ws_ns=0.185))["keep"])
        self.assertFalse(self.assess(metrics(area_um2=98.0, hold_ws_ns=0.085))["keep"])

    def test_current_best_reference_is_recorded(self):
        result = self.assess(metrics(area_um2=98.0))
        self.assertEqual(result["comparison_reference"], "current_best.epoch_000")


class SynthesisAndConvergenceTests(unittest.TestCase):
    def test_normalized_hash_and_noop(self):
        graph = {"modules": {"top": {"ports": {"y": {"bits": [2], "direction": "output"}},
                  "cells": {"cell": {"type": "AND", "connections": {"A": [2], "B": [3]}, "parameters": {}, "attributes": {"src": "old"}}},
                  "netnames": {"n": {"bits": [2], "attributes": {"init": "0"}}}}}}
        h = normalized_synthesis_hash(graph)
        clone = copy.deepcopy(graph)
        clone["modules"]["top"]["cells"]["cell"]["attributes"]["src"] = "new"
        self.assertEqual(h, normalized_synthesis_hash(clone))
        self.assertTrue(runner.synthesis_no_op({**h, "status": "passed"}, {**h, "status": "passed"}))

    def test_convergence_is_current_best_epoch_scoped(self):
        seen = {"A": {"attempt": 1}, "B": {"attempt": 3}}
        attempts = [
            {"attempt": 1, "current_best_epoch": 0, "synthesis_duplicate": False},
            {"attempt": 2, "current_best_epoch": 0, "synthesis_duplicate": True},
            {"attempt": 3, "current_best_epoch": 0, "synthesis_duplicate": False},
            {"attempt": 4, "current_best_epoch": 0, "synthesis_duplicate": True},
        ]
        verdict = runner.convergence_state(attempts, seen, epoch=0,
                                           min_unique_rejected_syntheses=2, duplicate_limit=2)
        self.assertIsNotNone(verdict)
        self.assertEqual(verdict["trigger_attempt"], 4)
        self.assertIsNone(runner.convergence_state(attempts, {}, epoch=1,
                                                   min_unique_rejected_syntheses=2, duplicate_limit=2))

    def test_model_stop_needs_backend_evidence(self):
        attempts = [
            {"attempt": 1, "current_best_epoch": 0, "synthesis_duplicate": False},
            {"attempt": 2, "current_best_epoch": 0, "synthesis_duplicate": True},
        ]
        self.assertIsNone(runner.model_stop_convergence_evidence(
            attempts, {"A": {}}, epoch=0, min_unique_rejected_syntheses=2, duplicate_limit=2))
        self.assertIsNotNone(runner.model_stop_convergence_evidence(
            attempts, {"A": {}, "B": {}}, epoch=0, min_unique_rejected_syntheses=2, duplicate_limit=2))


class ReportTests(unittest.TestCase):
    def test_aggregate_tracks_duplicate_and_stop_types(self):
        rows = [{
            "id": "fixture", "arm": "guided", "status": "converged", "current_best_epoch": 1,
            "attempts": [
                {"groq_call_attempted": True, "status": "promoted", "promotion": {"comparable": True},
                 "ppa_deltas": {"area_delta_pct": -2.0}, "guidance": {"mode_used": "guided"}},
                {"groq_call_attempted": True, "status": "duplicate_after_synthesis", "synthesis_duplicate": True,
                 "ppa_deltas": {}, "guidance": {"mode_used": "guided"}},
            ],
            "convergence": {"status": "converged"},
        }]
        result = aggregate(rows)
        self.assertEqual(result["accepted_candidates"], 1)
        self.assertEqual(result["ppa_skipped_duplicate_count"], 1)
        self.assertEqual(result["design_success_rate"], 1.0)
        self.assertEqual(result["converged_designs"], 1)


def run_tests():
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        raise AssertionError("pre-freeze RTL optimization fixture tests failed")


if __name__ == "__main__":
    run_tests()
