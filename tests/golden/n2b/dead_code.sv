// 死代码与不可达分支
module dc_bug (input logic clk, input logic rst_n, input logic en,
               output logic [3:0] cnt, output logic flag, output logic dbg);
  localparam bit DEBUG = 1'b0;
  always_ff @(posedge clk or negedge rst_n)
    if (!rst_n) cnt <= '0;
    else if (en && cnt != 4'd5) cnt <= cnt + 4'd1;     // saturates at 5
  always_ff @(posedge clk or negedge rst_n)
    if (!rst_n) flag <= 1'b0;
    else if (cnt == 4'd9) flag <= 1'b1;                // BUG: cnt never reaches 9
  always_ff @(posedge clk)
    if (DEBUG) dbg <= en;                              // BUG: condition is constant false
    else dbg <= 1'b0;
endmodule

module dc_ok (input logic clk, input logic rst_n, input logic en,
              output logic [3:0] cnt, output logic flag);
  always_ff @(posedge clk or negedge rst_n)
    if (!rst_n) cnt <= '0;
    else if (en && cnt != 4'd5) cnt <= cnt + 4'd1;
  always_ff @(posedge clk or negedge rst_n)
    if (!rst_n) flag <= 1'b0;
    else if (cnt == 4'd5) flag <= 1'b1;
endmodule
