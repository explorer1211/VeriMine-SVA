// 寄存器检查（配合 regs.csv）
module regs (input logic clk, input logic rst_n, input logic we, input logic [7:0] addr,
             input logic [7:0] wdata, input logic [7:0] hw_status, output logic [7:0] rdata);
  logic [7:0] ctrl, status, data, spare;
  always_ff @(posedge clk or negedge rst_n)
    if (!rst_n) begin
      ctrl <= 8'h00;                                      // BUG: map says 0x01
      status <= 8'h00; data <= 8'h00; spare <= 8'h00;
    end else begin
      status <= hw_status;
      if (we && addr == 8'h00) ctrl <= wdata;
      if (we && addr == 8'h04) status <= wdata;           // BUG: STATUS is read-only
      if (we && addr == 8'h08) data <= wdata;
      if (we && addr == 8'h0C) spare <= wdata;            // BUG: 0x0C not in the map
    end
  always_comb
    case (addr)
      8'h00: rdata = ctrl;
      8'h04: rdata = status;
      8'h08: rdata = data;
      default: rdata = spare;
    endcase
endmodule
