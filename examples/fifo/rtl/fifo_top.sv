module fifo_top import fifo_pkg::*; (
  input  logic             clk,
  input  logic             rst_n,
  input  logic             push,
  input  logic             pop,
  input  logic [WIDTH-1:0] wdata,
  output logic [WIDTH-1:0] rdata,
  output logic             full,
  output logic             empty
);
  logic [`FIFO_DEPTH_LOG2-1:0] waddr, raddr;
  fifo_ctrl u_ctrl (.clk, .rst_n, .push, .pop, .full, .empty, .waddr, .raddr);
  fifo_mem  u_mem  (.clk, .we(push && !full), .waddr, .raddr, .wdata, .rdata);
endmodule
