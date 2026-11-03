# N0a 配置补全

输入：RTL 目录，或已有的 .f，或缺字段的 YAML。
输出：一份 YAML 草稿（`status: draft`，需要用户决定的项在 `unresolved` 里）、一份推断报告 `n0a_report.json`，以及可选的 `.f` 导出。用户处理完 unresolved 后由 `verimine confirm` 把 status 改为 `confirmed`，然后才能进入 N0。

```bash
verimine infer path/to/rtl                      # 只有 RTL：推断文件、顺序、include 路径、top、时钟复位、宏、黑盒
verimine infer path/to/rtl --filelist my.f      # 有 .f 没 YAML：YAML 用 design.filelist 引用它
verimine infer --config verimine.yaml           # 有 YAML 但缺字段：只补缺的
verimine infer path/to/rtl --top core_top       # 手动指定 top，重新计算闭包
verimine infer path/to/rtl --export-f           # 同时导出 .f（文件头注明以 YAML 为准）
verimine infer path/to/rtl --dry-run --json     # 只看结果不写文件

verimine confirm verimine.yaml                  # 交互：逐项确认 unresolved
verimine confirm verimine.yaml --define WIDTH=8 # 给值宏取值，同时消掉“宏 WIDTH 需要取值”
verimine confirm verimine.yaml --resolve 1 3    # 把第 1、3 项标为已处理
verimine confirm verimine.yaml --all            # 已手工改好 YAML，全部标为已处理
```

`confirm` 退出码：0 已确认；2 仍有未处理项（status 保持 draft）。

## 第 0 步：用户给了什么

| 用户提供 | N0a 做什么 | `mode` |
|---|---|---|
| YAML（已确认、有 top、有文件、有时钟） | 跳过，直接进 N0，文件不动 | `skip` |
| 有 .f，没有 YAML | `design.filelist` 引用这个 .f，不复制内容；只推断 top、时钟复位、宏取值、黑盒。.f 里的 `+incdir+`、`+define+` 视为已知 | `filelist` |
| 只有 RTL | 第 1–9 步推断 `files`、编译顺序、`include_dirs`，再推断其余字段 | `rtl_only` |
| 有 YAML 但缺字段 | 只补缺的字段（不存在或为空）；用户写了的字段不改，推断结果不一致时记进报告 `conflicts`。原 YAML 的注释和字段顺序保留 | `fill_missing` |

补了任何字段、或 unresolved 不为空，status 都会改回 `draft`。

## 步骤与代码位置

| 步骤 | 内容 | 代码 |
|---|---|---|
| 1 | 扫描 .v/.sv/.vh/.svh，跳过 .git、build、obj_dir、sim_build、work 等目录；内容相同（CRLF 统一后 SHA-256）的文件只留一份 | `n0a/scan.py` |
| 2 | 单文件语法解析：定义、实例（含所有 ifdef 分支）、package 引用、接口端口、`include、宏定义/使用；解析不了的用正则兜底；事实表按内容哈希缓存在 `.verimine/n0a_cache/` | `n0a/facts.py` |
| 3 | 分类：被 include 的文件和 .vh/.svh 是头文件，不作为编译单元 | `n0a/graph.py: build` |
| 4 | 依赖图：package、宏决定顺序，实例只用于闭包；出现环就报告，不自动拆 | `n0a/graph.py: closure` |
| 5 | 推断 top：排除 testbench 后没被实例化的模块，按实例树大小、是否位于 rtl/src 排序 | `n0a/graph.py: top_candidates` |
| 6 | 从 top 取闭包，分层拓扑排序，同层按路径排序 | `n0a/graph.py: closure, _toposort` |
| 7 | include 路径反推：`include "common/defs.vh" 找到 `X/common/defs.vh` 就加 `X`；多处匹配选离引用文件最近的 | `n0a/incdir.py` |
| 8 | 宏分类：值宏未定义进 unresolved（阻断），条件宏默认不定义、列为待确认（不阻断），SYNTHESIS 一类特别提示 | `n0a/macros.py` |
| 9 | 闭环：按草稿跑 slang elaboration，根据诊断自动调整，最多 3 轮 | `n0a/check.py`、`n0a/pipeline.py: _infer_top` |
| 10 | 时钟复位候选 | `n0a/clkrst.py` |
| 11–13 | YAML 草稿、推断报告、可选 .f | `n0a/emit.py` |
| 14 | 用户确认 | `n0a/pipeline.py: confirm`、`cli.py` |

## 推断规则细节

**testbench**（不作为 top 候选，报告 `testbenches` 里写明原因）：没有端口（`no_ports`）；initial 块里调用 `$finish/$display/$readmemh` 等（`sim_tasks`）；`always #5 clk = ~clk` 或 `forever #`（`clock_gen`）；文件名或模块名含 `tb_`、`_tb`、`test`、`bench`（`name`）；位于 `tb/`、`sim/`、`verif/`、`test/`、`dv/` 等目录（`dir`）。testbench 内的实例不算“被实例化”，否则 DUT 会因为被 tb 实例化而落选。

**多个 top**：每个 top 各生成一份 `verimine.<top>.yaml` 和 `n0a_report.<top>.json`，unresolved 第一项提示只保留需要的那份。

**重复定义**：内容完全相同的在扫描时已去重（报告 `copies`）。内容不同时，优先路径里不含 old/bak/backup/copy/model/sim/orig 的，其次与父模块同目录的；被选中的和其余的都写进 unresolved，等用户确认。

**闭环自动调整**：

| slang 诊断 | 自动调整 |
|---|---|
| UnknownModule | 扫描集里有定义就补文件（.f 模式下不改文件，提示“不在 .f 中但 X 里有定义”）；名字像 FPGA 原语或存储器宏（FDRE、BUFG、RAMB*、sram_* 等）就列为黑盒候选；都不是进 unresolved |
| UnknownPackage | 扫描集里有定义就补文件；否则进 unresolved |
| UnknownDirective（宏） | 已在第 8 步列为值宏的不重复报；其余进 unresolved |
| 其他 error | 3 轮后仍有，进 unresolved（列出个数和第一个） |

黑盒候选同时写进 `design.blackbox` 和 unresolved（“X：是黑盒还是漏了文件”），与规格中的草稿示例一致。

**.v 文件**按 SystemVerilog 解析失败、按 Verilog-2005 关键字集（`begin_keywords "1364-2005"`）解析成功的，在报告 `parse.verilog2005` 里注明。

**时钟复位**：在 top 及其所有子实例的 `always_ff`/`always` 敏感列表里找边沿信号，只认能通过端口直连追溯到 top 输入端口的信号，用 top 端口名报告（子模块的 `clk` 由 top 的 `sys_clk` 驱动，就报 `sys_clk`）。

- 时钟：边沿信号且不是复位
- 异步复位：在敏感列表里、同时是块内首个 if 的条件
- 同步复位：单时钟块首个 if 的条件，是 top 输入、名字像复位（rst/reset/clr/clear）。名字限制是为了不把 `if (en)` 当成复位
- 极性：`!x`、`~x`、`x == 0`、`negedge x` 为低有效，否则高有效
- 推断出多个时钟：全部写进 `clocks`，并进 unresolved 提示多时钟超出 MVP、N1 会报 `UNSUPPORTED_MULTICLOCK`
- 没推断出时钟：进 unresolved

## 推断报告 `n0a_report.json`

依据不写在 YAML 里（YAML 注释在程序读写时会丢），都在报告里：

| 字段 | 内容 |
|---|---|
| `fields.<名>` | 每个推断字段的 `value`、`basis`（依据）、`confidence`（高/中/低/需确认）、`alternatives`（备选） |
| `top_candidates`、`testbenches` | 所有 top 候选（实例树大小、是否在 rtl 目录）和被排除的 testbench 及原因 |
| `includes` | 每条 `include 的匹配结果、推出的 include 路径、备选 |
| `macros` | `value_missing`、`cond_missing`（默认不定义，需确认）、`flavour`（SYNTHESIS 等提示） |
| `missing_modules`、`duplicates`、`copies`、`cycles` | 缺失模块、重复定义、同内容副本、依赖环 |
| `check` | 闭环轮数、是否干净、剩余错误 |
| `conflicts`、`filled` | 补全模式下与用户值不一致的字段、本次补上的字段 |
| `unresolved` | 与 YAML 中一致 |

## 与哈希的关系

status、unresolved 和推断报告都不参与哈希；只有确认后的 YAML 内容参与 `build_hash`（由 N0 计算）。换推断算法不会让历史结果失效。

## 确定性

同一输入跑两次，YAML 和报告逐字节相同（`test_deterministic_byte_identical`）：列表一律排序或按编译顺序，路径一律相对 root，不写时间戳和机器名，报告 JSON 按键排序。

## 实现时的取舍

- **配置模型只有一份**：草稿写出前用 `config.Config`（N0 校验用的同一个 pydantic 模型）校验一遍。
- **依赖图没有用 networkx**：只需要分层拓扑排序和找环，几十行纯 Python 就够，少一个依赖。
- **命令行用 argparse**：与已有的 `n0`、`n1` 命令保持一致，没有引入 typer/click。
- **ifdef 分支**：语法解析只走激活分支，所以每个文件额外解析：所有被测试的宏都定义一次、每个宏单独定义一次（≤16 个宏时），后者覆盖 `elsif 分支。只在非默认分支出现的实例记为 `cond_instances`：找得到定义就进闭包，找不到不报缺失。
- **宏和 include 用正则从原文提取**：它们在 slang 语法树里是 trivia，单文件解析时宏展开失败会丢 token；正则在去掉注释和字符串后足够准确。
- **时钟复位用 elaboration 结果而不是语法树**：第 9 步已经有完整的 Compilation，端口方向、敏感列表、if 条件都能直接拿到，比在语法树上处理 ANSI/非 ANSI 端口简单可靠。
- **include 路径总是显式加入**：即使头文件就在引用文件的目录下，也把目录加进 `include_dirs`，不依赖各工具“先搜当前文件目录”的行为差异。

## 待定

- 验收第 1、2 项（Ibex、OpenTitan、PicoRV32 标准答案集与指标）需要下载这些项目跑一遍，目前只用 `examples/fifo` 验证：删掉 YAML 和 .f 后推断结果与手写配置一致（`test_fifo_example_matches_handwritten_config`）。
- 被 Verilog-2005 方式解析的文件只在报告里注明；N0/N1 仍按 `design.language` 统一解析，混合语言标准的工程需要后续支持按文件指定标准。
- 宏提供者（在编译单元里 `define、被别的文件使用）有多个时取路径排序的第一个，没有列备选。
