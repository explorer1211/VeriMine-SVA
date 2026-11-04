// Formal-friendly golden set: every module instantiated once under golden_top.
module golden_top (input logic clk, input logic rst_n, input logic set_n, input logic en, input logic go,
                   input logic fin, input logic err, input logic we, input logic [3:0] waddr,
                   input logic [7:0] d8, input logic [7:0] e8, input logic [1:0] sel, input logic [1:0] req,
                   input logic [3:0] a4, input logic ea, input logic eb, input logic da, input logic db,
                   input logic push, input logic pop, input logic [7:0] addr,
                   output logic [47:0] o8, output logic [43:0] o4, output logic [11:0] o1,
                   output tri bus_bug, output tri bus_ok, output logic [8:0] sum9);
  dc_bug  u_dc_bug  (.clk, .rst_n, .en, .cnt(o4[3:0]), .flag(o1[0]), .dbg(o1[1]));
  dc_ok   u_dc_ok   (.clk, .rst_n, .en, .cnt(o4[7:4]), .flag(o1[2]));
  fsm_bug u_fsm_bug (.clk, .rst_n, .go, .fin, .err, .busy(o1[3]));
  fsm_ok  u_fsm_ok  (.clk, .rst_n, .go, .fin, .busy(o1[4]));
  arr_bug u_arr_bug (.clk, .we, .waddr, .wdata(d8), .a(d8), .b(e8), .wide(e8), .sum(o8[7:0]),
                     .narrow(o4[11:8]), .q(o8[15:8]));
  arr_ok  u_arr_ok  (.clk, .we, .waddr, .wdata(d8), .a(d8), .b(e8), .sum(sum9), .q(o8[23:16]));
  xd_bug  u_xd_bug  (.sel, .a(a4), .ea, .eb, .da, .db, .y(o4[15:12]), .bus(bus_bug), .dangling(o1[5]), .z(o1[6]));
  xd_ok   u_xd_ok   (.sel, .a(a4), .ea, .da, .db, .y(o4[19:16]), .bus(bus_ok));
  ctl_bug u_ctl_bug (.clk, .rst_n, .set_n, .sel, .req, .a(a4), .y(o4[23:20]), .g(o4[25:24]), .q(o1[7]));
  ctl_ok  u_ctl_ok  (.sel, .req, .a(a4), .y(o4[29:26]), .g(o4[31:30]));
  cnt_bug u_cnt_bug (.clk, .rst_n, .ev(en), .events(o4[35:32]));
  cnt_ok  u_cnt_ok  (.clk, .rst_n, .ev(en), .events(o4[39:36]));
  fifo_bug u_fifo_bug (.clk, .rst_n, .push, .pop, .din(d8), .dout(o8[31:24]), .full(o1[8]), .empty(o1[9]));
  fifo_ok  u_fifo_ok  (.clk, .rst_n, .push, .pop, .din(d8), .dout(o8[39:32]), .full(o1[10]), .empty(o1[11]));
  regs    u_regs    (.clk, .rst_n, .we, .addr, .wdata(d8), .hw_status(e8), .rdata(o8[47:40]));
  assign o4[43:40] = '0;
endmodule
