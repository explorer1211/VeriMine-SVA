// N3a trace-mining sample; tb_ctl.sv drives it legally:
//   req_a / req_b never together, mode one-hot (or 0), start pulses last 3..5 cycles,
//   cfg = 2'b01 during reset, len only 2..10.
// LONG needs len > 12, so the range candidate (len <= 10) is over-strong; mode == 4'b0011 is a
// branch only illegal (non-one-hot) mode values reach.
module ctl (input logic clk, input logic rst_n, input logic req_a, input logic req_b,
            input logic [3:0] mode, input logic [3:0] len, input logic start, input logic [1:0] cfg,
            output logic gnt_a, output logic gnt_b, output logic [1:0] st_o, output logic dbg);
  localparam logic [1:0] IDLE = 2'd0, RUN = 2'd1, LONG = 2'd2;
  logic [1:0] st;
  assign gnt_a = req_a;
  assign gnt_b = req_b && !req_a;
  assign st_o = st;
  always_ff @(posedge clk or negedge rst_n)
    if (!rst_n) st <= IDLE;
    else case (st)
      IDLE: if (start) st <= RUN;
      RUN:  if (len > 4'd12) st <= LONG; else if (!start) st <= IDLE;
      LONG: st <= IDLE;
      default: st <= IDLE;
    endcase
  always_ff @(posedge clk or negedge rst_n)
    if (!rst_n) dbg <= 1'b0;
    else if (mode == 4'b0011) dbg <= cfg[0];
    else dbg <= 1'b0;
endmodule
