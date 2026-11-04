module counters #(parameter int MAX = 9) (
  input  logic       clk, rst_n, en, load, up, dn,
  input  logic [7:0] load_val,
  output logic [3:0] wrap_cnt,
  output logic [7:0] down_cnt,
  output logic [5:0] ud_cnt,
  output logic [15:0] cycles
);
  always_ff @(posedge clk or negedge rst_n)
    if (!rst_n) wrap_cnt <= '0;
    else if (en) wrap_cnt <= (wrap_cnt == MAX) ? '0 : wrap_cnt + 1'b1;
  always_ff @(posedge clk)
    if (load) down_cnt <= load_val;
    else if (down_cnt != 0) down_cnt <= down_cnt - 8'd1;
  always_ff @(posedge clk or negedge rst_n)
    if (!rst_n) ud_cnt <= '0;
    else ud_cnt <= ud_cnt + up - dn;
  always_ff @(posedge clk) cycles <= cycles + 16'd1;
endmodule
