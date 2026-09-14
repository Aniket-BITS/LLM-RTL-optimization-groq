// BAD candidate (interface): output y narrowed to [15:0].
// The port list no longer matches the golden -> lint MUST catch it
// (ports_equal = false), before any synth/SEC cycle is spent.
module mapper_reconvergent_test (
    input clk,
    input [31:0] a,
    input [31:0] b,
    input [31:0] c,
    input sel,
    output reg [15:0] y          // <-- width changed from [31:0]
);

    reg [31:0] a_reg;
    reg [31:0] b_reg;
    reg [31:0] c_reg;
    reg sel_reg;

    wire [31:0] add_path;
    wire [31:0] xor_path;
    wire [31:0] result;

    assign add_path = a_reg + b_reg;
    assign xor_path = a_reg ^ c_reg;
    assign result   = sel_reg ? add_path : xor_path;

    always @(posedge clk) begin
        a_reg   <= a;
        b_reg   <= b;
        c_reg   <= c;
        sel_reg <= sel;
        y       <= result[15:0];
    end

endmodule
