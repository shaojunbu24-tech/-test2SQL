# 料查分析动态 Query IR MVP

这个 MVP 实现以下可运行链路：

```text
自然语言
→ LangChain 解析实体类型、定位属性、取值和任务
→ 按 ID / 编号 / 名称定位真实生产计划（歧义时停止）
→ 普通问数：模型生成 Query IR；料差分析：注册任务按参数展开 Query IR
→ JSON Schema、实体类型、关系路径和指标依赖校验
→ 按 Query IR 动态组合参数化 SQL
→ 在 mom-test 中只读执行
→ 返回完整阶段追踪
```

规划模型使用实体、关系、属性、指标和算子，不接触账号或物理 SQL。查询后可选再次调用模型，根据实际结果回答。

本次优化的字段说明、调用链、任务口径和测试边界见 [优化说明](docs/MVP优化说明.md)。

`feature/contextual-chat` 分支新增五轮会话追问：保留原有七个展示页，每轮独立展示 Query IR、SQL 和结果；详细设计与字段见 [上下文对话设计与使用](docs/上下文对话设计与使用.md)。

## 1. 当前范围

MVP 从生产计划 ID、业务编号或名称定位开始，已经支持：

- 当前有效生产计划数量统计；
- `ProductionPlan → PlanBOMItem → Product` 计划 BOM 遍历；
- `ProductionPlan → ProcessTask` 实体遍历；
- 计划 BOM、领料、退料和报工关系；
- `Scan / Traverse / Aggregate / ComputeMetric / Filter / Sort / Project / Limit`；
- 领料量、退料量、实际报工、额定产出、差异量、差异率和严重度；
- 命令行和 Streamlit 可视化界面；
- 点击关系图中的实体或连线，浏览属性、关系定义、指标和数据库映射；
- 名称匹配多条时显示候选，不自动选第一条；
- 注册料差分析任务，固定步骤、参数化执行；
- `mom-test` 只读真实查询。

`ontology-v1` 作为只读本体设计包内置在仓库中，包含 Scope、Data、Behavior、Semantic 四层模型。
运行时模型只接收本体层的实体、关系、指标和算子，不接收数据库密码或物理 SQL。

## 2. 目录结构

```text
mvp-text-to-sql/
├── ontology-v1/                   # 完整本体设计包和语义映射
├── config/
│   ├── mapping.yaml              # 可组合实体、关系、指标物理映射
│   ├── property-semantics.yaml   # 业务属性说明、别名和关系描述
│   ├── tasks.yaml                # 固定任务注册及口径边界
│   └── query-ir.schema.json      # 模型 Candidate Query IR 契约
├── examples/
│   └── query-ir-*.json           # 8 类查询的 Candidate Query IR 样例
├── src/
│   ├── main.py                   # 命令行入口
│   ├── ui.py                     # Streamlit 可视化入口
│   └── material_query/
│       ├── config.py             # 路径、环境和文件加载
│       ├── ontology.py           # 本体注册表和模型上下文
│       ├── planner.py            # LangChain Query IR Planner
│       ├── conversation.py       # 五轮摘要、独立问题改写和可信主键继承
│       ├── request_router.py     # 模型解析定位条件和任务参数
│       ├── entity_resolver.py    # 参数化检索、唯一性判定和规范ID
│       ├── tasks.py              # 固定分析任务展开为 Query IR
│       ├── ontology_browser.py   # 点击浏览本体元数据
│       ├── validator.py          # Schema、类型、关系和依赖校验
│       ├── compiler.py           # Query IR 动态 SQL 编译器
│       ├── database.py           # mom-test 只读执行器
│       ├── pipeline.py           # 完整链路编排
│       └── presentation.py       # 命令行追踪输出
└── tests/
    └── test_dynamic_query.py     # 离线校验和动态编译测试
```

## 3. 安装

```bash
cd "/Users/boshaojun/workdir/合肥大数据与人工智能研究院/料查分析/mvp-text-to-sql"
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

项目本地 `.env` 读取以下变量：

```text
OPENAI_API_KEY=模型密钥
OPENAI_BASE_URL=https://api.deepseek.com
OPENAI_MODEL=deepseek-flash

DB_HOST=数据库地址
DB_PORT=3306
DB_USER=数据库账号
DB_PASSWORD=数据库密码
DB_NAME=mom-test
```

`.env` 已加入 `.gitignore`，不要提交或复制到日志中。

## 4. 命令行运行

完整自然语言到数据库执行：

```bash
.venv/bin/python src/main.py \
  "分析生产计划ID 665的投料差异，筛选差异率超过5%的记录" \
  --execute --answer
```

查看模型生成的完整 Query IR 和动态 SQL：

```bash
.venv/bin/python src/main.py \
  "分析生产计划ID 665的投料差异，筛选差异率超过5%的记录" \
  --execute --show-ir --show-sql
```

查看实际提供给模型的本体上下文：

```bash
.venv/bin/python src/main.py \
  "查询生产计划ID 665下的工序任务" \
  --show-context
```

输出完整机器可读追踪：

```bash
.venv/bin/python src/main.py \
  "查询生产计划ID 665下的工序任务" \
  --execute --json
```

不调用模型，直接验证 Query IR 到 SQL：

```bash
.venv/bin/python src/main.py \
  "离线测试" \
  --from-ir examples/query-ir-material-gap.json \
  --show-sql
```

## 5. 启动可视化界面

```bash
.venv/bin/streamlit run src/ui.py --server.address 127.0.0.1 --server.port 8502
```

浏览器访问：

```text
http://127.0.0.1:8502
```

界面包含七个页面：

- 本体浏览：点击全量关系图中的实体或连线，右侧显示对应属性或关系映射；
- 本体概览：实体表、关系图和指标表；
- 实体定位与任务：原始属性、匹配候选、规范 ID 和注册任务参数；
- Query IR：实际模型计划或固定任务展开的计划，注明来源；
- 校验与映射：变量类型、指标依赖和算子物理映射；
- SQL：动态参数化 SQL 和独立参数；
- 执行结果：规范实体、数据库结果、阶段耗时和完整追踪 JSON。

输入框上方现在可连续提问；下方对话记录保存最近五轮，可点击“查看本轮 Query IR / SQL / 结果”切换七个页面所展示的轮次。“清空对话上下文”只清理页面会话，不修改本体文件或数据库。

## 6. Query IR 如何动态变化

问题：

```text
查询生产计划ID 665下的工序任务
```

模型只需要生成：

```text
Scan ProductionPlan
→ Traverse HAS_PROCESS_TASK
→ Project
→ Limit
```

不会加载 BOM、领料、退料或报工。

问题：

```text
分析生产计划ID 665的投料差异，筛选差异率超过5%的记录
```

现在由模型识别为注册料差任务，程序根据已注册的计划和用户参数展开：

```text
HAS_PLAN_BOM_ITEM
HAS_MATERIAL_ISSUE
HAS_PRODUCTION_REPORT
Aggregate
ComputeMetric
Filter severityRate
```

普通查询仍由模型自由组合受支持的算子；复杂、固定口径的料差分析不再每次重新规划。两条路径都进入同一套 Query IR 校验和 SQL 编译器，不由模型自由编写 SQL。

## 7. 校验规则

模型输出在编译前必须通过：

- Candidate Query IR JSON Schema；
- 步骤连续编号和 bind 唯一性；
- input 必须引用此前生成的变量；
- Traverse 输入类型必须匹配关系起点；
- 基础指标必须使用正确来源实体和聚合粒度；
- 派生指标必须具备完整依赖；
- Project 和 Sort 只能引用白名单字段；
- 第一步必须是 `Scan`，最后一步必须是 `Limit`；
- 除全部有效计划计数外，查询必须有计划 ID；最多返回 200 行。

## 8. 数据库安全边界

- 连接配置必须精确指定 `DB_NAME=mom-test`；
- 连接后再次执行 `SELECT DATABASE()` 校验当前库；
- 只允许 `SELECT` 或 `WITH` 开头的查询；
- 启动只读事务；
- 执行前先验证生产计划真实存在；
- 查询结束统一回滚并关闭连接；
- 表、列和 JOIN 只能来自 `mapping.yaml`；
- 用户输入只作为绑定参数，不能成为 SQL 标识符。

## 9. 运行测试

```bash
.venv/bin/python -m unittest discover -s tests -v
```

真实数据库冒烟测试（不调用模型）：

```bash
.venv/bin/python tests/smoke_live.py --output local-traces/optimization-db.json
```

完整自然语言链路测试（需要模型额度和数据库连接可用）：

```bash
.venv/bin/python tests/smoke_live.py --with-llm --output local-traces/optimization-e2e.json
```

`local-traces/` 包含业务结果，已加入 Git 忽略。普通问数会调用请求解析模型和规划模型；固定料差任务仅调用请求解析模型。`--answer` 额外调用结果解读模型。`--from-ir` 跳过请求解析和规划；不加 `--execute` 不访问数据库，因此编号/名称定位会提示需要真实只读查询。

离线回归测试覆盖：

- 料差 Query IR 校验与动态 SQL 编译；
- 简单工序查询不会加载无关事实表；
- 生产计划计数仅命中 `produce_plan`；
- 计划 BOM 的层级、物料和规格映射；
- 8 个样例全部通过校验和编译；
- 从错误实体类型遍历关系时必须被拒绝。

在 `mom-test` 上只读执行全部样例：

```bash
.venv/bin/python tests/run_examples_readonly.py
```

调用真实模型评估 8 个自然语言场景：

```bash
.venv/bin/python tests/run_natural_language_eval.py
```

## 10. MVP 的已知边界

- 除“当前有效生产计划数量”外，明细查询当前仍需要明确的生产计划数字 ID；
- 当前动态编译器重点覆盖计划、任务和料差指标；
- 退料量目前展示但不从额定产出口径中扣除；
- Query IR 校验失败会直接停止，尚未增加模型自动修复循环；
- 诊断、质检和入库分支留到下一阶段。
