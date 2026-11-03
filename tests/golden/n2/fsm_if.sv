// One-process FSM with localparam encodings and if/else comparisons, plus a timeout counter.
module fsm_if (
  input  logic clk, rst, go, stop,
  output logic [2:0] phase_o
);
  localparam logic [2:0] S_IDLE = 3'd0, S_RUN = 3'd1, S_HOLD = 3'd2, S_END = 3'd4;
  logic [2:0] phase;
  logic [7:0] timer;
  always_ff @(posedge clk) begin
    if (rst) begin
      phase <= S_IDLE;
      timer <= 8'd0;
    end else begin
      if (phase == S_IDLE) begin
        if (go) phase <= S_RUN;
      end else if (phase == S_RUN) begin
        timer <= timer + 8'd1;
        if (stop) phase <= S_HOLD;
        else if (timer == 8'hff) phase <= S_END;
      end else if (phase == S_HOLD) begin
        if (go) phase <= S_RUN;
      end else begin
        phase <= S_IDLE;
        timer <= 8'd0;
      end
    end
  end
  assign phase_o = phase;
endmodule
