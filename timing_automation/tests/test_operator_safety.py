"""Regression tests for independent mutation/driver gates (no LLM required)."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from operator_scope import derive_scopes, verified_scopes
from diagnose_equivalence import analyze_lost_drivers
from design_manifest import DesignManifest
from yosys_structure import compare_manifests


class OperatorSafetyTests(unittest.TestCase):
    def setUp(self):
        self.target = {'file': 'top.v', 'module': 'top', 'original_sha256': 'a' * 64}
        self.locations = [dict(file='top.v', module='top', sha256='a' * 64,
                               line_start=n, line_end=n, operator_roles=[role])
                          for n, role in [(4, '$add'), (5, '$xor'), (7, '$mux')]]
        self.unit = {'unit_id': 'unit_000', 'source': {'source_locations': self.locations}}
        self.ids = ['arith.csla_split']

    def test_adjacent_context_is_read_only(self):
        self.assertEqual(derive_scopes(self.unit, self.target, self.ids), {self.ids[0]: [[4, 4]]})

    def test_shared_line_and_missing_attribution_fail_closed(self):
        self.locations[1].update(line_start=4, line_end=4)
        self.assertEqual(derive_scopes(self.unit, self.target, self.ids)[self.ids[0]], [])
        self.locations[0]['operator_roles'] = []
        self.assertEqual(derive_scopes(self.unit, self.target, self.ids)[self.ids[0]], [])

    def test_stale_source_is_not_writable(self):
        self.locations[0]['sha256'] = 'b' * 64
        self.assertEqual(derive_scopes(self.unit, self.target, self.ids)[self.ids[0]], [])

    def test_forged_permission_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'evidence.json'
            data = json.dumps({'optimization_units': [self.unit]}).encode()
            path.write_bytes(data)
            request = {'unit_id': 'unit_000', 'target': self.target,
                       'eligible_pattern_ids': self.ids,
                       'recommended_pattern_focus_ranges': {self.ids[0]: [[4, 5]]},
                       'operator_scope_evidence': {'path': str(path), 'sha256': hashlib.sha256(data).hexdigest()}}
            with self.assertRaises(ValueError):
                verified_scopes(request)
            request['recommended_pattern_focus_ranges'] = {self.ids[0]: [[4, 4]]}
            self.assertEqual(verified_scopes(request), request['recommended_pattern_focus_ranges'])
            path.write_text('{}')
            with self.assertRaises(ValueError):
                verified_scopes(request)

    def test_same_line_driver_diagnostic(self):
        self.assertEqual(analyze_lost_drivers('assign x=a;\nassign y=b;', 'assign x=a; assign y=b;'), [])
        self.assertTrue(analyze_lost_drivers('assign y=b;', '// assign y=b;'))

    def test_actual_yosys_driver_loss_and_dead_wire_removal(self):
        yosys = Path(__file__).resolve().parents[3] / 'tools/install/yosys/bin/yosys'
        if not yosys.is_file():
            self.skipTest('installed Yosys unavailable')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            golden = root / 'golden'; candidate = root / 'candidate'
            golden.mkdir(); candidate.mkdir()
            before = 'module top(input [3:0] a,b, output [3:0] y);\nwire [3:0] x;\nwire dead;\nassign x=a^b;\nassign y=x;\nendmodule\n'
            (golden / 'top.v').write_text(before)
            (candidate / 'top.v').write_text(before.replace('assign x=a^b;', ''))
            def manifest(directory):
                m = DesignManifest(dataset='test', design_id='driver', top='top', language='verilog', source_files=['top.v'])
                m.refresh_source_hashes(directory)
                return m
            comparison, _, _ = compare_manifests(golden_manifest=manifest(golden), golden_base_dir=golden,
                candidate_manifest=manifest(candidate), candidate_base_dir=candidate, yosys_exe=str(yosys))
            self.assertTrue(any(d.category == 'preexisting_live_net_undriven' for d in comparison.differences))
            (candidate / 'top.v').write_text(before.replace('wire dead;', ''))
            comparison, _, _ = compare_manifests(golden_manifest=manifest(golden), golden_base_dir=golden,
                candidate_manifest=manifest(candidate), candidate_base_dir=candidate, yosys_exe=str(yosys))
            self.assertTrue(comparison.ok, comparison)


if __name__ == '__main__':
    unittest.main()
