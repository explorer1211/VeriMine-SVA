module wave_top(input logic clk, input logic rst_n, input logic en, output logic [3:0] q);
  for (genvar i = 0; i < 1; i++) begin : g
    logic t;
    always_ff @(posedge clk) t <= q[0];
  end
  always_ff @(posedge clk or negedge rst_n)
    if (!rst_n) q <= '0;
    else if (en) q <= q + 4'd1;
endmodule
