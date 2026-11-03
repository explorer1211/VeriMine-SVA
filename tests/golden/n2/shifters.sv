module shifters (
  input  logic       clk, rst_n, sin, en,
  output logic [7:0] sr_left, sr_right, sr_op,
  output logic [7:0] lfsr,
  output logic       sout
);
  always_ff @(posedge clk) if (en) sr_left <= {sr_left[6:0], sin};
  always_ff @(posedge clk) sr_right <= {sin, sr_right[7:1]};
  always_ff @(posedge clk) sr_op <= sr_op << 1;
  always_ff @(posedge clk or negedge rst_n)
    if (!rst_n) lfsr <= 8'h01;
    else lfsr <= {lfsr[6:0], lfsr[7] ^ lfsr[5] ^ lfsr[4] ^ lfsr[3]};
  assign sout = sr_right[0];
endmodule
