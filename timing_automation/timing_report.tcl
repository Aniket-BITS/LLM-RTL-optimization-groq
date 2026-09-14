# timing_report.tcl

set report_dir "$::env(REPORTS_DIR)/automation"
file mkdir $report_dir

puts "Generating automation timing reports..."

# ---------------------------------------------------------
# 1. Critical timing paths
# ---------------------------------------------------------

report_checks \
    -path_delay max \
    -group_path_count 10 \
    -endpoint_path_count 1 \
    -sort_by_slack \
    -format full_clock_expanded \
    -fields {slew capacitance fanout input_pin net} \
    -digits 4 \
    > "$report_dir/timing_paths.rpt"


# ---------------------------------------------------------
# 2. JSON version of the same paths
# ---------------------------------------------------------

report_checks \
    -path_delay max \
    -group_path_count 10 \
    -endpoint_path_count 1 \
    -sort_by_slack \
    -format json \
    -digits 4 \
    > "$report_dir/timing_paths.json"


# ---------------------------------------------------------
# 3. Timing summary
# ---------------------------------------------------------

report_wns \
    -max \
    -digits 4 \
    > "$report_dir/wns.rpt"

report_tns \
    -max \
    -digits 4 \
    > "$report_dir/tns.rpt"


puts "Timing reports written to:"
puts "$report_dir"
