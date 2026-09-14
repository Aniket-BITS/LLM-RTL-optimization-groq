# nEBULA Phase 5–9 operating commands

Run these commands from the OpenROAD-flow-scripts `flow/` directory after replacing `timing_automation/` with this package.

## 1. Activate environment and verify Python

```bash
cd ~/OpenROAD-flow-scripts/flow
source .venv-nebula/bin/activate
python3 -m compileall -q timing_automation
python3 -m pytest timing_automation/tests -q
```

If your checkout contains the project helper scripts, also run:

```bash
bash tools/run_all_selftests.sh
python3 tools/check_duplicates.py
bash tools/smoke_test.sh
```

## 2. Run the analysis pipeline

```bash
python3 timing_automation/run_analysis.py \
  --design <DESIGN> \
  --platform nangate45 \
  --liberty "$PWD/platforms/nangate45/lib/NangateOpenCellLibrary_typical.lib" \
  --verbose
```

The pipeline now routes request-safe work into `guided` or `exploratory` optimizer units. Remaining physical ambiguity is diagnostic evidence, not by itself an optimizer blocker.

To resume a known run:

```bash
python3 timing_automation/run_analysis.py \
  --design <DESIGN> \
  --platform nangate45 \
  --variant <FLOW_VARIANT> \
  --resume \
  --liberty "$PWD/platforms/nangate45/lib/NangateOpenCellLibrary_typical.lib" \
  --verbose
```

To rerun only a later diagnostic stage:

```bash
# examples
--force-stage select_sat
--force-stage witness
--force-stage case_physical
--force-stage classification
--force-stage optimizer_input
```

## 3. Inspect the resulting optimizer packet

```bash
python3 - <<'PY'
import json, glob
p=sorted(glob.glob('ai_runs/*_optimizer_input.json'))[-1]
d=json.load(open(p))
print('FILE:', p)
print('validation:', json.dumps(d.get('validation'), indent=2))
for u in d.get('optimization_units', []):
    e=u.get('eligibility') or {}
    print(u.get('unit_id'), u.get('optimizer_mode'), e.get('request_ready'), u.get('diagnostic_confidence'))
PY
```

## 4. Build a request for one unit

```bash
python3 timing_automation/build_request.py \
  --optimizer-input ai_runs/<FLOW_VARIANT>_optimizer_input.json \
  --unit-id unit_000 \
  --golden-manifest <GOLDEN_DESIGN_MANIFEST.json> \
  --golden-base-dir <GOLDEN_SOURCE_ROOT> \
  --output ai_runs/<FLOW_VARIANT>_unit_000_request.json
```

For a guided unit you may first produce recommendations:

```bash
python3 timing_automation/filter_patterns.py \
  --optimizer-input ai_runs/<FLOW_VARIANT>_optimizer_input.json \
  --unit-id unit_000 \
  --output ai_runs/<FLOW_VARIANT>_unit_000_patterns.json

python3 timing_automation/build_request.py \
  --optimizer-input ai_runs/<FLOW_VARIANT>_optimizer_input.json \
  --unit-id unit_000 \
  --eligible ai_runs/<FLOW_VARIANT>_unit_000_patterns.json \
  --golden-manifest <GOLDEN_DESIGN_MANIFEST.json> \
  --golden-base-dir <GOLDEN_SOURCE_ROOT> \
  --output ai_runs/<FLOW_VARIANT>_unit_000_request.json
```

If guided recommendations are exhausted, omit `--eligible`; request construction falls back to exploratory mode rather than treating pattern exhaustion as a closure stop.

## 5. Preflight the LLM request

```bash
python3 timing_automation/pre_groq_check.py \
  --request ai_runs/<FLOW_VARIANT>_unit_000_request.json
```

## 6. Run one optimizer candidate

```bash
python3 timing_automation/run_optimizer.py \
  --request ai_runs/<FLOW_VARIANT>_unit_000_request.json \
  --golden-manifest <GOLDEN_DESIGN_MANIFEST.json> \
  --golden-base-dir <GOLDEN_SOURCE_ROOT> \
  --physical-recipe <PHYSICAL_RECIPE.json> \
  --baseline-metrics ai_runs/<FLOW_VARIANT>_metrics.json \
  --flow-dir "$PWD" \
  --ai-runs "$PWD/ai_runs" \
  --tag candidate_001 \
  --history-file "$PWD/ai_runs/optimizer_history.jsonl" \
  --model alongwith/chipseek-r1-qwen2.5 \
  --temperature 0
```

The recommended source lines are advisory. Hard gates remain SourceBundle membership/hash/module ownership, structural policy, formal equivalence, and same-recipe PPA evaluation.

## 7. Run the closure-attempt driver

```bash
python3 timing_automation/run_closure_loop.py \
  --optimizer-input ai_runs/<FLOW_VARIANT>_optimizer_input.json \
  --golden-manifest <GOLDEN_DESIGN_MANIFEST.json> \
  --golden-base-dir <GOLDEN_SOURCE_ROOT> \
  --physical-recipe <PHYSICAL_RECIPE.json> \
  --baseline-metrics ai_runs/<FLOW_VARIANT>_metrics.json \
  --flow-dir "$PWD" \
  --ai-runs "$PWD/ai_runs" \
  --max-attempts 3 \
  --max-flows 3 \
  --chain baseline \
  --yes
```

`--chain baseline` compares independent candidates against the analysed baseline. `--chain accept` stops safely after a promotion and writes the promoted candidate bundle path; re-run `run_analysis.py` on the promoted RTL before continuing so a stale timing diagnosis is never reused.

For a formal-only dry run:

```bash
python3 timing_automation/run_closure_loop.py \
  ...same arguments... \
  --no-orfs
```

## 8. Phase-9 advanced attribution only

Normally `run_analysis.py` invokes this conditionally. To rerun the integrated advanced physical-attribution stage on a resumed analysis:

```bash
python3 timing_automation/run_analysis.py \
  --design <DESIGN> \
  --platform nangate45 \
  --variant <FLOW_VARIANT> \
  --resume \
  --force-stage case_physical \
  --liberty "$PWD/platforms/nangate45/lib/NangateOpenCellLibrary_typical.lib" \
  --verbose
```

Unsupported case-analysis boundary kinds remain exploratory; solver/provenance errors fail closed.
