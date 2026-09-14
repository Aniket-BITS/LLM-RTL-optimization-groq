# Required environment: NEBULA_ODB and NEBULA_GEOMETRY_JSON.
read_db $::env(NEBULA_ODB)
set block [ord::get_db_block]
set units [$block getDbUnitsPerMicron]
set fields {}
foreach {name rect} [list die_area_um [$block getDieArea] core_area_um [$block getCoreArea]] {
  set coords {}
  foreach method {xMin yMin xMax yMax} {lappend coords [expr {double([$rect $method]) / $units}]}
  lappend fields "\"$name\": \[[join $coords {, }]\]"
}
lappend fields "\"instance_count\": [llength [$block getInsts]]"
lappend fields "\"db_units_per_micron\": $units"
set out [open $::env(NEBULA_GEOMETRY_JSON) w]
puts $out "{[join $fields {, }]}"
close $out
exit
