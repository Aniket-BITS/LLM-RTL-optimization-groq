"""V3 classification, Pareto and proof-outcome regression tests."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import calibrate_pairs_v3 as v3


class V3Tests(unittest.TestCase):
    def pair(self, pair_id, baseline, reference):
        tmp = tempfile.TemporaryDirectory(prefix="nebula-v3-test-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        result = {"id": pair_id}
        for side, text in (("baseline", baseline), ("reference", reference)):
            path = root / f"{side}.v"
            path.write_text(text)
            result[side] = {"source": str(path), "edge_signals": sorted(set(v3.re.findall(r"(?:pos|neg)edge\s+(\w+)", text)))}
        return result

    def test_pair_classes(self):
        self.assertEqual(v3.pair_class(self.pair("c", "module c(input a,output z); assign z=a; endmodule", "module r(input a,output z); assign z=a; endmodule")), "combinational")
        self.assertEqual(v3.pair_class(self.pair("s", "module s(input clk,a,output reg z); always @(posedge clk) z<=a; endmodule", "module r(input clk,a,output reg z); always @(posedge clk) z<=a; endmodule")), "same_cycle_sequential")
        self.assertEqual(v3.pair_class(self.pair("r", "module s(input clk,reset,a,output reg z); always @(posedge clk) if(reset) z<=0; else z<=a; endmodule", "module r(input clk,reset,a,output reg z); always @(posedge clk) if(reset) z<=0; else z<=a; endmodule")), "reset_sensitive_sequential")
        self.assertEqual(v3.pair_class(self.pair("mac", "module mac(input clk); always @(posedge clk); endmodule", "module mac_ref(input clk); always @(posedge clk); endmodule")), "latency_changing")

    def test_timeout_is_capacity_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "proof.log"
            log.write_text("still solving")
            outcome = v3.interpret_proof({"status": "timeout", "log": str(log)})
            self.assertEqual(outcome["category"], "proof_capacity_limited")
            self.assertEqual(outcome["optimization_conclusion"], "unknown")

    def test_counterexample_is_distinct(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "proof.log"
            log.write_text("SAT proof finished - model found: FAIL!")
            outcome = v3.interpret_proof({"status": "unproven", "log": str(log)})
            self.assertEqual(outcome["category"], "counterexample")

    def test_negative_slack_improvement_is_pareto_evidence(self):
        b = {"status": "passed", "recipe_fingerprint_sha256": "same",
             "metrics": {"area_um2": 100., "power_w": 1., "setup_ws_ns": -.2,
                         "hold_ws_ns": .01, "setup_tns_ns": -1., "hold_tns_ns": 0.}}
        r = {"status": "passed", "recipe_fingerprint_sha256": "same",
             "metrics": {"area_um2": 90., "power_w": .9, "setup_ws_ns": -.05,
                         "hold_ws_ns": .01, "setup_tns_ns": -.1, "hold_tns_ns": 0.}}
        result = v3.pareto(b, r, {"status": "passed"})
        self.assertEqual(result["classification"], "dominates")
        self.assertGreater(result["setup_slack_gain_ns"], 0)
        self.assertFalse(result["reference_timing_clean"])


if __name__ == "__main__":
    unittest.main()
