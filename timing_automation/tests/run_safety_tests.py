#!/usr/bin/env python3
"""Run the handwritten deterministic + SEC safety matrix.

Expected behavior:
  cand_bad_ports.v : lint FAIL
  cand_bad_sec.v   : lint PASS, SEC FAIL
  cand_good.v      : lint PASS, SEC PASS

Requires a real Yosys executable.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


HERE = Path(__file__).resolve().parent
SCRIPT_DIR = HERE.parent
FLOW_DIR = SCRIPT_DIR.parent
ORFS_ROOT = FLOW_DIR.parent


def find_yosys(explicit=None):
    candidates = []

    if explicit:
        candidates.append(explicit)

    if os.environ.get("YOSYS_EXE"):
        candidates.append(os.environ["YOSYS_EXE"])

    if shutil.which("yosys"):
        candidates.append(shutil.which("yosys"))

    candidates.append(
        str(ORFS_ROOT / "tools" / "install" / "yosys" / "bin" / "yosys")
    )

    for candidate in candidates:
        if not candidate:
            continue

        p = Path(candidate).expanduser()

        if p.is_file() and os.access(p, os.X_OK):
            return str(p.resolve())

        found = shutil.which(str(candidate))
        if found:
            return found

    return None


def run(cmd):
    return subprocess.run(
        [str(x) for x in cmd],
        capture_output=True,
        text=True,
    )


def main():
    ap = argparse.ArgumentParser()

    ap.add_argument("--golden", required=True)
    ap.add_argument("--design", default="mapper_reconvergent_test")
    ap.add_argument("--top", default=None)
    ap.add_argument("--platform", default="nangate45")
    ap.add_argument("--yosys", default=None)

    args = ap.parse_args()

    golden = Path(args.golden).resolve()

    if not golden.is_file():
        print(
            f"ERROR: golden RTL not found: {golden}",
            file=sys.stderr,
        )
        return 2

    yosys = find_yosys(args.yosys)

    if not yosys:
        print(
            "ERROR: Yosys not found. Set YOSYS_EXE or pass --yosys.",
            file=sys.stderr,
        )
        return 2

    top = args.top or args.design

    lint_py = SCRIPT_DIR / "lint_candidate.py"
    verify_py = SCRIPT_DIR / "verify_rtl.py"

    required = [
        lint_py,
        verify_py,
        HERE / "cand_bad_ports.v",
        HERE / "cand_bad_sec.v",
        HERE / "cand_good.v",
    ]

    missing = [x for x in required if not x.is_file()]

    if missing:
        print("ERROR: required safety-test files are missing:")
        for x in missing:
            print("  -", x)
        return 2

    print("=" * 68)
    print("RTL OPTIMIZER SAFETY TESTS")
    print("=" * 68)
    print(f"Golden   : {golden}")
    print(f"Design   : {args.design}")
    print(f"Platform : {args.platform}")
    print(f"Top      : {top}")
    print(f"Yosys    : {yosys}")

    rows = []
    ok_all = True

    with tempfile.TemporaryDirectory(prefix="rtl_safety_") as td:
        td = Path(td)

        def lint(name):
            out = td / f"{name}.lint.json"

            proc = run([
                sys.executable,
                lint_py,
                "--golden",
                golden,
                "--candidate",
                HERE / f"{name}.v",
                "--top",
                top,
                "--yosys",
                yosys,
                "--output",
                out,
            ])

            return proc, out

        def sec(name):
            out = td / f"{name}.verify.json"

            proc = run([
                sys.executable,
                verify_py,
                "--design",
                args.design,
                "--platform",
                args.platform,
                "--golden",
                golden,
                "--candidate",
                HERE / f"{name}.v",
                "--top",
                top,
                "--yosys",
                yosys,
                "--output",
                out,
            ])

            return proc, out

        # --------------------------------------------------
        # 0. Golden vs golden
        # --------------------------------------------------

        self_out = td / "self.verify.json"

        self_proc = run([
            sys.executable,
            verify_py,
            "--design",
            args.design,
            "--platform",
            args.platform,
            "--golden",
            golden,
            "--candidate",
            golden,
            "--top",
            top,
            "--yosys",
            yosys,
            "--output",
            self_out,
        ])

        self_ok = self_proc.returncode == 0

        rows.append(
            (
                "golden_vs_golden",
                "SEC",
                "PASS",
                "PASS" if self_ok else "FAIL",
            )
        )

        ok_all &= self_ok

        if not self_ok:
            print("\nGolden-vs-golden SEC failed.")
            print(self_proc.stdout)
            print(self_proc.stderr)

        # --------------------------------------------------
        # 1. Bad ports
        # --------------------------------------------------

        p, _ = lint("cand_bad_ports")

        actual = "PASS" if p.returncode == 0 else "FAIL"

        rows.append(
            (
                "cand_bad_ports",
                "lint",
                "FAIL",
                actual,
            )
        )

        ok_all &= actual == "FAIL"

        # --------------------------------------------------
        # 2. Bad functionality must pass lint
        # --------------------------------------------------

        p, _ = lint("cand_bad_sec")

        actual = "PASS" if p.returncode == 0 else "FAIL"

        rows.append(
            (
                "cand_bad_sec",
                "lint",
                "PASS",
                actual,
            )
        )

        ok_all &= actual == "PASS"

        # --------------------------------------------------
        # 3. Bad functionality must fail SEC
        # --------------------------------------------------

        p, _ = sec("cand_bad_sec")

        actual = "PASS" if p.returncode == 0 else "FAIL"

        rows.append(
            (
                "cand_bad_sec",
                "SEC",
                "FAIL",
                actual,
            )
        )

        ok_all &= actual == "FAIL"

        # --------------------------------------------------
        # 4. Good candidate must pass lint
        # --------------------------------------------------

        p, _ = lint("cand_good")

        actual = "PASS" if p.returncode == 0 else "FAIL"

        rows.append(
            (
                "cand_good",
                "lint",
                "PASS",
                actual,
            )
        )

        ok_all &= actual == "PASS"

        # --------------------------------------------------
        # 5. Good candidate must pass SEC
        # --------------------------------------------------

        p, _ = sec("cand_good")

        actual = "PASS" if p.returncode == 0 else "FAIL"

        rows.append(
            (
                "cand_good",
                "SEC",
                "PASS",
                actual,
            )
        )

        ok_all &= actual == "PASS"

    # ------------------------------------------------------
    # Report
    # ------------------------------------------------------

    print()
    print("Safety matrix")
    print("-" * 68)

    print(
        f"{'case':24} "
        f"{'gate':8} "
        f"{'expected':10} "
        f"{'actual':10}"
    )

    print("-" * 68)

    for case, gate, expected, actual in rows:
        print(
            f"{case:24} "
            f"{gate:8} "
            f"{expected:10} "
            f"{actual:10}"
        )

    print("-" * 68)

    print(
        "RESULT:",
        "PASS" if ok_all else "FAIL"
    )

    if not ok_all:
        print(
            "\nOne or more safety gates behaved differently "
            "from the expected matrix."
        )

    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
