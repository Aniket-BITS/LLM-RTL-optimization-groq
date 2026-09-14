// BAD candidate (functional): add_path is off by one (a_reg + b_reg + 1).
// Ports identical, one module, elaborates cleanly -> lint PASS.
// But it computes a different circuit -> SEC MUST catch it (NONEQUIVALENT).
// This is the case that proves SEC, not lint, is the correctness guard.
module mapper_reconvergent_test (
    input clk,
    input [31:0] a,
    input [31:0] b,
    input [31:0] c,
    input sel,
    output reg [31:0] y
);

    reg [31:0] a_reg;
    reg [31:0] b_reg;
    reg [31:0] c_reg;
    reg sel_reg;

    wire [31:0] add_path;
    wire [31:0] xor_path;
    wire [31:0] result;

    assign add_path = a_reg + b_reg + 32'd1;   // <-- functional bug
    assign xor_path = a_reg ^ c_reg;
    assign result   = sel_reg ? add_path : xor_path;

    always @(posedge clk) begin
        a_reg   <= a;
        b_reg   <= b;
        c_reg   <= c;
        sel_reg <= sel;
        y       <= result;
    end

endmodule
