# Baseline qualification: allow hold repair up to the original cell count.
# Same hook is frozen for every candidate; constraints are unchanged.
rename repair_timing_helper nebula_original_repair_timing_helper
proc repair_timing_helper {args} {
  nebula_original_repair_timing_helper {*}$args -max_buffer_percent 100
}
