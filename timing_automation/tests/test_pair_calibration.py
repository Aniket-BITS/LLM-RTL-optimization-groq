"""Fail-closed selection and actual proof-engine contract regressions."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from calibrate_pairs import assess, elaborate, verify, YOSYS


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.base = {'status': 'passed', 'recipe_fingerprint_sha256': 'same',
                     'metrics': {'area_um2': 100., 'power_w': .001, 'setup_ws_ns': .1,
                                 'hold_ws_ns': .02, 'setup_tns_ns': 0., 'hold_tns_ns': 0.}}
        self.ref = copy.deepcopy(self.base)
        self.ref['metrics']['area_um2'] = 90.
        self.proof = {'status': 'passed'}

    def test_improvement_needs_proof_and_matching_physics(self):
        self.assertTrue(assess(self.base, self.ref, self.proof)['keep'])
        self.assertFalse(assess(self.base, self.ref, {'status': 'timeout'})['keep'])
        self.ref['recipe_fingerprint_sha256'] = 'different'
        self.assertFalse(assess(self.base, self.ref, self.proof)['keep'])

    def test_area_win_cannot_hide_power_or_timing_regression(self):
        self.ref['metrics']['power_w'] = .0011
        self.assertFalse(assess(self.base, self.ref, self.proof)['keep'])
        self.ref['metrics']['power_w'] = .001
        self.ref['metrics']['setup_ws_ns'] = -.001
        self.assertFalse(assess(self.base, self.ref, self.proof)['keep'])

    def test_rounding_noise_is_not_headroom(self):
        self.ref['metrics']['area_um2'] = 99.9
        self.assertFalse(assess(self.base, self.ref, self.proof)['keep'])


@unittest.skipUnless(YOSYS.exists(), 'Yosys unavailable')
class ProofTests(unittest.TestCase):
    def check_pair(self, golden, candidate, mode, expected):
        with tempfile.TemporaryDirectory(prefix='nebula-proof-') as tmp:
            directory = Path(tmp)
            pair = {'verification_method': {'mode': mode}}
            for side, text in [('baseline', golden), ('reference', candidate)]:
                source = directory / f'{side}.v'
                source.write_text(text)
                pair[side] = {'source': str(source), 'top': 'top'}
            self.assertIsNotNone(elaborate(pair, directory))
            result = verify(pair, directory)
            self.assertEqual(result['status'] == 'passed', expected,
                             Path(result['log']).read_text()[-2000:])

    def test_changed_internal_names_do_not_define_equivalence(self):
        self.check_pair('module top(input clk,a, output reg q); wire x=a; always @(posedge clk) q<=x; endmodule',
                        'module top(input clk,a, output reg q); wire x=~a; always @(posedge clk) q<=~x; endmodule',
                        'sequential_equivalence', True)

    def test_latency_change_is_rejected(self):
        self.check_pair('module top(input clk,a, output reg q); always @(posedge clk) q<=a; endmodule',
                        'module top(input clk,a, output reg q); reg d; always @(posedge clk) begin d<=a; q<=d; end endmodule',
                        'sequential_equivalence', False)

    def test_combinational_counterexample_is_rejected(self):
        self.check_pair('module top(input a,b, output q); assign q=a&b; endmodule',
                        'module top(input a,b, output q); assign q=a|b; endmodule',
                        'combinational_cec', False)


if __name__ == '__main__':
    unittest.main()
