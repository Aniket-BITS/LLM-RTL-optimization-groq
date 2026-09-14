# Self-contained nEBULA smoke design for ORFS/Nangate45.
export DESIGN_NAME = nebula_smoke
export PLATFORM = nangate45
DESIGN_DIR := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))
export VERILOG_FILES := $(DESIGN_DIR)/nebula_smoke.v
export SDC_FILE := $(DESIGN_DIR)/constraint.sdc
export CORE_UTILIZATION = 30
export PLACE_DENSITY = 0.40
export CORE_ASPECT_RATIO = 1
export CORE_MARGIN = 2
