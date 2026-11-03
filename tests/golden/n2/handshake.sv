// valid/ready register slice and a req/ack requester.
module handshake (
  input  logic        clk, rst_n,
  input  logic        in_valid,
  output logic        in_ready,
  input  logic [31:0] in_data,
  output logic        out_valid,
  input  logic        out_ready,
  output logic [31:0] out_data,
  output logic        bus_req,
  input  logic        bus_ack,
  input  logic        kick
);
  assign in_ready = !out_valid || out_ready;
  always_ff @(posedge clk or negedge rst_n)
    if (!rst_n) out_valid <= 1'b0;
    else if (in_valid && in_ready) out_valid <= 1'b1;
    else if (out_ready) out_valid <= 1'b0;
  always_ff @(posedge clk)
    if (in_valid && in_ready) out_data <= in_data;
  always_ff @(posedge clk or negedge rst_n)
    if (!rst_n) bus_req <= 1'b0;
    else if (kick) bus_req <= 1'b1;
    else if (bus_req && bus_ack) bus_req <= 1'b0;
endmodule
