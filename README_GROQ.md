# NEBULA Groq r3: optional evidence-backed guidance

This version is at `flow/versions/phase5_to_9_groq_r3`, derived from `phase5_to_9_groq_r2`. The original version and source RTL are preserved. Old experiment outputs were not copied.

The `rtl-opt-groq` runner offers `auto` (default), `guided`, and `exploratory`. Both `auto` and `guided` attempt timing-backed diagnosis and degrade safely according to the evidence:

| Evidence | Prompt state |
|---|---|
| High-confidence, hash-verified mapping and high/medium-confidence classification | `guided` |
| High-confidence mapping without reliable classification | `guided_evidence_only` |
| Missing, ambiguous, unsupported, or low-confidence mapping | `exploratory` |
| Diagnostic exception, malformed data, or exhausted advisory budget | `exploratory`, with a persisted reason |

Guidance is advisory. It never gates source validation, Groq, formal equivalence, or physical evaluation. A graph-builder timeout is recorded as failed guidance. Only exact source hashes and complete trusted source regions qualify as mapping evidence. Reconvergent cones without resolved attribution remain exploratory. Classification is structural evidence, not proof that one RTL operator causes the physical delay. No carry-preservation, subtraction-avoidance, or design-ID-based architecture rules are added.

Each design selects a setup/hold-clean baseline clock period. The first diagnosis comes from that selected run. Guidance is always regenerated from the promoted-only CURRENT BEST snapshot; a rejected candidate never becomes the next diagnosis/PPA base. The model may construct one complete-source candidate from either GOLDEN or CURRENT BEST, but formal remains anchored to GOLDEN and physical comparison remains anchored to the unchanged CURRENT BEST snapshot at attempt start.

The exact system/user prompt, full and compact guidance, source identity, budget decisions, retry outcomes, and measured deltas are saved per attempt. Timing reports are parsed by the existing parser; the existing RTL graph mapper and classifier supply evidence. The old raw report excerpt is retained only in baseline diagnostic artifacts and is excluded from prompts.

After lint, elaboration, and formal proof, both implementations run the same frozen synthesis recipe. Valid equal normalized post-synthesis hashes produce `no_op_after_synthesis`, skip global-route PPA, and request a materially different implementation strategy. Hash normalization conservatively preserves names, bit IDs, connectivity, cell parameters, and initialization while removing source-location metadata. Consequently it can miss renamed no-ops; it does not treat matching cell counts as matching implementations. Missing hashes never trigger a PPA skip.

Acceptance uses measured same-recipe metrics and the user's area, power, setup, and hold limits. Both designs must be timing-clean. The existing measurable-improvement threshold remains: at least 1% area improvement, 1% power improvement, or 0.01 ns setup-slack improvement. Provisional acceptance is confirmed by a repeated baseline/candidate comparison under the same budgets. Model predictions never enforce acceptance. Vectorless power remains the existing flow's estimate.

Paired experiments share baseline period selection for each design/repetition, isolate both arms' physical output names, use identical model/settings/budgets, and alternate arm order between repetitions. Existing output directories are refused. This prevents runner overwrites; it is not an operating-system write lock. Repetitions measure provider/run variability; identical model settings do not guarantee deterministic outputs.

## Local setup and validation

```bash
cd ~/OpenROAD-flow-scripts/flow/versions/phase5_to_9_groq_r3
./nebula.sh self-test
./nebula.sh rtl-opt-groq --help
```

The local `.venv` reuses r2's installed Python environment through a symlink, and `.env.local` links to the existing private r2 configuration without duplicating its contents. ORFS scripts, platforms, external corpus, and installed EDA tools remain shared dependencies. `env.sh` loads this version's private `.env.local` when present, or accepts an exported `GROQ_API_KEY`. Private keys and old run outputs are excluded from the version's change artifacts. This is a versioned runner checkout, not a standalone ORFS distribution.

Validation covers pure-Python fixtures, mocked runner paths, and syntax. No live Groq, formal, or PPA run was performed for r3. The inherited `e2e`/closure commands are separate workflows; the new guidance controls belong to `rtl-opt-groq`.

See `OPERATING_COMMANDS.md` for commands and artifacts, and `VALIDATION.md` for review notes and known limits.

## Latency-aware Golden / Current-Best optimizer semantics

`rtl-opt-groq` now uses `timing_automation/response_schema_latency.json` (schema 3.2) and
keeps both the shared legacy `response_schema.json` and prior pre-freeze schema unchanged for other/replay entry points.

For this runner:

- **Golden RTL** is immutable and is always the formal correctness reference.
- **Current Best RTL** starts as Golden and changes only after repeat-confirmed promotion.
- The model chooses `editing_base = golden | current_best` for candidate construction.
- Exact Golden/Current-Best/prior-candidate copies are hard rejected before formal.
- The prompt prohibits comment/formatting-only rewrites; the hard pre-formal duplicate gate is byte-exact, while source-different hardware no-ops are authoritatively caught by normalized synthesis hashing.
- Normalized synthesis hashing remains the authority for synthesis equivalence.
- PPA always compares Candidate against the unchanged Current Best snapshot for that attempt.
- Rejected candidates never become the diagnosis/PPA base.
- Promotion creates a new Current-Best epoch and optimization continues while attempts remain.
- `no_further_optimization` is a legal advisory model response; backend convergence owns termination.
- Fixed-cycle latency-changing/pipelining verification is available through `rtl-opt-groq` with `--max-latency-regression-cycles N`. Budget `0` preserves the old cycle-exact path; positive budgets use backend temporal formal against frozen Golden before PPA. Phase-1 is intentionally limited to one existing reference clock and one design-level non-negative cycle shift.

Latency-aware transformation-choice update (v1.2)
- Pipeline insertion is available immediately when latency headroom exists; the LLM chooses whether it is worthwhile.
- Latency is described as a limited resource, not a preferred transformation.
- --model-history-limit defaults to 5 recent meaningful attempts; older search history is aggregated.
- Model intent uses primary_transformation + proposed_transformations; backend structural effects and verified latency remain authoritative.
