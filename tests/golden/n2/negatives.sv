// Things that look like states or counters but are not.
module negatives (
  input  logic       clk, rst_n,
  input  logic [7:0] din,
  output logic       hit,
  output logic [7:0] acc,
  output logic [7:0] d2
);
  logic [7:0] data_q, d1;
  logic [7:0] state_like_name;
  always_ff @(posedge clk) data_q <= din;              // data register compared below
  always_ff @(posedge clk) hit <= (data_q == 8'hff);
  always_ff @(posedge clk or negedge rst_n)
    if (!rst_n) acc <= '0; else acc <= acc + din;      // accumulator, not a counter
  always_ff @(posedge clk) begin d1 <= din; d2 <= d1; end  // delay line, not a shift register
  always_ff @(posedge clk) state_like_name <= din ^ data_q;  // named like a state, is not
endmodule
