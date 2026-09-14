module nebula_smoke (
    input  wire       clk,
    input  wire [7:0] a,
    input  wire [7:0] b,
    input  wire       sel,
    output wire [8:0] y
);

// Reconvergent cone: a/b feed both arithmetic and XOR branches.
wire [8:0] add_full;
wire [8:0] xor_path;
wire [8:0] selected;

assign add_full = {1'b0, a} + {1'b0, b};
assign xor_path = {1'b0, (a ^ b)};
assign selected = sel ? add_full : xor_path;
assign y = selected;

// clk is intentionally present only so the fixture can be constrained by ORFS.
wire _unused_clk = clk;

endmodule
