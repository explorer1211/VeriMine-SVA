// N3a handshake sample. in_ready rises one cycle after in_valid is seen; the protocol says
// in_valid stays up until in_ready. `level` counts started handshakes minus finished ones: with
// the protocol it never exceeds 1, but if in_valid drops while waiting it only grows, and N2b's
// counter.overflow assertion fails on that illegal input (an env_missing counterexample).
module hs_rx (input logic clk, input logic rst_n, input logic in_valid, input logic [7:0] in_data,
              output logic in_ready, output logic [7:0] last);
  localparam logic [1:0] IDLE = 2'd0, SEEN = 2'd1;
  logic [1:0] st;
  logic [2:0] level;
  assign in_ready = st == SEEN;
  always_ff @(posedge clk or negedge rst_n)
    if (!rst_n) begin
      st <= IDLE; level <= '0; last <= '0;
    end else begin
      case (st)
        IDLE: if (in_valid) begin st <= SEEN; level <= level + 3'd1; end
        SEEN: begin
          st <= IDLE;
          if (in_valid) begin level <= level - 3'd1; last <= in_data; end
        end
        default: st <= IDLE;
      endcase
    end
endmodule
