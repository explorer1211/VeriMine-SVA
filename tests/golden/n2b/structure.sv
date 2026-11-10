// 已识别结构：计数器、FIFO
module cnt_bug (input logic clk, input logic rst_n, input logic ev, output logic [3:0] events);
  always_ff @(posedge clk or negedge rst_n)
    if (!rst_n) events <= '0;
    else if (ev) events <= events + 4'd1;                 // BUG: wraps 15 -> 0, should saturate
endmodule

module cnt_ok (input logic clk, input logic rst_n, input logic ev, output logic [3:0] events);
  always_ff @(posedge clk or negedge rst_n)
    if (!rst_n) events <= '0;
    else if (ev && events != 4'd15) events <= events + 4'd1;
endmodule

module fifo_bug (input logic clk, input logic rst_n, input logic push, input logic pop,
                 input logic [7:0] din, output logic [7:0] dout, output logic full, output logic empty);
  logic [7:0] mem [4];
  logic [1:0] wp, rp;
  logic [2:0] count;
  assign full  = count == 3'd4;
  assign empty = count == 3'd0;
  always_ff @(posedge clk or negedge rst_n)
    if (!rst_n) begin
      wp <= '0; rp <= '0; count <= '0;
    end else begin
      if (push) wp <= wp + 2'd1;                          // BUG: no !full guard
      if (pop && !empty) rp <= rp + 2'd1;
      count <= count + push - (pop && !empty);            // BUG: push when full overflows
    end
  always_ff @(posedge clk) if (push) mem[wp] <= din;
  assign dout = mem[rp];
endmodule

module fifo_ok (input logic clk, input logic rst_n, input logic push, input logic pop,
                input logic [7:0] din, output logic [7:0] dout, output logic full, output logic empty);
  logic [7:0] mem [4];
  logic [1:0] wp, rp;
  logic [2:0] count;
  assign full  = count == 3'd4;
  assign empty = count == 3'd0;
  always_ff @(posedge clk or negedge rst_n)
    if (!rst_n) begin
      wp <= '0; rp <= '0; count <= '0;
    end else begin
      if (push && !full) wp <= wp + 2'd1;
      if (pop && !empty) rp <= rp + 2'd1;
      count <= count + (push && !full) - (pop && !empty);
    end
  always_ff @(posedge clk) if (push && !full) mem[wp] <= din;
  assign dout = mem[rp];
endmodule
