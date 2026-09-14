// GOOD candidate for mapper_reconvergent_test.
// add_path (a_reg + b_reg) is restructured as a 4-block carry-select adder.
// Functionally identical, same cycle latency, identical port list.
// Expected: lint PASS, SEC PASS.
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

    // Block 0: bits [7:0]
    wire [8:0] s0   = a_reg[7:0]   + b_reg[7:0];
    wire       c0   = s0[8];
    // Block 1: bits [15:8], precomputed for Cin = 0 and Cin = 1
    wire [8:0] s1_0 = a_reg[15:8]  + b_reg[15:8];
    wire [8:0] s1_1 = a_reg[15:8]  + b_reg[15:8]  + 1'b1;
    wire       c1   = c0 ? s1_1[8] : s1_0[8];
    // Block 2: bits [23:16]
    wire [8:0] s2_0 = a_reg[23:16] + b_reg[23:16];
    wire [8:0] s2_1 = a_reg[23:16] + b_reg[23:16] + 1'b1;
    wire       c2   = c1 ? s2_1[8] : s2_0[8];
    // Block 3: bits [31:24]
    wire [8:0] s3_0 = a_reg[31:24] + b_reg[31:24];
    wire [8:0] s3_1 = a_reg[31:24] + b_reg[31:24] + 1'b1;

    assign add_path[7:0]   = s0[7:0];
    assign add_path[15:8]  = c0 ? s1_1[7:0] : s1_0[7:0];
    assign add_path[23:16] = c1 ? s2_1[7:0] : s2_0[7:0];
    assign add_path[31:24] = c2 ? s3_1[7:0] : s3_0[7:0];

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
