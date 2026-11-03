// Two-process FSM: enum state, combinational next state, case on state.
module fsm_case (
  input  logic clk, rst_n, start, ack, abort,
  output logic busy, done
);
  typedef enum logic [1:0] {IDLE, REQ, WAIT, DONE} st_t;
  st_t state, state_nxt;
  always_ff @(posedge clk or negedge rst_n)
    if (!rst_n) state <= IDLE;
    else        state <= state_nxt;
  always_comb begin
    state_nxt = state;
    unique case (state)
      IDLE: if (start) state_nxt = REQ;
      REQ:  state_nxt = WAIT;
      WAIT: if (abort) state_nxt = IDLE; else if (ack) state_nxt = DONE;
      DONE: state_nxt = IDLE;
    endcase
  end
  assign busy = (state != IDLE);
  assign done = (state == DONE);
endmodule
