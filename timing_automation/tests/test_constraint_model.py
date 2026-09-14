import copy
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from constraint_model import validate_model, render_sdc, ConstraintError


class ConstraintTests(unittest.TestCase):
    def setUp(self):
        self.clocks = [{'name': 'core', 'port': 'clk', 'period_ns': 1.0}]
        self.ports = {'clk': {'direction': 'input', 'width': 1},
                      'a': {'direction': 'input', 'width': 32},
                      'y': {'direction': 'output', 'width': 32}}
        self.model = {'schema': 'nebula_constraints/1',
                      'input_delays': [{'ports': ['a'], 'clock': 'core', 'min_ns': 0, 'max_ns': .1}],
                      'output_delays': [{'ports': ['y'], 'clock': 'core', 'min_ns': 0, 'max_ns': .1}]}

    def test_complete_and_missing_io(self):
        self.assertTrue(validate_model(self.clocks, self.model, self.ports)['ok'])
        self.model['input_delays'] = []
        self.assertIn('CONSTRAINT_COVERAGE_INCOMPLETE', str(validate_model(self.clocks, self.model, self.ports)))

    def test_invalid_io_unknown_clock_and_nan(self):
        for field, value in [('ports', ['bogus']), ('clock', 'missing'), ('max_ns', float('nan'))]:
            model = copy.deepcopy(self.model)
            model['input_delays'][0][field] = value
            self.assertFalse(validate_model(self.clocks, model, self.ports)['ok'])

    def test_generated_clocks_and_async_groups(self):
        self.clocks.append({'name': 'other', 'port': 'clk2', 'period_ns': 2.0})
        self.model['generated_clocks'] = [{'name': 'div2', 'master_clock': 'core', 'divide_by': 2,
            'source': {'kind': 'ports', 'names': ['clk']}, 'target': {'kind': 'pins', 'names': ['divider/Q']}}]
        self.model['asynchronous_clock_groups'] = [[['core', 'div2'], ['other']]]
        sdc = render_sdc(self.clocks, self.model)
        self.assertIn('create_generated_clock', sdc)
        self.assertIn('set_clock_groups -asynchronous', sdc)
        self.model['generated_clocks'][0]['master_clock'] = 'missing'
        with self.assertRaises(ConstraintError):
            render_sdc(self.clocks, self.model)

    def test_combinational_budget_has_no_invented_clock(self):
        model = {'schema': 'nebula_constraints/1', 'max_delays': [{'ns': 1,
            'from': {'kind': 'ports', 'names': ['a']}, 'to': {'kind': 'ports', 'names': ['y']}}]}
        ports = {key: value for key, value in self.ports.items() if key != 'clk'}
        self.assertTrue(validate_model([], model, ports)['ok'])
        self.assertNotIn('create_clock', render_sdc([], model))

    def test_multicycle_requires_explicit_hold(self):
        self.model['multicycle_paths'] = [{'from': {'kind': 'clocks', 'names': ['core']},
            'to': {'kind': 'clocks', 'names': ['core']}, 'setup_cycles': 2, 'reason': 'test contract'}]
        self.assertFalse(validate_model(self.clocks, self.model)['ok'])
        self.model['multicycle_paths'][0]['hold_cycles'] = 1
        self.assertIn('set_multicycle_path -hold 1', render_sdc(self.clocks, self.model))

    def test_unsupported_fields_fail_closed(self):
        self.model['magic_skip_timing'] = True
        self.assertFalse(validate_model(self.clocks, self.model)['ok'])


if __name__ == '__main__':
    unittest.main()
