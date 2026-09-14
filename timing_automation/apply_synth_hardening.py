#!/usr/bin/env python3

"""
apply_synth_hardening.py

Installs the DR_RTL synthesis hardening fix into ORFS' own
flow/scripts/synth.tcl.

WHY
---
ORFS' default synthesis maps flip-flops with `dfflibmap` straight against the
platform liberty file. On Nangate45 that liberty has async-reset/async-set
cells (DFFR_X1, DFFS_X1, ...) but NO async-*load* cell. Designs that infer an
async-load flop ($_ALDFF_*) -- confirmed here for FIFO (32 cells), SPI, and
UART -- are left with unmapped cells and the flow dies at 1_synth.

This is the exact failure github.com/Nick01105/dr-rtl-oss-backend's syn.ys
already solved for the proprietary-to-open-source port:

    async2sync
    dfflegalize -cell $_DFF_P_ 01 -cell $_DFF_N_ 01 -cell $_DFF_PN0_ 01 \\
                -cell $_DFF_PN1_ 01 -cell $_DFF_PP0_ 01 -cell $_DFF_PP1_ 01
    dfflibmap -liberty LIB
    abc -liberty LIB

`async2sync` folds the async load/reset into clocked logic on D, and
`dfflegalize` normalizes every flop to one of six plain synchronous types
before `dfflibmap` runs -- so nothing is left for `dfflibmap` to fail on.

Verified empirically (Yosys 0.33, Nangate45 liberty, all 20 DR_RTL designs):
  default-style synthesis : 17/20 map cleanly (FIFO, SPI, UART fail)
  hardened synthesis      : 20/20 map cleanly

WHAT THIS SCRIPT DOES
----------------------
Inserts an env-gated block into flow/scripts/synth.tcl, immediately before
the existing "# Technology mapping of flip-flops" section (a stable anchor
that already exists in every current ORFS release and sits exactly where
async2sync/dfflegalize must run: after tech-independent synth, before
dfflibmap/abc). The block only fires when DR_RTL_SYNTH_HARDEN is exported
from config.mk (which run_dr_rtl_one.py now does by default) -- so every
other ORFS design/platform is completely unaffected.

The insertion is idempotent: re-running this script on an already-patched
synth.tcl is a no-op. A timestamped .orig backup is kept on first patch.

USAGE
-----
    python3 apply_synth_hardening.py --orfs-root ~/OpenROAD-flow-scripts
    python3 apply_synth_hardening.py --orfs-root ~/OpenROAD-flow-scripts --check
    python3 apply_synth_hardening.py --orfs-root ~/OpenROAD-flow-scripts --revert
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

MARKER_BEGIN = "# >>> DR_RTL_SYNTH_HARDEN (dr-rtl-oss-backend fix) >>>"
MARKER_END = "# <<< DR_RTL_SYNTH_HARDEN (dr-rtl-oss-backend fix) <<<"

ANCHOR = "# Technology mapping of flip-flops"

HARDEN_BLOCK = f"""{MARKER_BEGIN}
# Employs the fix from github.com/Nick01105/dr-rtl-oss-backend (syn.ys):
# async-load flops ($_ALDFF_*) have no Nangate45 liberty cell, so plain
# dfflibmap leaves them unmapped and the flow dies here. async2sync folds
# the async load/reset into clocked logic on D; dfflegalize then normalizes
# every flop to one of six plain synchronous types before dfflibmap/abc run.
# Gated on DR_RTL_SYNTH_HARDEN (exported by run_dr_rtl_one.py's config.mk) so
# every other design/platform is unaffected.
if {{ [env_var_exists_and_non_empty DR_RTL_SYNTH_HARDEN] }} {{
  async2sync
  dfflegalize \\
    -cell $_DFF_P_ 01 \\
    -cell $_DFF_N_ 01 \\
    -cell $_DFF_PN0_ 01 \\
    -cell $_DFF_PN1_ 01 \\
    -cell $_DFF_PP0_ 01 \\
    -cell $_DFF_PP1_ 01
}}
{MARKER_END}
"""


def find_synth_tcl(orfs_root: Path) -> Path:
    candidates = [
        orfs_root / "flow" / "scripts" / "synth.tcl",
        orfs_root / "scripts" / "synth.tcl",
    ]
    for c in candidates:
        if c.is_file():
            return c
    raise FileNotFoundError(
        "Could not find synth.tcl under "
        f"{orfs_root}\nTried:\n" + "\n".join(f"  {c}" for c in candidates)
    )


def already_patched(text: str) -> bool:
    return MARKER_BEGIN in text


def patch(text: str) -> str:
    if already_patched(text):
        return text

    if text.count(ANCHOR) != 1:
        raise RuntimeError(
            f"Expected exactly one occurrence of anchor comment "
            f"{ANCHOR!r} in synth.tcl, found {text.count(ANCHOR)}. "
            "ORFS' synth.tcl may have changed structure upstream; "
            "insert the hardening block manually (see HARDEN_BLOCK "
            "in this script) rather than risk a wrong patch location."
        )

    idx = text.index(ANCHOR)
    return text[:idx] + HARDEN_BLOCK + "\n" + text[idx:]


def revert(text: str) -> str:
    if not already_patched(text):
        return text
    start = text.index(MARKER_BEGIN)
    end = text.index(MARKER_END) + len(MARKER_END)
    # patch() inserts the block as "<blank>BLOCK<blank>anchor"; eat the extra
    # blank line we added so revert restores the file byte-for-byte.
    after = text[end:]
    if after.startswith("\n\n"):
        end += 2
    elif after.startswith("\n"):
        end += 1
    return text[:start] + text[end:]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--orfs-root",
        required=True,
        type=Path,
        help="Path to OpenROAD-flow-scripts checkout (contains flow/).",
    )
    ap.add_argument(
        "--check",
        action="store_true",
        help="Only report whether synth.tcl is currently patched; exit 0/1.",
    )
    ap.add_argument(
        "--revert",
        action="store_true",
        help="Remove the hardening block, restoring stock ORFS behavior.",
    )
    args = ap.parse_args()

    try:
        synth_tcl = find_synth_tcl(args.orfs_root.expanduser().resolve())
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    text = synth_tcl.read_text(encoding="utf-8")

    if args.check:
        patched = already_patched(text)
        print(f"{synth_tcl}: {'PATCHED' if patched else 'NOT PATCHED'}")
        return 0 if patched else 1

    if args.revert:
        if not already_patched(text):
            print(f"{synth_tcl}: already unpatched, nothing to do.")
            return 0
        new_text = revert(text)
        synth_tcl.write_text(new_text, encoding="utf-8")
        print(f"Reverted: {synth_tcl}")
        return 0

    if already_patched(text):
        print(f"{synth_tcl}: already patched, nothing to do.")
        return 0

    backup = synth_tcl.with_suffix(
        synth_tcl.suffix + f".orig.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    )
    shutil.copy2(synth_tcl, backup)

    try:
        new_text = patch(text)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    synth_tcl.write_text(new_text, encoding="utf-8")

    print(f"Backed up  : {backup}")
    print(f"Patched    : {synth_tcl}")
    print()
    print("Hardening is env-gated on DR_RTL_SYNTH_HARDEN=1 (config.mk exports")
    print("this by default via run_dr_rtl_one.py). Every other design/platform")
    print("that does not set it is byte-for-byte unaffected.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
