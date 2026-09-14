#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$ROOT/env.sh"
cd "$ROOT"
cmd="${1:-help}"
shift || true
case "$cmd" in
 self-test) exec "$NEBULA_PYTHON" "$ROOT/self_test.py" "$@" ;;
 e2e) exec "$NEBULA_PYTHON" "$ROOT/run_end_to_end.py" "$@" ;;
 test) exec "$NEBULA_PYTHON" -m pytest timing_automation/tests "$@" ;;
 smoke) exec bash tools/smoke_test.sh "$@" ;;
 analysis) script=run_analysis ;;
 optimize) script=run_optimizer ;;
 closure) script=run_closure_loop ;;
 rtl-opt-groq) script=run_rtl_opt_groq ;;
 rtl-opt-batch) script=run_rtl_opt_batch ;;
 aggregate-results) script=aggregate_results ;;
 report-guided-experiment) script=report_guided_experiment ;;
 doctor) script=nebula_doctor ;;
 preflight) script=pre_groq_check ;;
 build-request) script=build_request ;;
 *) echo 'Usage: nebula.sh {self-test|e2e|doctor|test|smoke|analysis|build-request|preflight|optimize|closure|rtl-opt-groq|rtl-opt-batch|aggregate-results} [arguments]'; exit 2 ;;
esac
exec "$NEBULA_PYTHON" "$ROOT/timing_automation/$script.py" "$@"
