// A user testbench for examples/fifo: two directed tests separated by a reset.
`timescale 1ns/1ps
module tb_fifo;
  logic clk = 1'b0, rst_n = 1'b0, push = 1'b0, pop = 1'b0;
  logic [7:0] wdata = '0, rdata;
  logic full, empty;
  int test = 0;
  fifo_top u_dut (.clk, .rst_n, .push, .pop, .wdata, .rdata, .full, .empty);
  always #5 clk = ~clk;

  task automatic reset();
    rst_n = 1'b0; push = 1'b0; pop = 1'b0;
    repeat (2) @(negedge clk);
    rst_n = 1'b1;
  endtask

  initial begin
    if (!$value$plusargs("test=%d", test)) test = 0;
    // test 1: a few writes, then reads
    reset();
    repeat (5) begin @(negedge clk); push = 1'b1; wdata = wdata + 8'd1; end
    @(negedge clk); push = 1'b0;
    repeat (3) begin @(negedge clk); pop = 1'b1; end
    @(negedge clk); pop = 1'b0;
    // test 2: fill to full (only with +test=1)
    if (test == 1) begin
      reset();
      while (!full) begin @(negedge clk); push = 1'b1; wdata = wdata + 8'd3; end
      @(negedge clk); push = 1'b0;
      repeat (2) @(negedge clk);
    end
    $finish;
  end
endmodule
