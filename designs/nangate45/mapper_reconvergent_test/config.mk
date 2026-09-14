export PLATFORM = nangate45

export DESIGN_NAME = mapper_reconvergent_test

export VERILOG_FILES = $(sort $(wildcard ./designs/src/$(DESIGN_NAME)/*.v))

export SDC_FILE = ./designs/$(PLATFORM)/$(DESIGN_NAME)/constraint.sdc

export CORE_UTILIZATION = 40

export CORE_ASPECT_RATIO = 1

export CORE_MARGIN = 2

export PLACE_DENSITY = 0.60

export POST_FINAL_REPORT_TCL = $(DESIGN_HOME)/nangate45/mapper_reconvergent_test/timing_report.tcl

# Frozen geometry for Groq r2 baseline/candidate comparison.
export CORE_UTILIZATION =
export DIE_AREA = 0.0 0.0 56.595 56.595
export CORE_AREA = 2.09 2.8 54.53 53.2
export NUM_CORES = 4
export PRE_CTS_TCL = /home/aniket/OpenROAD-flow-scripts/flow/versions/phase5_to_9_groq_r2/timing_automation/physical_recipes/nebula_nangate45_repair_budget.tcl
export PRE_GLOBAL_ROUTE_TCL = /home/aniket/OpenROAD-flow-scripts/flow/versions/phase5_to_9_groq_r2/timing_automation/physical_recipes/nebula_nangate45_repair_budget.tcl
