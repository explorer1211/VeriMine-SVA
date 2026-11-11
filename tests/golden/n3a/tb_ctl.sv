`timescale 1ns/1ps
module tb_ctl;
  reg clk = 0, rst_n = 0, req_a = 0, req_b = 0, start = 0;
  reg [3:0] mode = 0, len = 4'd2;
  reg [1:0] cfg = 2'b01;
  wire gnt_a, gnt_b, dbg;
  wire [1:0] st_o;
  integer i, k, seed;
  ctl u_ctl (.clk(clk), .rst_n(rst_n), .req_a(req_a), .req_b(req_b), .mode(mode), .len(len),
             .start(start), .cfg(cfg), .gnt_a(gnt_a), .gnt_b(gnt_b), .st_o(st_o), .dbg(dbg));
  always #5 clk = ~clk;
  initial begin
    seed = 7;
    repeat (3) @(negedge clk);
    rst_n = 1;
    for (i = 0; i < 40; i = i + 1) begin
      // a start pulse of 3..5 cycles, with requests and modes changing meanwhile
      k = 3 + (($random(seed) & 32'h7fffffff) % 3);
      start = 1;
      repeat (k) begin
        @(negedge clk);
        case (($random(seed) & 32'h7fffffff) % 3)
          0: begin req_a = 1; req_b = 0; end
          1: begin req_a = 0; req_b = 1; end
          default: begin req_a = 0; req_b = 0; end
        endcase
        mode = (($random(seed) & 1) ? (4'b0001 << (($random(seed) & 32'h7fffffff) % 4)) : 4'b0000);
        len = 4'd2 + (($random(seed) & 32'h7fffffff) % 9);
      end
      start = 0;
      repeat (1 + (($random(seed) & 32'h7fffffff) % 3)) @(negedge clk);
    end
    $finish;
  end
endmodule
