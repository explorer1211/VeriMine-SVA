# N3a 假设挖掘

形式验证时，输入不加约束会在非法输入下产生假反例；自动激励不加约束，挖出的断言会把非法行为固化下来。这一步给出输入约束（assume）候选，经检查和用户确认后写进配置的 `assumes`。

```bash
verimine n3a cfg.yaml                              # 生成候选、检查、出报告（不改配置）
verimine n3a cfg.yaml --accept-high                # 接受全部“建议接受”的高置信候选
verimine n3a cfg.yaml --accept N3A_0007,N3A_0009   # 接受指定候选，写进配置 assumes
verimine n3a cfg.yaml --reject N3A_0011 --reason "len 可配置"
verimine n3a cfg.yaml --no-formal --no-n2b --dry-run --json
```

## 步骤与代码位置

| 步骤 | 内容 | 代码 |
|---|---|---|
| 1 | 对象：顶层输入；有 N2 接口事件的子模块的输入 | `n3a/cands.py: structural, mine` |
| 2 | 来源：用户轨迹里输入端口的行为；N2 识别的接口事件 | 同上 |
| 3 | 候选类型及默认处理，见下表；每个候选都拿用户轨迹核对 | `n3a/cands.py: check` |
| 4 | 过强检查：用 N2b 的 cover 检查加入假设后关键状态是否仍可达 | `n3a/formal.py: strength` |
| 5 | 用户确认；确认的假设写入配置 `assumes`，参与 build_hash | `n3a/pipeline.py: apply_decisions` |
| 6 | 层级复用：子模块假设在上层转成断言去证明，DISCHARGED / OPEN | `n3a/formal.py: discharge` |
| 7 | 落库：假设表 | `n3a/store.py` |
| — | 把 N2b 的失败断言按反例是否违反候选假设归为 env_missing | `n3a/formal.py: env_missing` |

## 候选类型

| 类型 | 来源 | 形式（例） | 置信度 | 默认处理 |
|---|---|---|---|---|
| 协议保持 | N2 事件 valid/ready、req/ack，本实例是接收方 | `in_valid && !in_ready \|=> in_valid` | 高 | 建议接受 |
| 协议保持（数据） | 同上；数据信号按与 valid 同前缀认定（命名，弱证据） | `in_valid && !in_ready \|=> in_data == $past(in_data)` | 中 | 需确认 |
| 流控 | N2 事件 push/full、pop/empty | `full \|-> !push` | 中 | 需确认；设计里已经把两者一起使用时，提示“可能不必要” |
| 复位期间固定值 | 用户轨迹：复位期间输入一直取同一个值 | `!rst_n \|-> cfg == 1` | 高 | 建议接受 |
| 互斥 | 用户轨迹：几个 1 位输入各自有效过（≥ `n3a_min_support` 拍），从未同时有效 | `!(req_a && req_b)` | 中 | 需确认 |
| 独热 | 用户轨迹：向量输入从未有两位同时为 1，且取过至少两个不同的单热值 | `(mode & (mode - 1)) == 0` | 中 | 需确认 |
| 最小保持周期 | 用户轨迹：1 位输入的高脉冲（≥ `n3a_min_pulses` 个）最短 L ≥ 2 拍 | `!start && $past(start) \|-> $past(start, 2) && $past(start, 3)` | 中 | 需确认 |
| 数据取值范围 | 用户轨迹：观察到的最小、最大值 | `len >= 2 && len <= 10` | 低 | 默认不采纳（容易过拟合、过强） |

- **轨迹类候选只用用户轨迹。** 即 N3 中 `legality = user` 的用例（用户波形、用户 testbench）。自动激励反映的是我们自己的激励，不能说明环境。复位期间的输入值来自 N3 在每个用例前记录的复位段（`detail.reset_inputs`）。没有用户轨迹时只有结构候选，并给出警告 `N3A_NO_USER_TRACES`。
- **反方向不生成假设。** 实例自己驱动 valid、对方给 ready 时，"valid 保持"是实例的义务，属于断言，不是假设，这里不生成。
- **核对用户轨迹。** 每个候选（包括结构候选）都在用户轨迹上逐拍核对：
  - 有违反：直接判 REJECTED，并写明首次违反的位置
  - 前件成立过、从未违反：来源加上 `+trace`

## 状态

| 状态 | 含义 |
|---|---|
| PROPOSED | 待确认；`recommend` 给出建议接受 / 需确认 / 默认不采纳 |
| ACCEPTED | 在配置的 `assumes` 里（内容与配置中某条相同的候选会合并成一行，来源记 `+config`） |
| REJECTED | 用户轨迹违反、过强，或用户拒绝 |
| DISCHARGED | 子模块假设，在上层实例化后作为断言被证明 |
| OPEN | 子模块假设，上层没有约束保证它；在报告里单独列出 |

**确认（第 5 步）：**

- `--accept` / `--accept-high` 把候选写进配置 `assumes`，写法是 ruamel 往返写入：保留原有注释，新条目后面注明来源和置信度。写入后用配置模型校验一遍。
- 配置一变，build_hash 随之变化，N1 及以后各阶段都会重算。
- `--reject` 记在库里（`n3a_decision`），之后再跑仍然是拒绝。这张表不是派生数据，schema 变化时也不会删。
- 已被拒绝的候选仍可以 `--accept`，这时会给出警告。子模块假设不能写进顶层配置。
- 规格里"高置信类无需确认"对应 `--accept-high`：只要不用 `--accept-high`，配置就不会被改动。

## 过强检查（第 4 步）

用 N2b 生成的 cover 做对照：状态机的每个状态、FIFO 满和空、每个带赋值的分支。

1. 不加任何假设跑一遍，得到基准可达集
2. 每条假设单独加入再跑一遍，配置里已有的假设也一样
3. 把配置里的假设和"建议接受"的候选一起加入再跑一遍

比较的结果：

| 结果 | 处理 |
|---|---|
| 状态机状态或 FIFO 满/空从可达变成不可达 | 过强。候选判 REJECTED；配置里的假设不能替用户删除，给出警告 `N3A_OVERSTRONG` |
| 只有分支变得不可达 | 给出警告 `N3A_OVERSTRONG_BRANCH`；建议接受降为需确认（可能是只服务于非法输入的分支，需要人判断） |
| 合在一起过强 | 给出警告 `N3A_OVERSTRONG_COMBINED` |

## 层级复用（第 6 步）

子模块的候选（目前来自 N2 接口事件）转成该实例上的断言（前件、后件不变），在整个设计里、配置的顶层假设之下用 k-induction 证明：

- 证明成立 → DISCHARGED
- 有反例或只能有界成立 → OPEN，原因里写明

例：fifo 示例中，`u_ctrl` 的 `full |-> !push` 被顶层假设 `full |-> !push` 保证，判 DISCHARGED；`empty |-> !pop` 在顶层没有约束，判 OPEN。

## env_missing 归类

N3a 会运行一次 N2b。对每条 FAILED 的断言，读出它的反例波形，检查是否违反了某个尚未接受的候选（PROPOSED 或 OPEN）：

- 违反了 → 归为缺环境假设（env_missing），并列出违反的候选
- 没违反 → 其他

这对应规格 N8 的 env_missing 规则：反例涉及输入端口、且违反协议模板。N8 建好后由 N8 接管。

## 激励与验证版对新写法的支持

为了让挖出的假设真正生效，N3 生成的 testbench 和形式引擎现在都支持以下写法：

- `$past(e)` 和 `$past(e, k)`：testbench 里用寄存器链实现，在有效沿采样
- `$stable`、`$changed`、`$rose`、`$fell`：先改写成 `$past` 形式（`verimine/sva_text.py`）

因此像 `|=> in_data == $past(in_data)` 这样的假设，在随机仿真里会通过拒绝采样被满足，在形式验证里会成为 assume。N3 也由此升到 `N3_VERSION = 2`。

## 哈希与落库

- **input_hash** 由以下各项求哈希得到（`tagged_hash("n3a_input", …)`）：
  - N3 的 input_hash 和 traces_hash
  - build_hash（含配置 assumes）、env_hash
  - 参数、是否跑形式检查、是否归类
  - 已拒绝的候选
  - `N3A_VERSION`
- **n3a_hash** 由候选表和归类结果求哈希得到，不含引擎路径
- **表：**
  - `n3a_run`：计数、过强检查、归类
  - `n3a_assume`：假设表，记录 id、作用域、类型、名字、表达式、来源、置信度、状态、原因、明细
  - `n3a_decision`：用户决定
- **文件：** `runs/<run_hash>/n3a.json`、`n3a_report.md`；引擎工作目录在 `n3a/<input_hash 前 16 位>/`

## 参数（`run.params`）

| 参数 | 默认 | 含义 |
|---|---|---|
| `n3a_min_support` | 3 | 互斥、独热、取值范围需要的最少样本数 |
| `n3a_min_pulses` | 3 | 最小保持周期需要的最少脉冲数 |
| `n3a_max_mutex` | 16 | 互斥候选的组数上限 |
| `n3a_depth` / `n3a_timeout` | 20 / 120 | 过强检查和层级复用证明的深度、超时 |

## 验收

规格的三条（`tests/test_n3a.py`，样例在 `tests/golden/n3a/`）：

| 规格要求 | 结果 |
|---|---|
| 带握手协议的样例上，加入假设前后 env_missing 反例数下降 | `hs_rx.sv`：没有假设时，N2b 的 `counter.overflow` 和 `counter.underflow` 两条断言失败，反例都违反"`in_valid` 在 `in_ready` 前保持"，归为 env_missing。`--accept-high` 后重跑，两条变为 BOUNDED，env_missing 从 2 降到 0。N3 的随机激励随后也遵守这条假设（逐拍检查过） |
| 过强检查能拦截故意构造的过强假设 | 在配置里写 `!in_valid`：状态 SEEN 变得不可达，给出警告 `N3A_OVERSTRONG` 和 `N3A_OVERSTRONG_COMBINED`。`ctl.sv` 上，从轨迹挖出的 `len >= 2 && len <= 10` 使 LONG 状态不可达，自动判 REJECTED；独热假设只让一个非法 mode 才走的分支变得不可达，所以只给警告、保留为需确认 |
| OPEN 的假设在报告中单独列出 | fifo 示例：`u_ctrl` 的 `empty \|-> !pop` 为 OPEN，列在报告的"OPEN"一节；`full \|-> !push` 为 DISCHARGED |

另外还测了：

- 5 类轨迹候选都能从 `tb_ctl.sv` 的轨迹里挖出来
- 用户轨迹违反结构候选时，该候选被拒绝
- 拒绝会被记住；接受会写进配置，写入后配置仍能通过 N0 的校验
- 写法改写（`desugar`、`$past` 解析）
- 命令行

## 待定

- **子模块候选只来自 N2 接口事件。** 子模块输入的轨迹行为（互斥等）没有挖；顶层只有结构候选时也一样。
- **最小保持周期只看 1 位信号的高脉冲。** 低脉冲、多位数据"变化后保持 N 拍"都没做。
- **最小保持周期在复位后的头几拍偏宽。** 写成 `$past(x, k)` 后，在复位后的前 k 拍里 `$past` 读到的是初值，这几拍里约束偏宽。
- **复位期间固定值不进随机激励。** 这类假设在形式模型里生效，但 N3 的随机激励在复位期间把输入固定为 0，不读这类假设。值不为 0 时，生成的复位段会违反它（复位段本来就会被 N3 裁掉）。
- **随机激励的拒绝采样可能卡住。** 假设之间互相制约时，拒绝采样可能找不到合法输入，N3 会给出警告 `N3_REJECTION_STUCK`；这时需要改用约束求解。
- **env_missing 归类只看已有候选。** 反例违反的环境规则不在候选里时，归为"其他"，交给 N8 判断。
- **PicoRV32、Ibex 还没跑。**
