# Latency-aware extension validation

This package extends the previously validated immutable-Golden/promoted-Current-Best optimizer with phase-1 fixed-cycle latency verification, advisory model latency intent, Golden-relative latency budgets, meaningful-iteration accounting, and formal-result classification.

Validation performed in this packaging environment:

- `py_compile` on the modified/new Python modules: PASS.
- Draft 2020-12 parsing/validation of the dedicated latency response schema: PASS.
- Focused pure-Python regression set covering latency contract/policy, prediction-as-hint behavior, over-budget equivalence, clock-edge handling, Current-Best iteration behavior, and guided optimizer fixtures: 27 passed.
- No live Groq request, Yosys temporal proof, synthesis, or OpenROAD PPA was executed in this container because the project EDA installation/API configuration is not present here. The generated temporal proof flow must therefore receive one local end-to-end Yosys/OpenROAD validation before this latency feature is considered empirically frozen.

The default `--max-latency-regression-cycles 0` deliberately preserves the previously exercised cycle-exact path.

---

# Pre-freeze Golden / Current-Best patch validation

The historical pre-latency package established the optimizer-core architecture: immutable Golden, promoted-only Current Best, model-selected editing base, exact SourceBundle duplicate rejection, backend-authoritative synthesis dedupe/convergence, advisory `no_further_optimization`, formal-to-Golden, and PPA-to-Current-Best. The current package extends that frozen core with the latency-aware phase described above.

For **this pre-freeze patch**, validation performed by the assistant was static only:

- Modified Python files passed `py_compile`.
- `response_schema.json` and `response_schema_prefreeze.json` passed JSON parsing and Draft 2020-12 schema validation.
- The active runner was statically audited for Golden formal reference, Current-Best PPA reference, promoted-only Current-Best updates, the `1_2_yosys.v` synthesis artifact, and the positive setup/hold regression-budget names.
- **No project test suite, live Groq call, Yosys lint/formal run, synthesis, OpenROAD PPA, or RTL benchmark run was executed for this patch.**

`PYTEST_RESULTS.txt`, `SELF_TEST_RESULTS.txt`, and `RUNNER_SELF_TEST_RESULTS.txt` below/alongside this package are historical results from the earlier r3 export and must not be interpreted as validation of the pre-freeze changes.

---

# r3 validation and implementation review

Created as a separate `phase5_to_9_groq_r3` directory. No r2 program, original RTL, or existing experiment output was edited. Runtime dependencies and private configuration are shared through symlinks; their contents are not part of the review patch.

## Results

- `./nebula.sh self-test`: passed the existing source/response contracts and 22 new pure-Python fixture tests.
- Targeted pytest: 23 passed (the 22 new fixtures plus the existing mocked Groq-provider regression).
- Changed Python files: AST syntax checks passed. Launcher/environment shell syntax checks passed.
- The runner's temporary-file `--self-test` also passed with mocked lint and mocked timing/mapping/classification/PPA paths.
- No live Groq, external formal, synthesis, or PPA execution was used for validation. No measured optimization benefit is claimed.

See `SELF_TEST_RESULTS.txt`, `PYTEST_RESULTS.txt`, and `RUNNER_SELF_TEST_RESULTS.txt` for captured output. `GUIDED_CHANGES.patch` contains the program/documentation changes relative to r2. `VERSION.json` records source/new program hashes.

## Validation-driven adjustments to the plan

1. Added `guided_evidence_only` to the real lint request validator. Without this compatibility change, valid evidence-only requests would fail before evaluation.
2. Split the existing physical helper into a selectable synthesis stage and its normal global-route stage, with a directory-derived namespace. This enables pre-PPA netlist comparison and prevents paired arms/repetitions from overwriting each other's physical artifacts.
3. Reused parser, mapper, and classifier public functions unchanged through `guidance_packet.py`. Reused the existing generic operator signature and added generic family-ID normalization in `build_optimizer_input.py`. No design-name heuristics or architecture constraints were introduced.
4. Added an explicit graph-builder timeout. Mapping/classification/serialization failures become persisted exploratory fallback. A classifier's generic/unclassified/empty structural result is not promoted into a recommendation.
5. Kept the existing measurable-improvement threshold and repeat-confirmation requirement while adding user-selected area, power, setup, and hold budgets. The shared metric-delta implementation is reused; policy boundaries use unrounded measurements.
6. Baseline selection compares the golden source with itself for elaboration. Paired arms share that golden-selected period per design/repetition and alternate execution order. They have independent proposal histories and uniquely named physical runs.
7. Normalized synthesis hashing is intentionally conservative: source metadata is removed, while exact named connectivity, initialization, and parameters remain. Renamed but equivalent synthesized implementations can escape no-op detection and still receive PPA. Missing or malformed hashes never cause a skip.
8. Prompt caps apply to advisory JSON, not the mandatory full-source/contract sections. If the full protected prompt exceeds a provider limit, the attempted call is recorded as a provider failure. The exact submitted strings and compaction decisions are retained.

## Covered failure and policy cases

Fixtures exercise all three effective prompt states, malformed timing, mapper/classifier crashes, untrusted source ranges, source-preserving prompt trimming, generic feedback without invented families, infrastructure exclusions from rejected-family history, hash connectivity/initialization sensitivity, missing-hash non-equality, synthesis-only no-op skips, immutable paired directories, unique physical namespaces, shared paired baselines, candidate-guidance refresh, no refresh after incomparable PPA, lint feedback, repeat-budget rejection, and missing-data experiment aggregation.

The new test module uses opaque in-memory source strings and JSON/report/text fixtures. It does not read or write design RTL or launch EDA/API tools. The separate runner self-test retains the requested temporary source-bundle exercise, with lint mocked.

## Remaining empirical validation

A live controlled experiment is still needed to establish guidance coverage, synthesis-hash hit rate, realized PPA benefit, and cost per accepted candidate on this corpus. The offline reporter explicitly exposes missing comparisons and uses null for absent observations. It makes no statistical-significance or causal-benefit claim.

## Editing-base identity echo fix

Runtime validation showed that after Current-Best promotion the model could confuse bundle SHA-256, design identity, and per-file SHA-256 fields. The model response schema now requires only `editing_base`; backend state resolves the authoritative snapshot identity and per-file hashes. This fix was statically validated only; rerun the Current-Best validation before freeze.

## Prefreeze truncation guard

Static validation only. The runner now treats provider `finish_reason=length` as `model_output_truncated` before candidate parsing and feeds back a concise-candidate instruction. The prompt discourages exhaustive lookup/case-table source blowups. No project tests, Groq calls, Yosys, formal, synthesis, OpenROAD, or PPA were run by the assistant for this patch.

## Pre-freeze source-file-set classification fix

Validation performed in the packaging environment:

- `python -m py_compile` on modified Python files: PASS
- Draft 2020-12 response schema check: PASS
- `timing_automation/tests/test_current_best_iteration.py`: 4 passed
- source-file-set rejection feedback classified as non-infrastructure: PASS

The full Groq/Yosys/OpenROAD FSM regression was NOT run in the packaging environment because the shared ORFS `external`/`platforms` tree, a Yosys binary, and `GROQ_API_KEY` are unavailable there. Run it in the restored local ORFS installation before final freeze.

## Guided source-provenance regression fix

The final FSM pre-freeze regression showed that RTL cones were structurally mapped, but guidance fell back to exploratory because the authoritative Current-Best snapshot path differed from the source path recorded in the Yosys physical-run checkpoint. The guidance adapter now resolves the actual `VERILOG_FILES` path from the physical-run command and accepts it for mapping only when its SHA-256 exactly matches both the run metadata and authoritative Current-Best source. Focused pure-Python tests pass. A full local FSM EDA/model rerun is still required to validate guided mode end-to-end.

## V1.4 robustness/scalability validation

V1.4 intentionally preserves V1.3 optimizer/formal semantics and hardens the
interfaces around them. Model self-checks are advisory for schema 3.3; raw Yosys
synthesis is preserved and remains the normalized-hash authority; a safe
post-synthesis STA compatibility copy removes signed declaration qualifiers only
when the netlist is structurally safe; design capabilities are recorded before
attempt 1; formal/PPA stage timeouts are configurable; batch runs are
fault-isolated and design-granular resumable; aggregate JSON/CSV output is
available.

Assistant-environment validation: Python compile PASS, 69 pytest PASS / 14
SKIPPED, runner self-test 11/11 PASS, top-level self-test PASS, and a mocked
synthesis -> signed-netlist sanitize -> global-route handoff PASS. No live Groq,
Yosys project formal, or OpenROAD PPA run is claimed here. Runtime confirmation
must verify that `mul` and `saturating_add` pass the previous OpenSTA STA-0171
signed-declaration failure and that `adder` remains unchanged.

## V1.4.1 pre-OpenSTA Yosys interception

V1.4 revealed that ORFS `make synth` is not a Yosys-only checkpoint: it proceeds
into OpenROAD `synth_odb`, allowing OpenSTA to parse the raw Yosys Verilog before
nEBULA could sanitize safe structural `signed` declarations.  V1.4.1 replaces
that interception with the exact `$(RESULTS_DIR)/1_2_yosys.v` target, preserves
that raw file, sanitizes only the working STA copy, and then invokes the original
requested ORFS stage.

Regression coverage asserts that:

1. calibration and candidate flow use the exact `1_2_yosys.v` target rather than
   the phony `synth` target for interception;
2. stage `synth` is also intercepted before `synth_odb`;
3. a later physical stage reuses the preserved raw Yosys artifact rather than
   overwriting it with the sanitized copy;
4. a genuine new Yosys rebuild refreshes raw authority correctly;
5. normalized synthesis hashing continues to use the raw artifact.
