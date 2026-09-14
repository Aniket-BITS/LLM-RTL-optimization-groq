from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from physical_recipe import frozen_physical_variables, PhysicalRecipeValidationError
from constraint_model import verify_frozen_constraints, render_sdc, ConstraintError


class FrozenPhysicalTests(unittest.TestCase):
    def test_fixed_geometry_and_clear_adaptation(self):
        variables = frozen_physical_variables(SimpleNamespace(flow_variables={'DIE_AREA':'0 0 100 100', 'CORE_AREA':'2 2 98 98'}))
        self.assertEqual(variables['CORE_UTILIZATION'], '')
        self.assertEqual(variables['FLOORPLAN_DEF'], '')
        self.assertEqual(variables['FOOTPRINT'], '')

    def test_reject_adaptive_missing_or_outside_geometry(self):
        for variables in [{'CORE_UTILIZATION':40},
                          {'DIE_AREA':'0 0 100 100','CORE_AREA':'2 2 98 98','CORE_UTILIZATION':40},
                          {'DIE_AREA':'0 0 100 100','CORE_AREA':'2 2 101 98'},
                          {'DIE_AREA':'0 0 nan 100','CORE_AREA':'2 2 98 98'}]:
            with self.assertRaises(PhysicalRecipeValidationError):
                frozen_physical_variables(SimpleNamespace(flow_variables=variables))

    def test_reject_physical_override_of_candidate_identity(self):
        with self.assertRaises(PhysicalRecipeValidationError):
            frozen_physical_variables(SimpleNamespace(flow_variables={'DIE_AREA':'0 0 100 100','CORE_AREA':'2 2 98 98','VERILOG_FILES':'golden.v'}))

    def test_sdc_must_match_qualified_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = SimpleNamespace(clocks=[],constraints={'schema':'nebula_constraints/1','max_delays':[{'ns':1,'from':{'kind':'ports','names':['a']},'to':{'kind':'ports','names':['y']}}]})
            path=Path(tmp)/'timing.sdc'
            path.write_text(render_sdc(manifest.clocks,manifest.constraints))
            recipe=SimpleNamespace(source_base_dir=tmp,files=[SimpleNamespace(role='constraints',logical_path='timing.sdc')])
            verify_frozen_constraints(manifest,recipe)
            path.write_text('create_clock -period 99 [get_ports clk]\n')
            with self.assertRaises(ConstraintError):
                verify_frozen_constraints(manifest,recipe)


if __name__ == '__main__':
    unittest.main()
