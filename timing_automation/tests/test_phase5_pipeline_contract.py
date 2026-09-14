from pathlib import Path

from timing_automation import run_analysis


def test_diagnostic_stages_precede_classification():
    order = run_analysis.STAGE_ORDER
    expected = ["mapping", "select_sat", "witness", "case_physical", "classification"]
    indices = [order.index(name) for name in expected]
    assert indices == sorted(indices)
    assert all(b == a + 1 for a, b in zip(indices, indices[1:]))


def test_generic_report_tcl_contains_no_legacy_design_names():
    root = Path(run_analysis.__file__).resolve().parent
    text = (root / "report_checks.tcl").read_text(encoding="utf-8")
    for token in ("a_reg", "b_reg", "c_reg", "sel_reg", "a27", "b27", "c27"):
        assert token not in text
    assert "all_clocks" in text
    assert "all_registers" in text


def test_witness_reports_are_not_discovered_as_critical_groups(tmp_path):
    (tmp_path / "clk_critical_paths.rpt").write_text("critical\n", encoding="utf-8")
    (tmp_path / "clk_reg2reg_critical_paths.rpt").write_text("reg2reg\n", encoding="utf-8")
    (tmp_path / "witness_w000.rpt").write_text("witness\n", encoding="utf-8")
    found = run_analysis.discover_timing_reports(tmp_path)
    assert [p.name for p in found] == ["clk_critical_paths.rpt"]
