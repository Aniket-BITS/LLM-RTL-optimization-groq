# Phase 5–9 EDA regression fix — 2026-09-12

This patch fixes the two Yosys-backed regressions exposed after installing the
Phase 5–9 guided/exploratory redesign.

## Fixed

1. `lint_candidate.py`
   - Removed stale `chosen_pattern_is_eligible` gate references.
   - `chosen_pattern_is_recommended` is diagnostic only, as intended.
   - Model `self_checks` truth claims are diagnostic only; deterministic
     SourceBundle/frozen/Yosys/formal checks remain the hard safety boundary.

2. `response_schema.json`
   - Aligned `self_checks` with `extract_candidate.py` and `lint_candidate.py`.

3. Smoke candidate fixtures
   - Added the complete canonical self-check object required by extraction.

4. `tools/smoke_test.sh`
   - Rebuilt the smoke optimizer request with exact `SourceBundle` identity.
   - Removed the legacy editable-range permission contract.
   - Preserved the known-good `PROVEN_COMPLETE` and deliberately bad
     `DISPROVEN (exit 3)` formal acceptance checks.

## Local validation in the packaging environment

- `python3 -m compileall -q timing_automation` — PASS
- `python3 -m pytest timing_automation/tests -q` — 36 passed, 13 skipped
- `bash -n tools/smoke_test.sh` — PASS

EDA-dependent tests are skipped in the packaging environment because its Yosys
installation is unavailable. Run the commands below in the ORFS checkout where
Yosys/OpenROAD are installed.
