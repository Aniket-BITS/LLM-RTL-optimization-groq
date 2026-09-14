# DR_RTL 20-design sweep — experiment copy

This is a working copy of `flow/timing_automation/`, made so the fix below
could be developed and tested without touching your live
`generative-rtl-timing-closure` tree. Your original scripts are untouched.

## What was wrong

Several DR_RTL designs stop at synthesis (`1_synth`) under ORFS's default
Yosys flow. Root cause, confirmed empirically (Yosys 0.33 + the real
Nangate45 liberty, all 20 DR_RTL designs, see `evidence/`):

- **FIFO, SPI, UART** each infer an **asynchronous-load flip-flop**
  (`$_ALDFF_*` — FIFO alone has 32 of them).
- Nangate45's liberty has async-**reset**/**set** cells (`DFFR_X1`,
  `DFFS_X1`, ...) but **no async-load cell**.
- ORFS's default synthesis maps flops with a plain `dfflibmap` against that
  liberty, so the `$_ALDFF_*` cells are left unmapped and the flow dies.
- The other 17 designs already synthesize fine — this is not a general
  synthesis problem, it's specifically the async-load pattern.

This is exactly the failure your `dr-rtl-oss-backend` project already
solved when porting off Synopsys DC: `syn.ys` runs `async2sync` (folds the
async load/reset into clocked logic on D) followed by `dfflegalize` to a
fixed set of six plain synchronous flop types, before `dfflibmap`/`abc` run.
Under that sequence, all 20/20 designs map cleanly (`evidence/synthesis_fix_matrix.txt`).

## What was changed

1. **`apply_synth_hardening.py`** (new) — installs an env-gated block into
   ORFS's own `flow/scripts/synth.tcl`, immediately before its existing
   `# Technology mapping of flip-flops` section (the exact point your OSS
   `syn.ys` runs `async2sync`/`dfflegalize`, i.e. after tech-independent
   `synth`, before `dfflibmap`/`abc`). The block only fires when
   `DR_RTL_SYNTH_HARDEN` is set, so every other design/platform is
   byte-for-byte unaffected. Idempotent; keeps a timestamped backup;
   supports `--check` and `--revert`.

2. **`run_dr_rtl_one.py`** (edited) —
   - `config.mk` now exports `DR_RTL_SYNTH_HARDEN=1` and a raised
     `SYNTH_MEMORY_MAX_BITS` (default 4096, was ORFS's much lower default)
     so inferred memories/register files map to flops instead of being
     shrunk to one row — the OSS "memory pass" behavior.
   - The Yosys preflight no longer **rejects** async-reset/async-load
     designs when hardening is on (previously this SKIPped FIFO/SPI/UART
     before ORFS even ran, which was itself a second reason those three
     never reached synthesis).
   - New flags: `--no-harden` (restore old behavior) and
     `--synth-memory-max-bits N`.
   - Hardening choice is preserved across the adaptive-floorplan retry loop.

3. **`run_dr_rtl_sweep.py`** (new) — batch-runs all 20 (or a chosen subset)
   through `run_dr_rtl_one.py`, one subprocess per design so a single
   failure never aborts the sweep. Writes a JSON summary incrementally and
   prints a PASS/SKIP/FAIL table. Supports `--resume-from` to only retry
   designs that didn't pass last time.

## How to run it

These commands run inside your real ORFS checkout on the WSL host (the
`drrtl` container / wherever `external/DR_RTL` is cloned) — they need the
actual ORFS + Yosys + OpenSTA toolchain, which isn't available in this
sandbox.

```bash
# 0) One-time: install the hardening into ORFS's own synth.tcl
cd ~/OpenROAD-flow-scripts
python3 flow/timing_automation/apply_synth_hardening.py --orfs-root .

# Sanity check it's installed
python3 flow/timing_automation/apply_synth_hardening.py --orfs-root . --check

# 1) Full 20-design sweep, hardening on (default)
cd flow
python3 timing_automation/run_dr_rtl_sweep.py

# 2) Just the three the fix targets, with live output
python3 timing_automation/run_dr_rtl_sweep.py --designs FIFO SPI UART -v

# 3) Before/after comparison (baseline = old behavior, no code changes needed
#    on the ORFS side since the block is env-gated and off by default)
python3 timing_automation/run_dr_rtl_sweep.py --no-harden --out sweep_baseline.json
python3 timing_automation/run_dr_rtl_sweep.py             --out sweep_hardened.json

# 4) If a sweep gets interrupted, retry only what didn't pass
python3 timing_automation/run_dr_rtl_sweep.py --resume-from sweep_results.json
```

To go back to stock ORFS behavior entirely:

```bash
python3 flow/timing_automation/apply_synth_hardening.py --orfs-root . --revert
```

## What to expect

- **Before** (`--no-harden` or unpatched `synth.tcl`): FIFO, SPI, UART fail
  at the FLOW stage inside `run_analysis.py`; the DR_RTL manifest for each
  records `baseline_status: "skip"` (or `"fail"` if the preflight change
  isn't picked up) with the async-load reason.
- **After**: all 20 designs should reach `RESULT: PASS` and produce
  canonical WNS/TNS/area/power metrics, unblocking `build_optimizer_input.py`
  for the full set.

## Caveats / what I could not verify here

- I validated the **synthesis mapping** step in isolation (Yosys 0.33
  against the real Nangate45 liberty and all 20 real DR_RTL RTL files —
  see `evidence/`), which is the specific stage that was failing.
- I could **not** run the full ORFS place-and-route flow, `run_analysis.py`'s
  downstream stages, or the sweep itself here — there's no OpenROAD/ORFS
  toolchain or your `external/DR_RTL` checkout in this sandbox. Please run
  the commands above in your real environment and let me know what
  `sweep_results.json` shows; if any design still stops downstream of
  synthesis (placement, CTS, routing), that's a different failure mode and
  I'll dig into that log specifically.
- `SYNTH_MEMORY_MAX_BITS=4096` is a reasonable default but not something I
  could tune against your actual designs' memory sizes — if any design has
  a large inferred RAM, watch its runtime/area and adjust with
  `--synth-memory-max-bits`.
