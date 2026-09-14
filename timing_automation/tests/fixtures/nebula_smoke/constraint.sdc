create_clock -name clk -period 2.000 [get_ports {clk}]
set_input_delay 0.200 -clock clk [get_ports {a[*] b[*] sel}]
set_output_delay 0.200 -clock clk [get_ports {y[*]}]
