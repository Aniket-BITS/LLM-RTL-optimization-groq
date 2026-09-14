from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


TIMING_AUTOMATION_DIR = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "script_name",
    [
        "qualify_dataset.py",
        "lint_candidate.py",
        "verify_rtl.py",
        "yosys_structure.py",
    ],
)
def test_yosys_backed_self_test(script_name: str, yosys: str) -> None:
    script = TIMING_AUTOMATION_DIR / script_name
    proc = subprocess.run(
        [sys.executable, str(script), "--self-test", "--yosys", yosys],
        cwd=TIMING_AUTOMATION_DIR.parent,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert proc.returncode == 0, (
        f"{script_name} --self-test failed with rc={proc.returncode}\n"
        f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )


def test_smoke_fixture_static_contract() -> None:
    import hashlib
    import json

    fixture_dir = TIMING_AUTOMATION_DIR / "tests" / "fixtures" / "nebula_smoke"
    manifest = json.loads((fixture_dir / "design_manifest.json").read_text())
    source = fixture_dir / "nebula_smoke.v"
    actual = hashlib.sha256(source.read_bytes()).hexdigest()
    assert manifest["source_hashes"]["nebula_smoke.v"] == actual
    assert manifest["verification"]["mode"] == "combinational_cec"

    for name in ("smoke_candidate.json", "smoke_candidate_bad.json"):
        payload = json.loads((fixture_dir.parent / name).read_text())
        patch = payload["patches"][0]
        assert patch["original_sha256"] == actual
        assert patch["target_file"] == "nebula_smoke.v"
        assert patch["target_module"] == "nebula_smoke"

    # The good rewrite only swaps commutative operands. The bad rewrite
    # truncates to 8 bits and mistakes bit 7 for carry-out. Prove the fixture
    # itself contains both an equivalent and an inequivalent case independent
    # of any EDA tool.
    mismatches = 0
    for a in range(256):
        for b in range(256):
            original = a + b
            good = b + a
            sum_trunc = (a + b) & 0xFF
            bad_value = (((sum_trunc >> 7) & 1) << 8) | sum_trunc
            assert original == good
            mismatches += original != bad_value
    assert mismatches > 0


def test_smoke_fixture_good_and_bad_formal(yosys: str, tmp_path: Path) -> None:
    script = TIMING_AUTOMATION_DIR.parent / "tools" / "smoke_test.sh"
    env = dict(__import__("os").environ)
    env["NEBULA_YOSYS"] = yosys
    env["TMPDIR"] = str(tmp_path)
    proc = subprocess.run(
        ["bash", str(script)],
        cwd=TIMING_AUTOMATION_DIR.parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert proc.returncode == 0, (
        f"smoke_test.sh failed with rc={proc.returncode}\n"
        f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )
    assert "good candidate -> PROVEN_COMPLETE" in proc.stdout
    assert "bad candidate  -> DISPROVEN (exit 3)" in proc.stdout


def test_verify_classification_distinguishes_disproof_from_incomplete_proof() -> None:
    sys.path.insert(0, str(TIMING_AUTOMATION_DIR))
    try:
        from verify_rtl import classify_yosys_nonzero
    finally:
        if sys.path and sys.path[0] == str(TIMING_AUTOMATION_DIR):
            sys.path.pop(0)

    status, _reason, code = classify_yosys_nonzero(
        "ERROR: Called with -verify and proof did fail!",
        mode="combinational_cec",
    )
    assert (status, code) == ("disproven", 3)

    status, _reason, code = classify_yosys_nonzero(
        "ERROR: Found 4 unproven $equiv cells in 'equiv_status -assert'.",
        mode="sequential_equivalence",
    )
    assert (status, code) == ("not_proven", 1)


def test_structure_rejects_multiple_drivers(yosys: str, tmp_path: Path):
    import hashlib
    import json
    from design_manifest import DesignManifest
    from yosys_structure import run_yosys_views, YosysElaborationError
    source = tmp_path / "bad.v"
    source.write_text("module bad(input a, b, c, output y); assign y = a & b; assign y = b | c; endmodule\n")
    manifest_data = json.loads((TIMING_AUTOMATION_DIR / "tests/fixtures/nebula_smoke/design_manifest.json").read_text())
    manifest_data.update(top="bad", design_id="bad", source_files=["bad.v"],
                         source_hashes={"bad.v": hashlib.sha256(source.read_bytes()).hexdigest()})
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest_data))
    with pytest.raises(YosysElaborationError, match="multiple conflicting drivers"):
        run_yosys_views(DesignManifest.read_json(manifest_path), tmp_path, yosys_exe=yosys)
