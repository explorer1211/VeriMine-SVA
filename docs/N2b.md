# N2b 结构性检查

输入：N1 的结构索引、N2 的行为模型、slang 的分析诊断，有寄存器表时再加寄存器表。
输出：

- 结构性检查报告 `n2b_report.md`，内容同 `n2b.json`
- 交付版断言 `n2b_bind.sv`：标准 SVA，用 bind 挂到实例上
- 每条断言和 cover 的验证状态
- 候选 RTL 缺陷清单，交给 N8

不需要轨迹，也不需要 LLM。

```bash
verimine n2b examples/fifo/verimine.yaml              # 先跑 N0、N1、N2，再检查、验证、出报告
verimine n2b examples/fifo/verimine.yaml --no-formal  # 只出静态结论和交付版断言
verimine n2b examples/fifo/verimine.yaml --depth 30 --dry-run --json
```

形式验证部分需要 `yosys`、`sby`、`yosys-smtbmc` 和一个 SMT 求解器（bitwuzla、boolector、yices、z3 中任意一个）。缺任何一个都只跑静态检查，属性状态记为 `NOT_RUN`。

## 步骤与代码位置

| 步骤 | 内容 | 代码 |
|---|---|---|
| 1 | 按检查类套模板：静态结论直接给出，其余生成断言和 cover | `n2b/checks.py`、`n2b/regmap.py` |
| 1a | 渲染两份：交付版（标准 SVA）和验证版（Yosys 能读的立即断言） | `n2b/render.py` |
| 2 | 送入验证：本阶段内置一个最小引擎（Yosys + SymbiYosys），N6、N7 建好后由它们接管 | `n2b/engine.py` |
| 3 | 失败的断言标为候选 RTL 缺陷，报告里排在最前，交给 N8 判根因 | `n2b/pipeline.py: _merge` |
| 4 | 输出独立的结构性检查报告 | `n2b/report.py` |
| — | 交付版用 slang 编译一遍（N6 第 1 步） | `n2b/pipeline.py: _check_delivery` |
| — | 落库，写入输入 hash | `n2b/store.py` |

## 检查类与模板

“静态”指直接从 N1 索引、N2 模型或 slang 分析得出结论；“断言”“cover”指生成属性，交给引擎判定。

| 检查类 | 检查 | 方法 |
|---|---|---|
| 死代码与不可达分支 | `dead.const_cond` | 静态：if 条件能折成常量（如 `if (DEBUG)` 且 `DEBUG = 0`） |
| | `dead.case_unreachable` | 静态：slang CaseUnreachable |
| | `dead.branch` | cover：每个带赋值的分支一条。到不了就是死代码。`default` 分支不检查，因为它常是防御性写法，本来就不该被走到 |
| 状态机 | `fsm.unreachable_state` | 静态：沿 N2 迁移图从复位状态出发不可达；cover：每个状态一条 `state == S` |
| | `fsm.deadlock` | 静态：除了自环没有出口；cover：每个状态一条 `$past(state) == S && state != S` |
| | `fsm.illegal_encoding` | 断言：状态只取已知编码（编码空间比状态多时才生成） |
| | `fsm.dangling` | 静态：迁移到一个没有任何分支处理的编码 |
| 数组与算术 | `array.const_oob` | 静态：slang IndexOOB / RangeOOB（常量下标） |
| | `array.index_oob` | 断言：变量下标落在数组或向量的范围内。下标位宽本身就不可能越界时不生成 |
| | `array.overflow` | 断言：两个信号相加或相减的结果放得进目标位宽。加减常量的情况属于有意的模运算或计数器，不检查 |
| | `array.width_trunc` | 静态：slang WidthTruncate / PortWidthTruncate |
| X 与驱动 | `x.assign` | 静态：赋了 x |
| | `x.multi_driver` | 静态：slang 的多驱动诊断，再加上索引里按位比较驱动范围，驱动的位有重叠才报 |
| | `x.bus_conflict` | 断言：同一三态总线（`en ? d : 'z`）的使能两两互斥 |
| | `x.floating` | 静态：输出没有驱动；信号被读但没有驱动；子实例的输入端口未连接 |
| 控制完整性 | `control.case_incomplete` | 静态：slang CaseIncomplete / CaseEnum；断言：`unique` / `priority` case 的选择值总在分支项中 |
| | `control.case_parallel` | 静态：slang CaseDup / CaseOverlap，`priority case` 的重叠除外，因为重叠就是 priority 的本意；断言：`unique` / `unique0` 的通配 case 至多匹配一项 |
| | `control.set_reset` | 断言：同一过程块的两个异步控制（复位和置位）不同时有效 |
| 时序风险 | `timing.comb_loop` | 静态：VDG 里零延迟边构成的环（Tarjan 强连通分量） |
| | `timing.latch` | 静态：slang InferredLatch；`always_latch` |
| 已识别结构 | `counter.overflow` / `counter.underflow` | 断言（N2 识别出的计数器）：`r + 增量 <= 最大值 + 减量`、`r + 增量 >= 减量`，比较在 32 位下进行，不会回绕。两类计数器不检查：给存储器作地址的指针（回绕是预期行为），以及没有条件、也不和常量比较的自由运行计数器 |
| | `fifo.overflow` / `fifo.underflow` | 断言（N2 识别出 push/full、pop/empty 和占用计数器的实例）：满时只写不读，下一拍占用计数和写指针都不变；空时只读不写同理 |
| | `fifo.bound` | 断言：占用计数不超过 full 的阈值（从 `full = count == K` 读出 K） |
| | `fifo.reachable` | cover：full 和 empty 都能到达 |
| 寄存器检查 | `reg.address` | 静态：两个寄存器地址相同或范围重叠 |
| | `reg.decode` | 静态（需要 `registers.addr`）：表里每个地址都在 RTL 中被地址信号比较过；RTL 也没有译码表里没有的地址 |
| | `reg.reset` | 静态：RTL 复位值与表一致；断言：复位有效的下一拍寄存器等于表中复位值 |
| | `reg.access` | 静态：只读寄存器在写使能控制下被赋值；可写寄存器除了复位以外没有任何赋值；表里的寄存器在 RTL 里找不到对应信号 |

**静态检查覆盖全部实例，N1 判为 BLOCKED 的也包括在内。** 工具读不进的设计，往往正是最需要这些检查的地方，所以静态检查另建一张覆盖全部实例的 VDG。断言和 cover 只生成给 N2 建了模的实例，因为引擎也读不进 BLOCKED 的实例。

## 两份渲染

开源 Yosys 的前端不认 `assert property (@(posedge clk) ...)`，所以同一棵表达式树要渲染两次（规格 N5、N6 的交付版和验证版）：

| | 交付版 `n2b_bind.sv` | 验证版（只给引擎用） |
|---|---|---|
| 形式 | `N2B_0007: assert property (@(posedge clk) disable iff (!rst_n) (full && push && !pop) \|=> count == $past(count));` | `always @(posedge clk) if (!$initstate && rst_n && vm_n2b_pv_clk && $past(rst_n)) vm_3: assert(!$past(...) \|\| (...));` |
| 挂法 | 每个实例一个 checker 模块，`bind <实例路径> …` | 在模块源码副本的 `endmodule` 前、同一行插入 `` `include "vm_n2b_<模块>.svh" ``，行号不变 |
| 常量 | 一律写成整数，不用参数名或枚举名，因为 checker 模块看不到设计里的名字 | 同左 |
| 通配比较 | `req ==? 2'b1?` | Yosys 不支持 `==?`，改写成掩码比较 `(req & 2'd2) == 2'd2` |

交付版会用 slang 连同设计一起编译一遍，结果记在 `delivery_check` 里。

验证版按模块插入，同一份文本作用于该模块的所有实例，所以只有各实例文本完全相同的属性才送去验证。参数不同的实例可能生成不同的属性，这些标 `SKIPPED` 并写明原因。

## 引擎与状态

验证环境：

- 第一拍复位有效（`assume`）
- 配置里的 `assumes` 能转换的也加进去：`a |-> b`、`a |=> b` 和纯表达式
- 其余输入不受约束（N3a 以后补充环境假设）
- 三态总线用 `tribuf -formal` 转成逻辑，并删掉它自带的冲突断言。`x.bus_conflict` 查的是同一件事；留着的话，总线冲突会让每一个无关属性的证明都失败

| 状态 | 含义 |
|---|---|
| `FAILED` | BMC 找到反例，保留波形。这是**候选 RTL 缺陷**，交给 N8 |
| `PROVEN` | k-induction 证明对任意深度都成立 |
| `BOUNDED` | `depth` 拍内没有反例，但归纳证明不出来 |
| `REACHED` | cover 可达，附到达的拍数 |
| `UNREACHABLE` | 把 cover 取反当断言，k-induction 证明它成立，即永远到不了 |
| `UNREACHED` | `depth` 拍内到不了 |
| `ENV_ERROR` | Yosys 读不进带断言的设计 |
| `SKIPPED` | 验证版渲染不出来，原因见 `skip` |
| `NOT_RUN` | 缺工具，或用了 `--no-formal`；或者 N1 已判定 Yosys 读不了这个设计，此时会给出警告 `N2B_NO_FORMAL` |

运行方式：

1. BMC（`--keep-going`，一次跑出所有失败）和 cover 并行跑
2. 再把有界通过的断言和有界内不可达的 cover 放在一次证明里跑
3. 第 2 步没有全部通过时，按 `n2b_prove_max` 的上限逐条并行证明

结果从 sby 的完整摘要文件里解析，不用控制台输出，因为控制台在几条反例之后会截断。

静态结论和形式结论会合并：

- 两者都指向同一个问题时（比如 N2 迁移图说状态 LOST 不可达，cover 也证明了不可达），合成一条，依据记为 `static+formal`
- 形式验证到达了静态判为不可达的状态时，静态结论标为 `REFUTED` 并降为提示

## 配置

```yaml
registers:                      # 可选；寄存器检查只在配置了它时进行
  file: rtl/regs.csv            # CSV（name,address,width,access,reset[,signal]）或 IP-XACT XML
  scope: top.u_regs             # 寄存器所在实例，默认顶层
  addr: addr                    # 地址信号，用于译码检查
  write: we                     # 写使能，用于只读检查
run:
  params: {n2b_depth: 20, n2b_timeout: 120, n2b_prove: true, n2b_prove_max: 64, n2b_max_branch_covers: 200}
```

寄存器表文件按 `registers` 角色计入 source hash，`registers` 这一节写进规范化配置。没配置 `registers` 时，规范化配置和各级哈希与以前完全一样。

## 哈希与落库

- `input_hash` 由以下各项求哈希得到（`tagged_hash("n2b_input", …)`）。`n2b_version` 在检查或模板变化时递增
  - N2 的 input_hash
  - build_hash
  - env_hash（含 yosys、sby 版本）
  - `registers` 配置
  - `n2b_version`
  - 引擎选项
- `n2b_hash = tagged_hash("n2b_result", 发现 + 属性及其状态)`，不含路径和耗时；同一输入跑两次结果一致
- 表（与 N0–N2 同库）：
  - `n2b_run`：主键 input_hash，记录 n2b_hash、引擎摘要、按类汇总
  - `n2b_finding`
  - `n2b_property`：两份渲染文本和状态
  - schema 版本变化时 n2b_* 表删除重建
- 文件：`runs/<run_hash>/n2b.json`、`n2b_report.md`、`n2b_bind.sv`；引擎的工作目录和反例波形在 `n2b/<input_hash 前 16 位>/formal/`

## 对 N1 的补充

- case 栈项带上了整条 case 的信息：`all`、`has_default`、`condition`、`check`
- 含 x/z 的常量单独成为 `xz` 节点

详见 [N1.md](N1.md)。这两项会改变 index_hash，属于派生数据，重跑即可。

## 验收

规格要求：

- 带已知缺陷的小样例集，每一类至少检出一个
- 每类记录误报数
- PicoRV32、Ibex 上无人工干预产出完整报告

样例集 `tests/golden/n2b/` 分两个设计：

| 设计 | 内容 |
|---|---|
| `golden_top`（形式可读） | 每类一个带缺陷的模块（`*_bug`）和一个干净的对照模块（`*_ok`）；`regs.sv` + `regs.csv` 演示寄存器检查 |
| `static_top`（只做静态） | 组合环、锁存器、非三态多驱动。这些写法开源形式引擎读不进，单独放一个设计，正好验证 BLOCKED 设计照样能得到静态结论 |

`expected.json` 列出：

- 27 条已知缺陷，其中 12 条需要引擎
- 8 条由同一缺陷派生出的真实发现，例如 FIFO 满写之后计数器也会回绕

此外任何发现都算误报。

当前结果（`test_golden_every_class_detected_and_no_false_positives`）：

- 8 类全部检出，27/27
- 每类误报 0；`*_ok` 模块上的断言没有一条失败
- fifo 示例：0 条发现，断言全部 PROVEN，cover 全部可达
- 样例集一次完整运行约 7 秒

## 待定

- PicoRV32、Ibex 还没跑（同 N2）。需要先下载这两个项目，再看开源引擎的容量；规模超出时要做 COI 裁剪。这部分和 N7 的容量测试一起做。
- 输入不受约束：失败的断言可能是缺环境假设。例如 `control.set_reset` 的两个异步控制都来自顶层输入时，它一定会失败。要等 N3a 补假设、N8 判根因。
- `array.overflow` 只查“信号 ± 信号”。真正有意的回绕（比如校验和）也会报出来，需要审阅时排除。
- 组合环按信号粒度计算，只涉及不同位（`a[0] → a[1]`）时可能是假环。
- 多时钟设计在验证版里按同一步长推进所有时钟，这是近似。
- 寄存器检查只读名字、地址、宽度、访问属性和复位值，不处理字段级定义和 W1C 等副作用的时序。
- 本阶段内置的引擎只是 N6、N7 的最小替身，没有引擎抽象层、能力矩阵和差分验证，那些在 N7 做。
