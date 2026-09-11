# 料查分析本体查询契约 V1

本目录是一套可直接进入编码的设计契约。YAML 保存 Scope、Data、Behavior、Semantic 四类本体模型；JSON Schema 约束模型输出和内部 Query IR。生产事实不写入 YAML，仍保存在 `mom-test` 或后续语义快照库中。

## 1. 文件职责

| 文件 | 作用 | 是否给模型 |
|---|---|---|
| `manifest.yaml` | 版本、入口和全局安全策略 | 只摘取必要策略 |
| `scope-model.yaml` | 支持的意图、范围、算子和查询边界 | 给相关意图片段 |
| `data-model.yaml` | 实体、属性、关系及类型约束 | 给相关子图，不给全量 |
| `behavior-model.yaml` | 指标、阈值、诊断规则和计划模板 | 只给名称、含义、依赖摘要；公式由程序执行 |
| `semantic-model.mom-test.yaml` | 本体到表、字段和关联路径的映射 | 不给模型 |
| `semantic-request.schema.json` | 第一阶段模型输出约束 | 作为结构化输出 Schema |
| `query-ir.schema.json` | 查询编译器产物约束 | 默认不给模型，由程序使用 |

## 2. 模型到底接收什么

模型不接收整个本体包，也不接收数据库 Schema。Context Builder 根据用户问题检索一个小型语义子图，组合出：

1. 用户原始问题；
2. 当前时间和时区，用于理解“最近七天”等表达；
3. 当前场景允许的意图；
4. 相关实体类型、业务名称和可识别编号类型；
5. 相关指标的 ID、业务含义和适用对象；
6. 允许使用的过滤字段和响应模式；
7. 输出 JSON Schema；
8. 禁止猜 ID、禁止输出 SQL、禁止创造指标等约束。

模型不应看到：

- 数据库账号、令牌和连接信息；
- 表名、字段名和 SQL 模板；
- 与当前问题无关的全量本体；
- 大批量业务实例；
- 可被程序直接执行的自由文本表达式。

示例输入见 `model-context.example.json`。

## 3. 模型输出什么

V1 模型只输出 `SemanticRequest`，即“未落地的结构化语义请求”：

```text
意图 + 用户提到的实体表述 + 过滤条件 + 时间范围 + 所需指标 + 是否诊断
```

用户说“计划 ID 665”时，模型只能输出：

```json
{
  "surface": "665",
  "expectedType": "ProductionPlan",
  "resolutionStatus": "UNRESOLVED"
}
```

它不能自行输出 `mom:production-plan:665`。后者必须由实体解析器在 `produce_plan` 或实体目录中验证记录真实存在、有效且用户有权访问后才能生成。

模型也不能输出：

- SQL；
- API 地址；
- 数据库字段；
- 指标公式；
- 未注册的实体、关系、指标或规则；
- `PersistCase` 等写操作。

## 4. 完整执行链路

```text
① 用户自然语言
   ↓
② Context Builder 检索相关本体子图
   ↓
③ LLM 输出 SemanticRequest
   ↓ JSON Schema 校验
④ 实体解析器 ResolveEntity
   ↓ 唯一、有效、有权限的规范实体引用
⑤ Semantic Validator
   ↓ 校验意图、类型、指标、时间和权限范围
⑥ Query Compiler
   ↓ 依据 plan_recipe 生成 Query IR
⑦ Logical Plan Validator
   ↓ 校验关系路径、变量依赖、写策略和成本
⑧ Physical Planner
   ↓ 依据 Semantic Model 选择 SQL/API/快照执行器
⑨ 参数化 SQL 或受控 API 调用
   ↓
⑩ Normalizer 规范化 ID、状态、单位、时间和空值
   ↓
⑪ Metric Engine 执行已注册指标
   ↓
⑫ Rule Engine 分类并生成原因候选
   ↓
⑬ Evidence Builder 生成证据、血缘、质量和口径
   ↓
⑭ Response Generator 组织最终答案
```

### 4.1 Context Builder

先用关键词、向量检索或规则定位领域和锚点实体，再从 Data Model 取 1～3 跳相关实体关系，从 Behavior Model 取依赖指标和规则摘要。它解决“大本体不能全部塞给模型”的问题。

具体实现采用“种子召回 + 图闭包扩展”，而不是简单对 YAML 做全文搜索：

```text
用户问题
  ↓ 文本规范化、编号模式和时间表达式识别
种子召回
  ├─ 意图种子
  ├─ 实体类型种子
  ├─ 指标种子
  └─ 是否要求诊断、排名等特征
  ↓
沿本体依赖图扩展
  ├─ Intent → Plan Recipe
  ├─ Recipe → Entity + Relation + Metric
  ├─ Metric → Dependency Metric/Property
  └─ Relation → From Type + To Type
  ↓
权限、数据源能力、版本和 Token 预算裁剪
  ↓
最小但依赖完整的 Ontology Context
```

索引配置见 `retrieval-index.yaml`。该文件在 V1 可以手工维护；进入工程化后应在本体发布时从四类模型自动编译生成，避免定义和索引不一致。

#### 4.1.1 为什么不能只做向量 Top-K

向量检索可能找到 `severityRate`，却遗漏它依赖的 `signedDifferenceRate`、`ratedOutputQty` 和计划 BOM。上下文检索必须在初始召回后做依赖闭包，保证每个指标和关系都能解释、能执行。向量检索只负责找种子，不负责决定最终上下文。

#### 4.1.2 V1 召回顺序

1. 正则识别“计划 ID 665”“工序任务编号”等强标识，确定候选实体类型；
2. 精确匹配业务术语和同义词，例如“料差”命中 `ANALYZE_MATERIAL_GAP`；
3. 识别“原因、为什么、诊断”等特征，决定是否扩展质检和入库诊断分支；
4. 如果精确规则不足，再使用 BM25/全文检索；
5. 本体进一步扩大后，再增加向量检索召回；
6. 多个意图得分接近时，只把意图卡片交给轻量路由模型选择，不给数据库映射。

#### 4.1.3 图闭包扩展示例

“分析计划 ID 665 的投料差异并说明原因”首先召回：

```text
Intent: ANALYZE_MATERIAL_GAP
Anchor: ProductionPlan
Feature: diagnosis_requested
```

然后根据 `analyze-material-gap-v1` 扩展出：

```text
ProductionPlan
├─ HAS_PROCESS_TASK → ProcessTask
├─ HAS_PLAN_BOM_ITEM → PlanBOMItem
ProcessTask
├─ HAS_MATERIAL_ISSUE → MaterialIssue
├─ HAS_MATERIAL_RETURN → MaterialReturn
└─ HAS_PRODUCTION_REPORT → ProductionReport
ProductionReport
├─ HAS_QUALITY_INSPECTION → QualityInspection
└─ HAS_INVENTORY_RECEIPT → InventoryReceipt
```

指标闭包继续补齐：

```text
severityRate
└─ signedDifferenceRate
   ├─ signedDifferenceQty
   │  ├─ ratedOutputQty
   │  │  ├─ grossIssuedQty
   │  │  └─ PlanBOMItem.standardUsage
   │  └─ actualReportedQty
   └─ ratedOutputQty
```

最终给模型的是这些概念的业务名称、用途和允许引用的 ID。表名、字段名和 JOIN 仍不进入模型上下文。

#### 4.1.4 上下文完整性检查

Context Builder 输出前必须验证：

- 每个关系的起点和终点实体都已包含；
- 每个指标的递归依赖都已包含；
- 每个意图引用的查询模板真实存在；
- 查询子图从锚点到所需事实连通；
- 所有内容属于同一已发布本体版本；
- 不包含 Semantic Model 的物理表字段；
- 不超过 Token 预算；超出时先删除示例和长描述，不能删除执行依赖。

### 4.2 SemanticRequest 校验

模型输出首先通过 `semantic-request.schema.json`。随后做语义白名单校验：意图必须存在，指标必须属于该意图允许集合，过滤字段必须有类型定义，时间范围不能超出 Scope Model 限制。

### 4.3 实体解析

实体解析使用确定性优先级：

```text
源系统 ID 精确匹配
→ 业务编码精确匹配
→ 标准名称精确匹配
→ 已登记别名精确匹配
→ 返回候选要求确认
```

模糊匹配只产生候选，不能自动落地。“一车间”当前没有可靠主数据映射，必须返回未解析，而不能猜成 `WS-01`。

### 4.4 Query Compiler

V1 不让大模型自由编排算子。编译器根据 `scope-model.yaml` 中意图绑定的 `plan_recipe`，展开 `behavior-model.yaml` 里的逻辑步骤，再把已落地实体、过滤条件和指标参数注入，生成 `query-ir.example.json` 形式的 Query IR。

计划和工序任务都可以作为查询锚点。编译器会先执行内部的 `NormalizeAnchor`：计划锚点向下得到任务，任务锚点沿反向关系得到所属计划。`NormalizeAnchor` 是编译期宏，不会出现在最终 Query IR 的公开算子集合中。

这样既保留通用查询机制，又让第一版行为可测试。以后需要支持开放式组合查询时，可以让模型生成“候选 Query IR”，但仍必须经过相同 Schema 和逻辑校验。

### 4.5 Physical Planner

Physical Planner 只读取 `semantic-model.mom-test.yaml`。它把：

```text
ProductionPlan
  -[HAS_PROCESS_TASK]-> ProcessTask
```

转换成已登记关联：

```text
produce_plan.id
  = produce_order_make_process_task.produce_plan_id
```

然后通过白名单查询模板和参数绑定生成 SQL。表名、字段名和 JOIN 路径只能来自已发布 Mapping，不允许模型或用户输入进入 SQL 标识符。

### 4.6 指标和规则执行

模型只选择 `ratedOutputQty` 等指标 ID。Metric Engine 根据 Behavior Model 展开依赖并调用登记的 handler。`formula_ast` 用于审计、展示和一致性检查，实际执行不能直接 `eval` 字符串。

规则引擎输出“原因候选 + 证据 + 置信度”，在当前质检和入库关联不完整的情况下，不自动宣称唯一根因。

## 5. 从 Query IR 到 SQL 的示例

Query IR：

```json
{
  "op": "Traverse",
  "input": "plans",
  "relation": "HAS_PROCESS_TASK",
  "bind": "tasks"
}
```

Mapping Registry：

```yaml
HAS_PROCESS_TASK:
  join: "produce_plan.id = produce_order_make_process_task.produce_plan_id"
```

物理规划器产生参数化查询：

```sql
SELECT t.id, t.make_process_task_code, t.product_id,
       t.plan_produce_num, t.status_id, t.start_time, t.finish_time
FROM `mom-test`.produce_order_make_process_task AS t
WHERE t.deleted = 0
  AND t.produce_plan_id = :plan_id;
```

`:plan_id` 来自经过验证的 `sourceBinding.sourceId`，而不是模型自由生成的 SQL 文本。

## 6. 建议的代码模块

```text
src/
├── ontology/
│   ├── loader              # 加载和合并 YAML
│   ├── registry            # 按版本查询实体、关系、指标和映射
│   └── validator           # 发布前结构与引用完整性校验
├── semantic/
│   ├── context_builder     # 构建最小模型上下文
│   ├── llm_parser          # 调用模型并获取 SemanticRequest
│   └── entity_resolver     # 名称/编号到规范实体引用
├── planner/
│   ├── query_compiler      # SemanticRequest → Query IR
│   ├── logical_validator   # 关系、类型、权限和成本校验
│   └── physical_planner    # Query IR → SQL/API Plan
├── adapters/
│   ├── mysql_adapter
│   └── mom_api_adapter
├── analysis/
│   ├── metric_engine
│   ├── rule_engine
│   └── evidence_builder
└── application/
    └── query_service       # 编排完整链路
```

## 7. 第一轮编码顺序

1. 本体 YAML Loader 和引用校验器；
2. `SemanticRequest`、`GroundedEntity`、`QueryIR` 数据类；
3. 模型 Context Builder 和结构化输出解析；
4. `ProductionPlan`、`ProcessTask`、`Product` 实体解析器；
5. 两个意图的确定性 Query Compiler；
6. `mom-test` 只读 SQL 物理规划器；
7. 领料、BOM、报工三个规范化查询；
8. 指标引擎、严重度分类和证据输出；
9. 质检、入库和原因候选规则；
10. 黄金样例、错误分支和数据质量测试。

第一轮跑通的最小闭环建议是：

```text
“分析生产计划 ID 665 的投料差异”
→ SemanticRequest
→ 验证计划 665
→ Query IR
→ 查询计划、计划 BOM、任务、领料和报工
→ 计算差异率
→ 输出结果、公式、源记录 ID 和数据质量说明
```

退料是否扣减、状态枚举、质检和入库关联可以保留版本化开关，不阻塞最小闭环编码。
