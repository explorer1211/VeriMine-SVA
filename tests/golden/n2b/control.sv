// 控制完整性
module ctl_bug (input logic clk, input logic rst_n, input logic set_n, input logic [1:0] sel,
                input logic [1:0] req, input logic [3:0] a, output logic [3:0] y, output logic [1:0] g,
                output logic q);
  always_comb begin
    y = '0;
    unique case (sel)                                     // BUG: sel == 3 not listed
      2'd0: y = a;
      2'd1: y = a + 4'd1;
      2'd2: y = a - 4'd1;
    endcase
  end
  always_comb begin
    g = '0;
    unique casez (req)                                    // BUG: 2'b11 matches both items
      2'b1?: g = 2'b10;
      2'b?1: g = 2'b01;
    endcase
  end
  always_ff @(posedge clk or negedge rst_n or negedge set_n)   // BUG: set and reset together
    if (!rst_n) q <= 1'b0;
    else if (!set_n) q <= 1'b1;
    else q <= a[0];
endmodule

module ctl_ok (input logic [1:0] sel, input logic [1:0] req, input logic [3:0] a,
               output logic [3:0] y, output logic [1:0] g);
  always_comb begin
    y = '0;
    unique case (sel)
      2'd0: y = a;
      2'd1: y = a + 4'd1;
      2'd2: y = a - 4'd1;
      2'd3: y = a;
    endcase
  end
  always_comb begin
    g = '0;
    priority casez (req)
      2'b1?: g = 2'b10;
      2'b?1: g = 2'b01;
      default: g = '0;
    endcase
  end
endmodule
