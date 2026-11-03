module golden_top (
  input  logic clk, rst_n, rst
);
  fsm_case   u_fsm_case  (.clk, .rst_n, .start(1'b0), .ack(1'b0), .abort(1'b0), .busy(), .done());
  fsm_if     u_fsm_if    (.clk, .rst, .go(1'b0), .stop(1'b0), .phase_o());
  counters   u_counters  (.clk, .rst_n, .en(1'b1), .load(1'b0), .up(1'b0), .dn(1'b0), .load_val(8'd0),
                          .wrap_cnt(), .down_cnt(), .ud_cnt(), .cycles());
  shifters   u_shifters  (.clk, .rst_n, .sin(1'b0), .en(1'b1), .sr_left(), .sr_right(), .sr_op(), .lfsr(), .sout());
  handshake  u_handshake (.clk, .rst_n, .in_valid(1'b0), .in_ready(), .in_data('0), .out_valid(),
                          .out_ready(1'b0), .out_data(), .bus_req(), .bus_ack(1'b0), .kick(1'b0));
  negatives  u_negatives (.clk, .rst_n, .din('0), .hit(), .acc(), .d2());
endmodule
