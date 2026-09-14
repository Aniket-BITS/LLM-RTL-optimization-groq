# Operating Groq r3 — latency-aware Golden / Current-Best architecture

Run commands from:

```bash
cd ~/OpenROAD-flow-scripts/flow/versions/phase5_to_9_groq_r3
```

## State semantics

The optimizer now has three explicit model contexts:

```text
GOLDEN RTL
    immutable original correctness reference

CURRENT BEST RTL
    best repeat-confirmed promoted implementation
    starts equal to Golden

PREVIOUS ATTEMPTS
    compact backend-generated outcomes only
```

Hard backend rules:

```text
Formal verification: Candidate vs GOLDEN
PPA/STA comparison:  Candidate vs unchanged CURRENT BEST snapshot
Guidance source:      CURRENT BEST only
Rejected candidate:   never updates CURRENT BEST
Promoted candidate:   becomes a new immutable CURRENT BEST epoch
```

The model independently chooses `editing_base = golden | current_best` for each
candidate. The editing base controls construction only; it never changes the
formal or PPA reference rules.

The model is explicitly prohibited from returning Golden, Current Best, or a
previous candidate unchanged. Exact source duplicates and formatting/comment-only
no-ops are rejected before formal. Normalized synthesis equivalence remains a
backend check; synthesis-equivalent rejected implementations skip OpenROAD PPA.

The model may return `no_further_optimization`. This is advisory. nEBULA only
accepts that stop recommendation when backend convergence evidence supports it.
Automatic convergence is independently enforced by the configured thresholds.

## Latency-changing / pipelining policy

`--max-latency-regression-cycles 0` is the backward-compatible default and keeps
the existing cycle-exact formal path. A positive value enables the phase-1
latency verifier: one existing reference clock, unchanged top interface, and one
backend-proven non-negative fixed cycle shift for the complete design. The model
may predict latency and declare pipeline/retiming intent, but those fields are
advisory. Nebula independently proves the Candidate-vs-Golden temporal relation
before synthesis or PPA. New clocks, clock domains, latches, and changed external
clock/reset behavior remain deterministic lint failures.

Total latency regression is always measured from frozen Golden. PPA remains
Candidate versus the unchanged Current Best at attempt start, so latency cannot
creep across promotions. Variable-latency protocols, CDC latency changes, and
multi-clock latency-changing transformations are intentionally unsupported in
this phase. If a positive budget is requested for an unsupported design, Nebula
advertises zero effective latency headroom and safely falls back to the existing
cycle-exact formal path so ordinary same-cycle optimizations can still proceed.

Use separate retry budgets when desired:

```text
--max-model-calls N             hard provider-call cap
--max-meaningful-iterations M   novel structurally valid candidates reaching semantic verification
```

Provider failures, truncation, malformed responses, exact source duplicates, and
deterministic materialization/lint failures do not consume the meaningful
iteration budget. `--max-attempts` remains the backward-compatible default for
both limits when the two explicit flags are omitted.

## Recommended guided run

```bash
./nebula.sh rtl-opt-groq \
  --output "$PWD/ai_runs/prefreeze_eval_01" \
  --pairs comparator_16bit fsm_encode mul_const calculation adder_carry \
  --max-model-calls 8 \
  --max-meaningful-iterations 5 \
  --optimizer-mode guided \
  --max-latency-regression-cycles 0 \
  --max-setup-slack-regression-ns 0.010 \
  --max-hold-slack-regression-ns 0.010 \
  --convergence-min-unique-syntheses 2 \
  --convergence-duplicate-limit 2
```

Area and power regression limits remain configurable with
`--max-area-regression-pct` and `--max-power-regression-pct`. Setup/hold limits
are positive maximum losses; `0.010` means up to 10 ps regression is permitted.
Absolute timing-clean checks remain mandatory.

To permit a one-cycle pipeline regression from Golden on an eligible sequential
design, use for example:

```bash
./nebula.sh rtl-opt-groq \
  --output "$PWD/ai_runs/latency_adder_01" \
  --pairs adder \
  --max-model-calls 8 \
  --max-meaningful-iterations 5 \
  --optimizer-mode guided \
  --max-latency-regression-cycles 1 \
  --max-setup-slack-regression-ns 0.010 \
  --max-hold-slack-regression-ns 0.010 \
  --convergence-min-unique-syntheses 2 \
  --convergence-duplicate-limit 2
```

## Convergence

Automatic convergence is scoped to the active Current-Best epoch. With defaults,
the design stops when at least two distinct synthesized implementations have been
fully PPA-evaluated and rejected in that epoch and the search later hits already
rejected synthesized implementations twice. Duplicate memory itself remains
global across Current-Best epochs, so old implementations are never needlessly
re-evaluated after promotion.

A model `no_further_optimization` recommendation may be accepted only after the
backend has already measured at least the configured number of distinct rejected
synthesized implementations and observed at least one synthesis duplicate in the
same Current-Best epoch.

## Runtime state layout

Each design run creates:

```text
<design>/
  state/
    golden/
      design_manifest.json
      snapshot.json
      <sources>
    golden_snapshot.json
    current_best/
      epoch_000/
      epoch_001/
      ...
    current_best_snapshot.json
    current_best_history.jsonl
    candidate_identity_index.json
    candidate_identity_index.json
    synthesis_implementation_index.json
  baseline_guidance.json
  attempt_01/
  attempt_02/
  ...
  result.json
```

`golden_snapshot.json` never changes. `current_best_snapshot.json` points to the
latest promoted epoch. `current_best_history.jsonl` is append-only. Raw candidate
and synthesis fingerprints stay in backend state files; the LLM only receives
compact labels such as `synth_impl_001`.

## Per-attempt records

Every attempt retains:

```text
design
attempt_number
prompt_tokens
completion_tokens
total_tokens
path_groups_considered
optimization_mode
candidate response/source hashes
normalized synthesis hash
editing_base
current_best_epoch
lint result
formal-equivalence result
latency result
PPA result
promotion/rejection result
rejection reason
```

Exact duplicates may not have formal/synthesis/PPA results because they stop
earlier. Synthesis duplicates retain the normalized synthesis identity and skip
PPA. Provider/tool failures do not populate rejected-synthesis convergence sets.

## Promotion

Promotion no longer ends the design run immediately. A repeat-confirmed accepted
candidate becomes the next Current Best epoch, timing/bottleneck guidance is
regenerated from it, and remaining attempts may improve it further. Golden stays
unchanged for all formal proofs.

## Robustness/scalability release (v1.4)

Latency-aware fixed-cycle verification remains unchanged from V1.3. V1.4 adds
operational hardening only: model self-checks are advisory, signed post-synthesis
netlists receive an auditable OpenSTA compatibility copy, raw synthesis remains
authoritative for dedupe, stage timeouts are configurable, design capabilities
are recorded before attempt 1, and multi-design runs are fault-isolated.

EQY/SBY, variable-latency protocols, multi-clock latency-changing transforms,
and formal/mapping architecture redesigns remain intentionally deferred.

Latency-aware transformation-choice update (v1.2)
- Pipeline insertion is available immediately when latency headroom exists; the LLM chooses whether it is worthwhile.
- Latency is described as a limited resource, not a preferred transformation.
- --model-history-limit defaults to 5 recent meaningful attempts; older search history is aggregated.
- Model intent uses primary_transformation + proposed_transformations; backend structural effects and verified latency remain authoritative.

## V1.4 robust single-design example

```bash
./nebula.sh rtl-opt-groq \
  --output "$PWD/ai_runs/v1_4_mul" \
  --pairs mul \
  --max-model-calls 8 \
  --max-meaningful-iterations 5 \
  --max-latency-regression-cycles 1 \
  --formal-timeout-s 120 \
  --ppa-timeout-s 900 \
  --optimizer-mode guided
```

## V1.4 fault-isolated batch example

```bash
./nebula.sh rtl-opt-batch \
  --output "$PWD/ai_runs/v1_4_batch" \
  --pairs adder mul saturating_add register \
  --resume \
  --max-design-runtime-min 60 \
  --max-model-calls 8 \
  --max-meaningful-iterations 5 \
  --max-latency-regression-cycles 1 \
  --formal-timeout-s 120 \
  --ppa-timeout-s 900 \
  --optimizer-mode guided
```

`--resume` skips completed design directories. `--retry-failed` archives a
previous design directory before rerunning it. Batch state is written
atomically to `batch_state.json`; aggregate attempt tables are produced as
`aggregate_results.json` and `aggregate_results.csv`.

## Environment preflight

```bash
./nebula.sh doctor --groq --calibration --verbose
```

The doctor checks tool resolution, platform/liberty availability, schemas,
disk space, Groq credentials, RTL-OPT payload integrity, and the STA netlist
compatibility helper before an expensive run.

## V1.4.1 signed-netlist handoff hotfix

For every ORFS physical stage, nEBULA now intercepts at the exact Yosys file target
`<results-dir>/1_2_yosys.v` before invoking the requested ORFS stage.  This is
intentional: ORFS's phony `synth` target continues into OpenROAD `synth_odb`, so
it is too late for the OpenSTA compatibility transform.

The authoritative artifacts are:

- `1_2_yosys.raw.v` — untouched Yosys output; used for normalized-synthesis identity.
- `1_2_yosys.v` — STA-facing structural copy; may have safe signed declaration qualifiers removed.
- `1_2_yosys_sta_compat.json` — hashes and an audit record of the compatibility transform.

No new CLI option is required.  Existing `rtl-opt-groq` and `rtl-opt-batch`
commands automatically use this sequencing.
