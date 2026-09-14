#!/usr/bin/env bash
NEBULA_VERSION_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export NEBULA_FLOW_DIR="$NEBULA_VERSION_ROOT"
export NEBULA_ORFS_ROOT="$(cd "$NEBULA_VERSION_ROOT/../../.." && pwd)"
export NEBULA_YOSYS="$NEBULA_ORFS_ROOT/tools/install/yosys/bin/yosys"
export NEBULA_OPENROAD="$NEBULA_ORFS_ROOT/tools/install/OpenROAD/bin/openroad"
export YOSYS_EXE="$NEBULA_YOSYS"
export OPENROAD_EXE="$NEBULA_OPENROAD"
export NEBULA_PYTHON="$NEBULA_VERSION_ROOT/.venv/bin/python"
export PYTHON="$NEBULA_PYTHON"
export PYTHONPATH="$NEBULA_VERSION_ROOT"
export PATH="$NEBULA_VERSION_ROOT/.venv/bin:$(dirname "$NEBULA_YOSYS"):$(dirname "$NEBULA_OPENROAD"):$PATH"
export OPTIMIZER_MODEL="${OPTIMIZER_MODEL:-openai/gpt-oss-120b}"
export NUM_CORES="${NUM_CORES:-4}"
export OMP_NUM_THREADS="$NUM_CORES"
if [[ -f "$NEBULA_VERSION_ROOT/.env.local" ]]; then
  set -a
  source "$NEBULA_VERSION_ROOT/.env.local"
  set +a
fi
