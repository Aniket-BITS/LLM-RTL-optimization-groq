# Generic nEBULA critical-path reporting.
# No design-specific register names or bit indices are embedded here.

proc nebula_json_escape {value} {
    return [string map [list "\\" "\\\\" "\"" "\\\"" "\n" "\\n" "\r" "\\r" "\t" "\\t"] $value]
}

proc nebula_safe_name {value} {
    set out $value
    regsub -all {[^A-Za-z0-9_.-]} $out {_} out
    if {$out eq ""} { set out "all" }
    return $out
}

if {[info exists ::env(NEBULA_REPORTS_DIR)] && $::env(NEBULA_REPORTS_DIR) ne ""} {
    set report_dir $::env(NEBULA_REPORTS_DIR)
} elseif {[info exists ::env(REPORTS_DIR)] && $::env(REPORTS_DIR) ne ""} {
    set report_dir $::env(REPORTS_DIR)
} else {
    error "NEBULA_REPORTS_DIR or REPORTS_DIR must be set"
}
file mkdir $report_dir

set path_count 20
if {[info exists ::env(NEBULA_PATH_COUNT)] && [string is integer -strict $::env(NEBULA_PATH_COUNT)]} {
    set path_count $::env(NEBULA_PATH_COUNT)
}

set max_slack ""
if {[info exists ::env(NEBULA_MAX_SLACK)] && $::env(NEBULA_MAX_SLACK) ne ""} {
    set max_slack $::env(NEBULA_MAX_SLACK)
}

set want_json 1
if {[info exists ::env(NEBULA_REPORT_JSON)]} {
    set want_json $::env(NEBULA_REPORT_JSON)
}

set groups {}
if {[info exists ::env(NEBULA_PATH_GROUPS)] && [string trim $::env(NEBULA_PATH_GROUPS)] ne ""} {
    set groups [split [string trim $::env(NEBULA_PATH_GROUPS)]]
} else {
    set clocks [all_clocks]
    foreach clk $clocks {
        set name ""
        if {[catch {set name [get_name $clk]}]} {
            set name [string trim $clk]
        }
        if {$name ne ""} { lappend groups $name }
    }
}

# Designs with no discoverable clock group still get one all-path report.
if {[llength $groups] == 0} {
    set groups [list ""]
}

set registers [all_registers]

puts ""
puts "============================================================"
puts "nEBULA generic critical-path reporting"
puts "Reports : $report_dir"
puts "Groups  : $groups"
puts "============================================================"

foreach group $groups {
    set display_group $group
    if {$display_group eq ""} { set display_group "all" }
    set stem [nebula_safe_name $display_group]
    set critical_rpt [file join $report_dir "${stem}_critical_paths.rpt"]
    set reg2reg_rpt [file join $report_dir "${stem}_reg2reg_critical_paths.rpt"]
    set critical_json [file join $report_dir "${stem}_critical_paths.json"]
    set status_json [file join $report_dir "${stem}_report_status.json"]

    set errors {}
    set critical_ok true
    set reg2reg_ok true
    set json_ok false

    set critical_error ""
    if {$group eq ""} {
        if {$max_slack eq ""} {
            set rc [catch {
                report_checks -path_delay max -group_path_count $path_count -endpoint_path_count 1 -sort_by_slack -format full_clock_expanded -fields {input_pin slew capacitance fanout net} -digits 4 -no_line_splits > $critical_rpt
            } critical_error]
        } else {
            set rc [catch {
                report_checks -path_delay max -slack_max $max_slack -group_path_count $path_count -endpoint_path_count 1 -sort_by_slack -format full_clock_expanded -fields {input_pin slew capacitance fanout net} -digits 4 -no_line_splits > $critical_rpt
            } critical_error]
        }
    } else {
        if {$max_slack eq ""} {
            set rc [catch {
                report_checks -path_delay max -path_group $group -group_path_count $path_count -endpoint_path_count 1 -sort_by_slack -format full_clock_expanded -fields {input_pin slew capacitance fanout net} -digits 4 -no_line_splits > $critical_rpt
            } critical_error]
        } else {
            set rc [catch {
                report_checks -path_delay max -path_group $group -slack_max $max_slack -group_path_count $path_count -endpoint_path_count 1 -sort_by_slack -format full_clock_expanded -fields {input_pin slew capacitance fanout net} -digits 4 -no_line_splits > $critical_rpt
            } critical_error]
        }
    }
    if {$rc} {
        set critical_ok false
        lappend errors "critical:$critical_error"
        catch {file delete $critical_rpt}
    }

    if {[llength $registers] > 0} {
        set rr_error ""
        if {$group eq ""} {
            set rr_rc [catch {
                report_checks -path_delay max -from $registers -to $registers -group_path_count $path_count -endpoint_path_count 1 -sort_by_slack -format full_clock_expanded -fields {input_pin slew capacitance fanout net} -digits 4 -no_line_splits > $reg2reg_rpt
            } rr_error]
        } else {
            set rr_rc [catch {
                report_checks -path_delay max -path_group $group -from $registers -to $registers -group_path_count $path_count -endpoint_path_count 1 -sort_by_slack -format full_clock_expanded -fields {input_pin slew capacitance fanout net} -digits 4 -no_line_splits > $reg2reg_rpt
            } rr_error]
        }
        if {$rr_rc} {
            set reg2reg_ok false
            lappend errors "reg2reg:$rr_error"
            catch {file delete $reg2reg_rpt}
        }
    } else {
        set reg2reg_ok false
        lappend errors "reg2reg:no_registers"
    }

    if {$want_json} {
        set json_error ""
        if {$group eq ""} {
            set json_rc [catch {
                report_checks -path_delay max -group_path_count $path_count -endpoint_path_count 1 -sort_by_slack -format json -digits 4 > $critical_json
            } json_error]
        } else {
            set json_rc [catch {
                report_checks -path_delay max -path_group $group -group_path_count $path_count -endpoint_path_count 1 -sort_by_slack -format json -digits 4 > $critical_json
            } json_error]
        }
        if {$json_rc} {
            catch {file delete $critical_json}
        } else {
            set json_ok true
        }
    }

    set f [open $status_json w]
    set escaped_group [nebula_json_escape $display_group]
    puts -nonewline $f "{\"schema\":\"nebula_report_status/1\",\"path_group\":\"$escaped_group\",\"critical_report_ok\":$critical_ok,\"reg2reg_report_ok\":$reg2reg_ok,\"json_report_ok\":$json_ok,\"errors\":\["
    set first true
    foreach err $errors {
        if {!$first} { puts -nonewline $f "," }
        puts -nonewline $f "\"[nebula_json_escape $err]\""
        set first false
    }
    puts $f "\]}"
    close $f

    puts "$display_group: critical=$critical_ok reg2reg=$reg2reg_ok json=$json_ok"
}

puts "nEBULA generic critical-path reporting complete."
