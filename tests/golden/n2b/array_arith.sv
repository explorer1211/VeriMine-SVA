// 数组与算术
module arr_bug (input logic clk, input logic we, input logic [3:0] waddr, input logic [7:0] wdata,
                input logic [7:0] a, input logic [7:0] b, input logic [7:0] wide,
                output logic [7:0] sum, output logic [3:0] narrow, output logic [7:0] q);
  logic [7:0] mem [0:9];
  always_ff @(posedge clk) if (we) mem[waddr] <= wdata;   // BUG: waddr can be 10..15
  assign q = mem[0];
  assign sum = a + b;                                     // BUG: carry lost
  assign narrow = wide;                                   // BUG: truncated
endmodule

module arr_ok (input logic clk, input logic we, input logic [3:0] waddr, input logic [7:0] wdata,
               input logic [7:0] a, input logic [7:0] b,
               output logic [8:0] sum, output logic [7:0] q);
  logic [7:0] mem [0:15];
  always_ff @(posedge clk) if (we) mem[waddr] <= wdata;
  assign q = mem[0];
  assign sum = a + b;
endmodule
