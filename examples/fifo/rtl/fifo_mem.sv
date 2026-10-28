`include "fifo_defs.vh"
module fifo_mem import fifo_pkg::*; (
  input  logic                        clk,
  input  logic                        we,
  input  logic [`FIFO_DEPTH_LOG2-1:0] waddr,
  input  logic [`FIFO_DEPTH_LOG2-1:0] raddr,
  input  logic [WIDTH-1:0]            wdata,
  output logic [WIDTH-1:0]            rdata
);
  logic [WIDTH-1:0] mem [2**`FIFO_DEPTH_LOG2];
  always_ff @(posedge clk) if (we) mem[waddr] <= wdata;
  assign rdata = mem[raddr];
endmodule
