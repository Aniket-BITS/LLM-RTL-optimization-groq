from pathlib import Path

import pytest

from sta_netlist_compat import StaNetlistCompatError, preserve_raw_and_sanitize, sanitize_text, orfs_yosys_netlist_target, orfs_make_target


def test_signed_structural_declarations_are_safely_removed():
    text = """module m(a,b,y);\ninput signed [7:0] a;\ninput signed [7:0] b;\noutput signed [7:0] y;\nwire signed [7:0] y;\nBUF_X1 u0(.A(a[0]),.Z(y[0]));\nendmodule\n"""
    out, info = sanitize_text(text)
    assert " signed " not in out
    assert info["signed_declaration_qualifiers_removed"] == 4
    assert info["safe_structural_netlist"] is True


def test_unsigned_structural_netlist_is_unchanged():
    text = "module m(input [1:0] a, output [1:0] y);\nassign y = a;\nendmodule\n"
    out, info = sanitize_text(text)
    assert out == text
    assert info["transformations"] == []


def test_signed_behavioral_arithmetic_fails_closed():
    text = """module m(input signed [7:0] a,b, output signed [7:0] y);\nassign y = a + b;\nendmodule\n"""
    with pytest.raises(StaNetlistCompatError):
        sanitize_text(text)


def test_raw_and_sta_artifacts_are_preserved(tmp_path: Path):
    net = tmp_path / "1_2_yosys.v"
    net.write_text("module m(a,y); input signed [0:0] a; output [0:0] y; BUF_X1 u(.A(a),.Z(y)); endmodule\n")
    meta = tmp_path / "compat.json"
    result = preserve_raw_and_sanitize(net, metadata_path=meta)
    raw = Path(result["raw_path"])
    assert raw.is_file()
    assert "signed" in raw.read_text()
    assert "signed" not in net.read_text()
    assert result["input_sha256"] != result["output_sha256"]
    assert meta.is_file()


def test_preserve_raw_is_idempotent_across_synth_then_grt(tmp_path: Path):
    net = tmp_path / "1_2_yosys.v"
    raw = tmp_path / "1_2_yosys.raw.v"
    net.write_text("module m(a,y); input signed [0:0] a; output [0:0] y; BUF_X1 u(.A(a),.Z(y)); endmodule\n")
    first = preserve_raw_and_sanitize(net, raw_path=raw)
    raw_before = raw.read_text()
    assert first["raw_preserved_now"] is True
    assert "signed" in raw_before
    assert "signed" not in net.read_text()

    # A later physical stage re-enters the same FLOW_VARIANT.  The raw artifact
    # must remain the original Yosys output rather than being overwritten by the
    # already-sanitized working copy.
    second = preserve_raw_and_sanitize(net, raw_path=raw)
    assert second["raw_reused"] is True
    assert raw.read_text() == raw_before
    assert "signed" in raw.read_text()
    assert "signed" not in net.read_text()


def test_orfs_yosys_target_is_exact_file_target(tmp_path: Path):
    target = orfs_yosys_netlist_target(tmp_path, "nangate45", "rtlopt_mul", "variant_001")
    assert target == (tmp_path / "results/nangate45/rtlopt_mul/variant_001/1_2_yosys.v").resolve()


def test_new_yosys_output_refreshes_raw_authority(tmp_path: Path):
    net = tmp_path / "1_2_yosys.v"
    raw = tmp_path / "1_2_yosys.raw.v"
    meta = tmp_path / "1_2_yosys_sta_compat.json"
    first_raw = "module m(a,y); input signed [0:0] a; output [0:0] y; BUF_X1 u(.A(a),.Z(y)); endmodule\n"
    net.write_text(first_raw)
    preserve_raw_and_sanitize(net, raw_path=raw, metadata_path=meta)

    # Simulate a genuine Yosys rebuild of the same make target.
    second_raw = "module m(a,y); input signed [1:0] a; output [1:0] y; BUF_X1 u0(.A(a[0]),.Z(y[0])); BUF_X1 u1(.A(a[1]),.Z(y[1])); endmodule\n"
    net.write_text(second_raw)
    result = preserve_raw_and_sanitize(net, raw_path=raw, metadata_path=meta)
    assert result["raw_preserved_now"] is True
    assert result["raw_reused"] is False
    assert raw.read_text() == second_raw
    assert "signed [1:0]" not in net.read_text()


def test_orfs_make_target_is_relative_to_flow_dir(tmp_path: Path):
    artifact = orfs_yosys_netlist_target(tmp_path, "nangate45", "rtlopt_mul", "variant_001")
    target = orfs_make_target(tmp_path, artifact)
    assert target == "results/nangate45/rtlopt_mul/variant_001/1_2_yosys.v"
    assert not target.startswith("/")


def test_orfs_make_target_rejects_artifact_outside_flow_dir(tmp_path: Path):
    outside = tmp_path.parent / "outside" / "1_2_yosys.v"
    with pytest.raises(StaNetlistCompatError):
        orfs_make_target(tmp_path, outside)
