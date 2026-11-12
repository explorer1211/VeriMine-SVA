`timescale 1ns/1ps
module tb_src;
  reg clk = 0, rst_n = 0, go = 0, out_ready = 0;
  wire out_valid, waiting, five; wire [7:0] out_data;
  integer t, i, seed;
  src u_src (.clk(clk), .rst_n(rst_n), .go(go), .out_ready(out_ready), .out_valid(out_valid),
             .out_data(out_data), .waiting(waiting), .five(five));
  always #5 clk = ~clk;
  initial begin
    seed = 11;
    for (t = 0; t < 6; t = t + 1) begin          // six tests, reset in between: six cases
      rst_n = 0; repeat (2) @(negedge clk); rst_n = 1;
      for (i = 0; i < 60; i = i + 1) begin
        @(negedge clk);
        go = ($random(seed) & 3) != 0;
        out_ready = ($random(seed) & 1);
      end
    end
    $finish;
  end
endmodule
