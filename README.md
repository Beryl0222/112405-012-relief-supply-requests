# 跨语种公共卫生物资调拨的需求解释与办理

项目保存用于本地业务联调的合成原始文本、对象登记及已有办理记录。文字包含尚未确认的主张，不能直接当作业务结论。资料口径见 [资料口径](docs/materials.md)。

`data/case.json` 是一份可追加原文的资料包；`docs` 记录输入含义，`tests` 检查资料完整性与业务规则。所有姓名、地点、灾情均为虚构演练内容。

## 能力概览

`relief/` 包把“读入原文”到“可兑现调拨”做成一条事件驱动的链路：

1. **双语解释（不猜测）**：区分“再要两箱”（追加）与“总共要两箱”（总量）；“两套滤芯、一套两个 F2”登记为 4 个 F2 而不是两箱；“水还够，滤芯只剩两套”不会被翻译成缺水。语义不明时提出具体澄清，答复只解决对应问题，其他独立缺项继续办理。
2. **代报与盘点分层**：志愿者转述只留痕，不覆盖现场确认；盘点是时刻存量、不是新需求，核验员确认有效时段后才改变缺口，转发旧盘点不产生新需求。
3. **缺口与替代条件**：保留原文单位、包装、消耗期限、替代接受条件和相互引用；F1 不能按通用数量补 F2 的缺口；泵滤套装拆成部件缺口，部件不齐不整套承诺。
4. **调拨链路（不是优先级表）**：备货占用库存、装车占用车位，同一库存/车位不能被两张有效单占用；车辆受容量、出发窗口与可达点约束；装车单必须同时满足可用库存与核验后的缺口。
5. **改送与短装**：改送后原点未满足量与新点交接量按实际卸货重判，同一批货不能两边都算完成；已装车货物不会因请求改口凭空回库，只有仓管显式回库才释放库存。
6. **冲突裁决**：新盘点撞上备货/装车单时冻结受影响安排并提示值班员，既避免超送也保住已作出的运输交接。
7. **可重复办理与读取**：命令带幂等键，事件以 JSONL 只追加落盘，重放日志可完整重建状态；请求方可查询已确认缺口、在途承诺与未解决澄清。

## 目录结构

```
relief/            事件溯源领域模型、解析、引擎、分配、读模型、CLI
tests/             资料完整性测试与业务规则测试
work/demo.py       端到端演示脚本（不改动资料包）
data/case.json     合成原文与对象登记
docs/materials.md  资料口径与模块说明
```

## 使用方式

需要 Python 3.11+，无第三方运行依赖。建议用隔离模式运行，避免当前目录的同名模块被导入：

```sh
# 端到端演示（内存重放完整链路）
python3 -I work/demo.py

# 接入资料包并把事件持久化到日志
python3 -I -c "import sys; sys.path.insert(0,'.'); \
from relief.cli import main; \
main(['--case','data/case.json','--events','work/events.jsonl','ingest'])"

# 缺口看板 / 分配计划 / 单点查询（同一日志可重复读取）
python3 -I -c "import sys; sys.path.insert(0,'.'); \
from relief.cli import main; main(['--case','data/case.json','--events','work/events.jsonl','board'])"
python3 -I -c "import sys; sys.path.insert(0,'.'); \
from relief.cli import main; main(['--case','data/case.json','--events','work/events.jsonl','allocate'])"
python3 -I -c "import sys; sys.path.insert(0,'.'); \
from relief.cli import main; main(['--case','data/case.json','--events','work/events.jsonl','query','N'])"
```

也可在代码中直接使用 `ReliefService`：`ingest_record` 接入原文、`verify_count` 核验盘点、`create_reservation`/`confirm_load` 办理备货装车、`plan_reroute`/`record_handover` 处理改送交接、`decide_conflict` 值班裁决、`gap_board`/`query_site`/`allocate` 读取结论。

## 测试

```sh
python3 -I -m unittest discover -s tests -v
```

测试覆盖：口径区分、澄清闭环、代报留痕、盘点核验、型号不兼容、套装拆分、库存与车位独占、容量/窗口/可达性、改送重判、短装交接、后备份额保护、条件期限触发、冲突冻结与裁决、幂等接入与事件日志回放。
