// 状态机
module fsm_bug (input logic clk, input logic rst_n, input logic go, input logic fin, input logic err,
                output logic busy);
  localparam logic [2:0] IDLE = 3'd0, RUN = 3'd1, DONE = 3'd2, HANG = 3'd3, LOST = 3'd4;
  logic [2:0] st;
  always_ff @(posedge clk or negedge rst_n)
    if (!rst_n) st <= IDLE;
    else case (st)
      IDLE: if (go) st <= RUN;
      RUN:  if (fin) st <= DONE; else if (err) st <= HANG;
      DONE: st <= 3'd6;        // BUG: undefined encoding, no branch handles it
      HANG: st <= HANG;        // BUG: no way out
      LOST: st <= IDLE;        // BUG: nothing goes to LOST
    endcase
  assign busy = st != IDLE;
endmodule

module fsm_ok (input logic clk, input logic rst_n, input logic go, input logic fin,
               output logic busy);
  typedef enum logic [1:0] {S_IDLE, S_RUN, S_DONE} st_t;
  st_t st, nxt;
  always_ff @(posedge clk or negedge rst_n)
    if (!rst_n) st <= S_IDLE; else st <= nxt;
  always_comb begin
    nxt = st;
    case (st)
      S_IDLE: if (go) nxt = S_RUN;
      S_RUN:  if (fin) nxt = S_DONE;
      S_DONE: nxt = S_IDLE;
      default: nxt = S_IDLE;
    endcase
  end
  assign busy = st != S_IDLE;
endmodule
