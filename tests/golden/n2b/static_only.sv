// Defects the open-source formal engine cannot even read (combinational loops, multiple
// drivers): checked statically only, in a design of their own.
module tm_bug (input logic x, input logic y, input logic en, input logic d, output logic o, output logic l);
  logic p, r;
  assign p = r & x;                                       // BUG: p -> r -> p
  assign r = p | y;
  assign o = r;
  always_comb if (en) l = d;                              // BUG: latch
endmodule

module md_bug (input logic clk, input logic a, input logic b, output logic q);
  always @(posedge clk) q <= a;                           // BUG: two blocks drive q
  always @(posedge clk) q <= b;
endmodule

module tm_ok (input logic x, input logic y, input logic en, input logic d, output logic o, output logic l);
  logic p, r;
  assign p = x & y;
  assign r = p | y;
  assign o = r;
  always_comb l = en ? d : 1'b0;
endmodule

module static_top (input logic clk, input logic x, input logic y, input logic en, input logic d,
                   input logic a, input logic b, output logic [5:0] o);
  tm_bug u_tm_bug (.x, .y, .en, .d, .o(o[0]), .l(o[1]));
  md_bug u_md_bug (.clk, .a, .b, .q(o[2]));
  tm_ok  u_tm_ok  (.x, .y, .en, .d, .o(o[3]), .l(o[4]));
  assign o[5] = 1'b0;
endmodule
