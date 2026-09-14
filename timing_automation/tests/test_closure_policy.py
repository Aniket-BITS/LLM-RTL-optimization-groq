from timing_automation.closure_policy import ClosurePolicy, evaluate_stop


def test_target_boundary():
    p = ClosurePolicy(target_wns_ns=0.0, max_flows=4)
    assert evaluate_stop({"wns_ns": -0.1}, [{"wns_ns": 0.0}], p, flows_used=2).reason == "target_met"


def test_gain_boundary_is_not_diminishing():
    p = ClosurePolicy(min_gain_ns=0.005, noise_floor_wns_ns=0.002, max_flows=4)
    assert evaluate_stop({"wns_ns": -0.1}, [{"wns_ns": -0.095}], p, flows_used=2).reason == "continue"


def test_noise_floor_can_dominate_min_gain():
    p = ClosurePolicy(min_gain_ns=0.001, noise_floor_wns_ns=0.004, max_flows=4)
    d = evaluate_stop({"wns_ns": -0.1}, [{"wns_ns": -0.097}], p, flows_used=2)
    assert d.reason == "diminishing_returns"
    assert d.effective_min_gain_ns == 0.004


def test_flow_budget_counts_flows_not_attempts():
    p = ClosurePolicy(max_flows=3)
    assert evaluate_stop({"wns_ns": -0.1}, [], p, flows_used=3).reason == "flow_budget_exhausted"
    assert evaluate_stop({"wns_ns": -0.1}, [], p, flows_used=2).reason == "continue"


def test_diagnostic_exhaustion_is_not_a_stop_reason():
    p = ClosurePolicy(max_flows=5)
    assert evaluate_stop({"wns_ns": -0.1}, [], p, flows_used=1).reason == "continue"
    assert evaluate_stop(
        {"wns_ns": -0.1}, [], p, flows_used=1, has_requestable_problem=False
    ).reason == "no_requestable_problem"
