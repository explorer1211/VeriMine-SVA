// X 与驱动
module xd_bug (input logic [1:0] sel, input logic [3:0] a, input logic ea, input logic eb,
               input logic da, input logic db,
               output logic [3:0] y, output tri bus, output logic dangling, output logic z);
  logic unused_drv;
  always_comb begin
    case (sel)
      2'd0: y = a;
      2'd1: y = ~a;
      default: y = 'x;                                    // BUG: X assignment
    endcase
  end
  assign bus = ea ? da : 1'bz;                            // BUG: both enables can be on
  assign bus = eb ? db : 1'bz;
  assign z = unused_drv & a[0];                           // BUG: unused_drv has no driver
  // BUG: output dangling is never driven
endmodule

module xd_ok (input logic [1:0] sel, input logic [3:0] a, input logic ea, input logic da, input logic db,
              output logic [3:0] y, output tri bus);
  always_comb begin
    case (sel)
      2'd0: y = a;
      2'd1: y = ~a;
      default: y = '0;
    endcase
  end
  assign bus = ea ? da : 1'bz;
  assign bus = !ea ? db : 1'bz;
endmodule
