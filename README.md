# 料查分析动态 Query IR MVP

这个 MVP 实现以下可运行链路：

```text
自然语言
→ LangChain 调用模型读取相关本体
→ 模型生成 Candidate Query IR
→ JSON Schema、实体类型、关系路径和指标依赖校验
→ 按 Query IR 动态组合参数化 SQL
→ 在 mom-test 中只读执行
→ 返回完整阶段追踪
```

模型只使用实体、关系、指标和算子，不接触数据库表名、字段名、JOIN、账号或 SQL。

## 1. 当前范围

MVP 从生产计划 ID 开始，已经支持：

- 当前有效生产计划数量统计；
- `ProductionPlan → PlanBOMItem → Product` 计划 BOM 遍历；
- `ProductionPlan → ProcessTask` 实体遍历；
- 计划 BOM、领料、退料和报工关系；
- `Scan / Traverse / Aggregate / ComputeMetric / Filter / Sort / Project / Limit`；
- 领料量、退料量、实际报工、额定产出、差异量、差异率和严重度；
- 命令行和 Streamlit 可视化界面；
- `mom-test` 只读真实查询。

`ontology-v1` 当前只作为只读本体来源，本次重构没有修改其中任何文件。

## 2. 目录结构

```text
mvp-text-to-sql/
├── config/
│   ├── mapping.yaml              # 可组合实体、关系、指标物理映射
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
  --execute
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
.venv/bin/streamlit run src/ui.py
```

浏览器访问：

```text
http://localhost:8501
```

界面包含五个页面：

- 本体概览：实体表、关系图和指标表；
- Query IR：模型实际生成的候选查询计划；
- 校验与映射：变量类型、指标依赖和算子物理映射；
- SQL：动态参数化 SQL 和独立参数；
- 执行结果：规范实体、数据库结果、阶段耗时和完整追踪 JSON。

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

模型会根据指标依赖增加：

```text
HAS_PLAN_BOM_ITEM
HAS_MATERIAL_ISSUE
HAS_PRODUCTION_REPORT
Aggregate
ComputeMetric
Filter severityRate
```

因此当前实现不是“意图选择整段 SQL 模板”，而是由 Query IR 决定本次真正需要哪些关系分支和指标组件。

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
- 查询必须有计划 ID，最多返回 200 行。

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
