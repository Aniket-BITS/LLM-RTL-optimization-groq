# Generic second-pass physical witness reporting for nEBULA.
# Required environment:
#   NEBULA_WITNESS_REQUEST : nebula_witness_request/1 JSON
#   NEBULA_REPORTS_DIR     : output directory (falls back to REPORTS_DIR)

proc nebula_json_escape {value} {
    return [string map [list "\\" "\\\\" "\"" "\\\"" "\n" "\\n" "\r" "\\r" "\t" "\\t"] $value]
}

if {![info exists ::env(NEBULA_WITNESS_REQUEST)] || $::env(NEBULA_WITNESS_REQUEST) eq ""} {
    error "NEBULA_WITNESS_REQUEST is required"
}
set request_file $::env(NEBULA_WITNESS_REQUEST)
if {![file isfile $request_file]} {
    error "Witness request JSON not found: $request_file"
}

if {[info exists ::env(NEBULA_REPORTS_DIR)] && $::env(NEBULA_REPORTS_DIR) ne ""} {
    set report_dir $::env(NEBULA_REPORTS_DIR)
} elseif {[info exists ::env(REPORTS_DIR)] && $::env(REPORTS_DIR) ne ""} {
    set report_dir $::env(REPORTS_DIR)
} else {
    error "NEBULA_REPORTS_DIR or REPORTS_DIR must be set"
}
file mkdir $report_dir

# Keep JSON parsing out of Tcl: Python emits base64-encoded TSV fields so Tcl
# does not need a JSON package and regex metacharacters remain byte-exact.
set pycode {
import base64, json, sys
p=json.load(open(sys.argv[1], encoding='utf-8'))
if p.get('schema') != 'nebula_witness_request/1':
    raise SystemExit('bad witness request schema')
for r in p.get('requests', []):
    vals=[r.get('witness_id',''), r.get('derivation_status',''), r.get('branch',''),
          r.get('from_pin_regex',''), r.get('to_pin_regex',''), r.get('reason','')]
    print('\t'.join(base64.b64encode(str(v).encode()).decode() for v in vals))
}
set encoded_rows [exec python3 -c $pycode $request_file]

puts ""
puts "============================================================"
puts "nEBULA targeted witness reporting"
puts "Request : $request_file"
puts "Reports : $report_dir"
puts "============================================================"

foreach row [split $encoded_rows "\n"] {
    if {[string trim $row] eq ""} { continue }
    set fields [split $row "\t"]
    if {[llength $fields] != 6} {
        puts "WARNING: malformed witness request row"
        continue
    }
    lassign $fields e_id e_status e_branch e_from e_to e_reason
    set witness_id [binary decode base64 $e_id]
    set derivation_status [binary decode base64 $e_status]
    set branch [binary decode base64 $e_branch]
    set from_re [binary decode base64 $e_from]
    set to_re [binary decode base64 $e_to]
    set reason [binary decode base64 $e_reason]

    set status_file [file join $report_dir "witness_${witness_id}.json"]
    set report_file [file join $report_dir "witness_${witness_id}.rpt"]

    if {$derivation_status ne "ready"} {
        set f [open $status_file w]
        puts $f "{\"schema\":\"nebula_witness_result/1\",\"witness_id\":\"[nebula_json_escape $witness_id]\",\"branch\":\"[nebula_json_escape $branch]\",\"resolved\":false,\"reason\":\"[nebula_json_escape $reason]\"}"
        close $f
        continue
    }

    set from_pins {}
    set to_pins {}
    set query_error ""
    set query_status [catch {
        set from_pins [get_pins -hierarchical -regexp $from_re]
        set to_pins [get_pins -hierarchical -regexp $to_re]
    } query_error]

    set resolved false
    set result_reason ""
    if {$query_status} {
        set result_reason "pin_query_failed: $query_error"
    } elseif {[llength $from_pins] == 0} {
        set result_reason "from_pin_unresolved"
    } elseif {[llength $to_pins] == 0} {
        set result_reason "to_pin_unresolved"
    } else {
        set report_error ""
        set report_status [catch {
            report_checks \
                -from $from_pins \
                -to $to_pins \
                -path_delay max \
                -group_path_count 1 \
                -endpoint_path_count 1 \
                -sort_by_slack \
                -format full_clock_expanded \
                -fields {input_pin slew capacitance fanout net} \
                -digits 4 \
                -no_line_splits \
                > $report_file
        } report_error]
        if {$report_status} {
            set result_reason "report_checks_failed: $report_error"
        } else {
            set resolved true
            set result_reason "ok"
        }
    }

    set f [open $status_file w]
    puts $f "{\"schema\":\"nebula_witness_result/1\",\"witness_id\":\"[nebula_json_escape $witness_id]\",\"branch\":\"[nebula_json_escape $branch]\",\"resolved\":$resolved,\"reason\":\"[nebula_json_escape $result_reason]\",\"from_pin_count\":[llength $from_pins],\"to_pin_count\":[llength $to_pins],\"report\":\"[nebula_json_escape $report_file]\"}"
    close $f
    puts "$witness_id ($branch): $result_reason"
}

puts "nEBULA witness reporting complete."
