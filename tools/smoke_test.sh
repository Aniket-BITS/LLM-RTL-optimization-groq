#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-python3}"
FIXTURE="$ROOT/timing_automation/tests/fixtures/nebula_smoke"
FIXTURES="$ROOT/timing_automation/tests/fixtures"
MANIFEST="$FIXTURE/design_manifest.json"
GOOD="$FIXTURES/smoke_candidate.json"
BAD="$FIXTURES/smoke_candidate_bad.json"
SCHEMA="$ROOT/timing_automation/response_schema_patches_v2.json"

TMPBASE="${TMPDIR:-/tmp}"
WORK="$(mktemp -d "$TMPBASE/nebula_smoke_${USER:-user}_XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

REQUEST="$WORK/request.json"
GOOD_ACCEPTED="$WORK/good_accepted"
BAD_ACCEPTED="$WORK/bad_accepted"
GOOD_LINT="$WORK/good_lint.json"
BAD_LINT="$WORK/bad_lint.json"
GOOD_SEC="$WORK/good_sec.json"
BAD_SEC="$WORK/bad_sec.json"
GOOD_VERIFY="$WORK/good_verify.json"
BAD_VERIFY="$WORK/bad_verify.json"

printf '\n[1] validate fixture manifest, source hash, and candidate JSON schema\n'
"$PYTHON" - "$MANIFEST" "$FIXTURE" "$GOOD" "$BAD" "$SCHEMA" <<'PY'
import hashlib, json, sys
from pathlib import Path
import jsonschema

manifest_path, base_dir, good_path, bad_path, schema_path = map(Path, sys.argv[1:])
manifest=json.loads(manifest_path.read_text())
source=base_dir / manifest['source_files'][0]
actual=hashlib.sha256(source.read_bytes()).hexdigest()
expected=manifest['source_hashes'][manifest['source_files'][0]]
assert actual == expected, (actual, expected)
schema=json.loads(schema_path.read_text())
for path in (good_path,bad_path):
    jsonschema.Draft202012Validator(schema).validate(json.loads(path.read_text()))
print('fixture/schema validation: PASS')
PY

printf '\n[2] build deterministic optimizer request with exact SourceBundle identity\n'
"$PYTHON" - "$MANIFEST" "$FIXTURE" "$REQUEST" <<'PY'
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / 'timing_automation'))
from design_manifest import DesignManifest
from source_bundle import SourceBundle

manifest_path=Path(sys.argv[1]); base=Path(sys.argv[2]); out=Path(sys.argv[3])
manifest=DesignManifest.read_json(manifest_path)
bundle=SourceBundle.from_manifest(manifest, base)
top=bundle.top_source()
request={
    'schema':'optimizer_request/2',
    'request_format_version':'2.2',
    'response_schema_version':'2.0',
    'unit_id':'unit_000',
    'optimizer_mode':'guided',
    'diagnostic_confidence':'high',
    'routing_reason':'smoke_fixture',
    'source_bundle':bundle.prompt_context(),
    'primary_diagnostic_focus':{
        'file':top.manifest_path,
        'module':manifest.top,
        'original_sha256':top.sha256,
    },
    'recommended_focus_ranges':[[14,14]],
    'recommended_pattern_ids':['arith.csla_split'],
}
out.write_text(json.dumps(request,indent=2)+'\n')
print(out)
PY

printf '\n[3] resolve Yosys through nebula_env\n'
YOSYS="$($PYTHON - <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / 'timing_automation'))
from nebula_env import yosys
print(yosys())
PY
)"
printf 'Yosys: %s\n' "$YOSYS"

printf '\n[4] lint known-good candidate and materialize accepted source bundle\n'
"$PYTHON" timing_automation/lint_candidate.py \
  --manifest "$MANIFEST" \
  --base-dir "$FIXTURE" \
  --request "$REQUEST" \
  --candidate "$GOOD" \
  --candidate-out-dir "$GOOD_ACCEPTED" \
  --output "$GOOD_LINT" \
  --yosys "$YOSYS"
cat "$GOOD_LINT"
"$PYTHON" - "$GOOD_LINT" <<'PY'
import json,sys
r=json.load(open(sys.argv[1])); assert r['ok'] is True, r
print('good candidate lint: PASS')
PY

printf '\n[5] lint deliberately carry-buggy candidate (must pass structural gates)\n'
"$PYTHON" timing_automation/lint_candidate.py \
  --manifest "$MANIFEST" \
  --base-dir "$FIXTURE" \
  --request "$REQUEST" \
  --candidate "$BAD" \
  --candidate-out-dir "$BAD_ACCEPTED" \
  --output "$BAD_LINT" \
  --yosys "$YOSYS"
cat "$BAD_LINT"
"$PYTHON" - "$BAD_LINT" <<'PY'
import json,sys
r=json.load(open(sys.argv[1])); assert r['ok'] is True, r
print('bad candidate structural lint: PASS')
PY

printf '\n[6] build SEC bundle for known-good candidate\n'
"$PYTHON" timing_automation/sec_bundle.py \
  --golden-manifest "$MANIFEST" \
  --golden-base-dir "$FIXTURE" \
  --candidate-manifest "$GOOD_ACCEPTED/design_manifest.json" \
  --candidate-base-dir "$GOOD_ACCEPTED" \
  --output "$GOOD_SEC"

printf '\n[7] prove known-good candidate equivalent\n'
"$PYTHON" timing_automation/verify_rtl.py \
  --bundle "$GOOD_SEC" \
  --output "$GOOD_VERIFY" \
  --yosys "$YOSYS"
"$PYTHON" - "$GOOD_VERIFY" <<'PY'
import json,sys
r=json.load(open(sys.argv[1])); assert r['status']=='proven_complete', r
print('good candidate -> PROVEN_COMPLETE')
PY

printf '\n[8] build SEC bundle for deliberately bad candidate\n'
"$PYTHON" timing_automation/sec_bundle.py \
  --golden-manifest "$MANIFEST" \
  --golden-base-dir "$FIXTURE" \
  --candidate-manifest "$BAD_ACCEPTED/design_manifest.json" \
  --candidate-base-dir "$BAD_ACCEPTED" \
  --output "$BAD_SEC"

printf '\n[9] prove deliberately bad candidate is disproven\n'
set +e
"$PYTHON" timing_automation/verify_rtl.py \
  --bundle "$BAD_SEC" \
  --output "$BAD_VERIFY" \
  --yosys "$YOSYS"
RC=$?
set -e
if [[ "$RC" -ne 3 ]]; then
  echo "ERROR: expected bad candidate verification exit 3, got $RC" >&2
  cat "$BAD_VERIFY" >&2 || true
  exit 1
fi
"$PYTHON" - "$BAD_VERIFY" <<'PY'
import json,sys
r=json.load(open(sys.argv[1])); assert r['status']=='disproven', r
print('bad candidate  -> DISPROVEN (exit 3)')
PY

printf '\nSmoke test: PASS\n'
