// N3 formal-exploration sample: OPEN needs key == 16'hBEEF (random stimulus practically never
// hits it); ORPHAN needs req_a && req_b, which the assume one_req forbids.
module top(input logic clk, input logic rst_n, input logic start, input logic [15:0] key,
           input logic req_a, input logic req_b, output logic [1:0] st_o, output logic unlocked);
  localparam logic [1:0] IDLE = 2'd0, ARMED = 2'd1, OPEN = 2'd2, ORPHAN = 2'd3;
  logic [1:0] st;
  always_ff @(posedge clk or negedge rst_n)
    if (!rst_n) st <= IDLE;
    else case (st)
      IDLE:   if (start) st <= ARMED;
      ARMED:  if (key == 16'hBEEF) st <= OPEN; else if (!start) st <= IDLE;
      OPEN:   if (req_a && req_b) st <= ORPHAN; else st <= IDLE;   // never: assume says one-hot reqs
      ORPHAN: st <= IDLE;
    endcase
  assign st_o = st;
  assign unlocked = st == OPEN;
endmodule
