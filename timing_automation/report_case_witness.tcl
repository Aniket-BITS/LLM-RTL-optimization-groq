# nEBULA Phase-5e case-analysis physical witness reporter.
#
# Required environment:
#   NEBULA_CASE_WITNESS_REQUEST
#   NEBULA_REPORTS_DIR or REPORTS_DIR

proc nebula_json_escape {value} {
    return [string map \
        [list \
            "\\" "\\\\" \
            "\"" "\\\"" \
            "\n" "\\n" \
            "\r" "\\r" \
            "\t" "\\t"] \
        $value]
}

if {
    ![info exists ::env(NEBULA_CASE_WITNESS_REQUEST)]
    || $::env(NEBULA_CASE_WITNESS_REQUEST) eq ""
} {
    error "NEBULA_CASE_WITNESS_REQUEST is required"
}

set request_file $::env(NEBULA_CASE_WITNESS_REQUEST)

if {![file isfile $request_file]} {
    error "Case witness request JSON not found: $request_file"
}

if {
    [info exists ::env(NEBULA_REPORTS_DIR)]
    && $::env(NEBULA_REPORTS_DIR) ne ""
} {
    set report_dir $::env(NEBULA_REPORTS_DIR)
} elseif {
    [info exists ::env(REPORTS_DIR)]
    && $::env(REPORTS_DIR) ne ""
} {
    set report_dir $::env(REPORTS_DIR)
} else {
    error "NEBULA_REPORTS_DIR or REPORTS_DIR must be set"
}

file mkdir $report_dir

set pycode {
import base64
import json
import sys

p = json.load(open(sys.argv[1], encoding="utf-8"))

if p.get("schema") != "nebula_case_witness_request/1":
    raise SystemExit("bad case witness request schema")

for r in p.get("requests", []):
    case_tokens = []

    for item in r.get("case_analysis") or []:
        value = item.get("value")

        if value not in (0, 1):
            raise SystemExit("case-analysis value must be 0/1")

        signal = str(item.get("signal", ""))

        if not signal:
            raise SystemExit(
                "case-analysis signal is required"
            )

        encoded_signal = base64.b64encode(
            signal.encode()
        ).decode()

        case_tokens.append(
            f"{encoded_signal}:{value}"
        )

    case_blob = ",".join(case_tokens)

    vals = [
        r.get("witness_id", ""),
        r.get("derivation_status", ""),
        r.get("branch", ""),
        r.get("from_kind", ""),
        r.get("to_kind", ""),
        r.get("from_regex") or r.get("from_port_regex", ""),
        r.get("to_regex") or r.get("to_port_regex") or r.get("to_pin_regex", ""),
        r.get("from_signal", ""),
        r.get("to_signal", ""),
        r.get("reason", ""),
        case_blob,
    ]

    print(
        "\t".join(
            base64.b64encode(
                str(v).encode()
            ).decode()
            for v in vals
        )
    )
}

set encoded_rows [exec python3 -c $pycode $request_file]

puts ""
puts "============================================================"
puts "nEBULA case-analysis physical witness reporting"
puts "Request : $request_file"
puts "Reports : $report_dir"
puts "============================================================"

foreach row [split $encoded_rows "\n"] {

    if {[string trim $row] eq ""} {
        continue
    }

    set fields [split $row "\t"]

    if {[llength $fields] != 11} {
        puts "WARNING: malformed case witness request row"
        continue
    }

    lassign $fields \
        e_id \
        e_status \
        e_branch \
        e_from_kind \
        e_to_kind \
        e_from \
        e_to \
        e_from_signal \
        e_to_signal \
        e_reason \
        e_cases

    set witness_id [binary decode base64 $e_id]
    set derivation_status [binary decode base64 $e_status]
    set branch [binary decode base64 $e_branch]
    set from_kind [binary decode base64 $e_from_kind]
    set to_kind [binary decode base64 $e_to_kind]
    set from_re [binary decode base64 $e_from]
    set to_re [binary decode base64 $e_to]
    set from_signal [binary decode base64 $e_from_signal]
    set to_signal [binary decode base64 $e_to_signal]
    set reason [binary decode base64 $e_reason]
    set case_blob [binary decode base64 $e_cases]

    set status_file [
        file join $report_dir "case_witness_${witness_id}.json"
    ]

    set report_file [
        file join $report_dir "case_witness_${witness_id}.rpt"
    ]

    if {$derivation_status ne "ready"} {

        set f [open $status_file w]

        puts $f \
            "{\"schema\":\"nebula_case_witness_result/1\",\"witness_id\":\"[nebula_json_escape $witness_id]\",\"branch\":\"[nebula_json_escape $branch]\",\"resolved\":false,\"reason\":\"[nebula_json_escape $reason]\"}"

        close $f
        continue
    }

    set from_objects {}
    set to_objects {}
    set query_error ""

    set query_status [catch {
        if {$from_kind eq "primary_input" || $from_kind eq "primary_output"} {
            set from_objects [get_ports -regexp $from_re]
        } else {
            error "unsupported from_kind: $from_kind"
        }

        if {$to_kind eq "primary_output" || $to_kind eq "primary_input"} {
            set to_objects [get_ports -regexp $to_re]
        } elseif {$to_kind eq "register_capture"} {
            set to_objects [get_pins -hierarchical -regexp $to_re]
        } else {
            error "unsupported to_kind: $to_kind"
        }
    } query_error]

    set resolved false
    set result_reason ""

    set applied_case_ports {}
    set case_count 0

    if {$query_status} {

        set result_reason "boundary_query_failed: $query_error"

    } elseif {[llength $from_objects] != 1} {

        set result_reason "from_boundary_not_unique"

    } elseif {[llength $to_objects] != 1} {

        set result_reason "to_boundary_not_unique"

    } else {

        set case_error ""

        set case_status [
            catch {

                if {$case_blob ne ""} {

                    foreach token [split $case_blob ","] {

                        if {[string trim $token] eq ""} {
                            continue
                        }

                        set parts [split $token ":"]

                        if {[llength $parts] != 2} {
                            error "malformed case token"
                        }

                        lassign $parts encoded_signal case_value

                        if {
                            $case_value ne "0"
                            && $case_value ne "1"
                        } {
                            error "case value must be 0/1"
                        }

                        set case_signal [
                            binary decode base64 $encoded_signal
                        ]

                        if {$case_signal eq $from_signal} {
                            error "case analysis aliases timing launch"
                        }

                        set case_ports [
                            get_ports $case_signal
                        ]

                        if {[llength $case_ports] != 1} {
                            error "case port not unique: $case_signal"
                        }

                        set_case_analysis \
                            $case_value \
                            $case_ports

                        lappend \
                            applied_case_ports \
                            $case_ports

                        incr case_count
                    }
                }

            } case_error
        ]

        if {$case_status} {

            set result_reason \
                "case_analysis_failed: $case_error"

        } else {

            set report_error ""

            set report_status [
                catch {

                    report_checks \
                        -from $from_objects \
                        -to $to_objects \
                        -path_delay max \
                        -group_path_count 1 \
                        -endpoint_path_count 1 \
                        -sort_by_slack \
                        -format full_clock_expanded \
                        -fields {input_pin slew capacitance fanout net} \
                        -digits 4 \
                        -no_line_splits \
                        > $report_file

                } report_error
            ]

            if {$report_status} {

                set result_reason \
                    "report_checks_failed: $report_error"

            } else {

                set resolved true
                set result_reason "ok"
            }
        }
    }

    set cleanup_error ""

    set cleanup_status [
        catch {

            foreach ports $applied_case_ports {
                unset_case_analysis $ports
            }

        } cleanup_error
    ]

    if {$cleanup_status} {

        set resolved false

        set result_reason \
            "case_cleanup_failed: $cleanup_error"
    }

    set f [open $status_file w]

    puts $f \
        "{\"schema\":\"nebula_case_witness_result/1\",\"witness_id\":\"[nebula_json_escape $witness_id]\",\"branch\":\"[nebula_json_escape $branch]\",\"resolved\":$resolved,\"reason\":\"[nebula_json_escape $result_reason]\",\"from_signal\":\"[nebula_json_escape $from_signal]\",\"to_signal\":\"[nebula_json_escape $to_signal]\",\"from_boundary_count\":[llength $from_objects],\"to_boundary_count\":[llength $to_objects],\"case_count\":$case_count,\"report\":\"[nebula_json_escape $report_file]\"}"

    close $f

    puts \
        "$witness_id ($branch): $result_reason; cases=$case_count"
}

puts "nEBULA case-analysis witness reporting complete."
