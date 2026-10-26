`include "fifo_defs.vh"
module fifo_ctrl (
  input  logic                        clk,
  input  logic                        rst_n,
  input  logic                        push,
  input  logic                        pop,
  output logic                        full,
  output logic                        empty,
  output logic [`FIFO_DEPTH_LOG2-1:0] waddr,
  output logic [`FIFO_DEPTH_LOG2-1:0] raddr
);
  logic [`FIFO_DEPTH_LOG2:0] count;
  assign full  = count == 2**`FIFO_DEPTH_LOG2;
  assign empty = count == 0;
  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      count <= '0; waddr <= '0; raddr <= '0;
    end else begin
      if (push && !full) waddr <= waddr + 1'b1;
      if (pop && !empty) raddr <= raddr + 1'b1;
      count <= count + (push && !full) - (pop && !empty);
    end
  end
endmodule
