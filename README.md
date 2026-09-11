# 自然语言到 SQL 的最小可运行框架

这个 MVP 只实现一条链路：

```text
自然语言
→ 把完整 ontology.yaml 交给大模型
→ 大模型输出本体查询 JSON
→ 本地校验本体查询
→ 从意图展开实体、关系和指标轨迹
→ 从 mapping.yaml 选择固定 SQL 模板
→ 输出参数化 SQL 和参数
```

程序只生成 SQL，不连接或查询数据库。

## 1. 目录结构

```text
mvp-text-to-sql/
├── config/
│   ├── ontology.yaml       # 完整发送给模型的 MVP 本体
│   └── mapping.yaml        # 只供后端使用的本体到 SQL 映射
├── examples/
│   └── analyze-plan-665.json
├── src/
│   └── main.py             # 完整链路和命令行入口
├── .env.example
├── requirements.txt
└── README.md
```

## 2. 当前支持的问题

- 根据数字计划 ID 查询生产计划；
- 根据数字计划 ID 查询工序任务；
- 根据数字计划 ID 生成料差分析 SQL；
- 料差分析可以携带 0～1 的异常严重度阈值；
- 其他问题返回 `UNSUPPORTED`。

当前不支持计划名称、车间名称和产品名称的实体解析。MVP 要求用户明确给出数字计划 ID。

## 3. 安装

建议使用 Python 3.9 或更高版本：

```bash
cd 料查分析/mvp-text-to-sql
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
```

设置 API 密钥和模型：

```bash
export OPENAI_API_KEY="你的密钥"
export OPENAI_BASE_URL="https://api.deepseek.com"
export OPENAI_MODEL="你的模型名称"
```

程序也会自动读取项目根目录的 `.env`。当前测试环境已经配置完成，`.env` 权限为仅当前用户可读写，并已通过 `.gitignore` 排除。不要把 `.env` 内容复制到 README、日志或版本库。

## 4. 正常运行

```bash
python3 src/main.py "分析生产计划ID 665的投料差异，筛选差异率超过5%的记录"
```

模型预期输出的本体查询是：

```json
{
  "intent": "ANALYZE_MATERIAL_GAP",
  "plan_mention": "生产计划ID 665",
  "plan_id": 665,
  "threshold": 0.05,
  "limit": 100
}
```

### 本体查询 JSON 字段说明

| 字段 | 类型 | 必填 | 示例 | 含义 | 后续用途 |
|---|---|---:|---|---|---|
| `intent` | 字符串枚举 | 是 | `ANALYZE_MATERIAL_GAP` | 用户希望执行的本体查询类型，只能选择本体已经登记的意图 | 用于从 `mapping.yaml` 选择 SQL 模板 |
| `plan_mention` | 字符串或 `null` | 是 | `生产计划ID 665` | 用户原文中表示生产计划的短语，用于展示实体命中证据 | 写入 `ontology_trace.entity_hits[].mention`，不进入 SQL |
| `plan_id` | 正整数或 `null` | 是 | `665` | 用户明确说出的生产计划源系统 ID；不是模型生成的规范 ID | 绑定到 SQL 中的 `%(plan_id)s` |
| `threshold` | 0～1 的数字或 `null` | 是 | `0.05` | 料差严重度筛选阈值；5%必须表示为0.05；用户没说阈值时为 `null` | 绑定到 `severity_rate >= %(threshold)s`；为 `null` 时不过滤 |
| `limit` | 1～200 的整数 | 是 | `100` | 最多返回多少行；用户没指定时模型按本体规则返回100 | 绑定到 SQL 的 `LIMIT %(limit)s` |

`intent` 当前只允许以下值：

| 值 | 含义 | `plan_id`要求 | 对应结果 |
|---|---|---|---|
| `GET_PRODUCTION_PLAN` | 查询一条生产计划基本信息 | 必填 | 计划基本字段 SQL |
| `LIST_PROCESS_TASKS` | 查询某计划下的工序任务 | 必填 | 工序任务列表 SQL |
| `ANALYZE_MATERIAL_GAP` | 查询计划BOM、领退料和报工并计算料差 | 必填 | 料差分析 SQL |
| `UNSUPPORTED` | 问题超出当前 MVP 本体能力 | 不使用 | 停止生成 SQL并返回错误 |

为什么 `threshold` 和 `plan_id` 即使没有值也要求字段存在：这是为了让模型输出始终保持固定结构，后端不用猜某个字段是“遗漏了”还是“用户没有提供”。例如：

```json
{
  "intent": "GET_PRODUCTION_PLAN",
  "plan_mention": "生产计划ID 665",
  "plan_id": 665,
  "threshold": null,
  "limit": 100
}
```

其中 `threshold: null` 明确表示该查询不需要异常阈值。

程序最终输出：

```json
{
  "mapping_version": "mom-test@0.1.0",
  "ontology_query": {
    "intent": "ANALYZE_MATERIAL_GAP",
    "plan_mention": "生产计划ID 665",
    "plan_id": 665,
    "threshold": 0.05,
    "limit": 100
  },
  "ontology_trace": {
    "natural_language": "分析生产计划ID 665的投料差异，筛选差异率超过5%的记录",
    "matched_intent": {
      "id": "ANALYZE_MATERIAL_GAP"
    },
    "entity_hits": [
      {
        "mention": "生产计划ID 665",
        "entity_type": "ProductionPlan",
        "identifier_kind": "source_id",
        "identifier_value": 665,
        "status": "TYPE_AND_ID_EXTRACTED_NOT_DATABASE_VERIFIED"
      }
    ],
    "relation_connections": [
      {
        "relation": "HAS_PROCESS_TASK",
        "from": "ProductionPlan",
        "to": "ProcessTask"
      }
    ],
    "metrics": ["ratedOutputQty", "actualReportedQty", "severityRate"],
    "sql_mapping": {
      "mapping_version": "mom-test@0.1.0",
      "template_id": "ANALYZE_MATERIAL_GAP"
    }
  },
  "sql": "参数化SQL模板",
  "parameters": {
    "plan_id": 665,
    "threshold": 0.05,
    "limit": 100
  }
}
```

默认输出不是上述完整 JSON，而是六个便于阅读的分段：

```text
[1] 自然语言
[2] 本体命中
[3] 关系连接
[4] SQL 映射
[5] SQL
[6] 参数
```

默认情况下 `[5] SQL` 只显示命中的模板名称，避免长 SQL 淹没链路信息。需要展开完整 SQL 时使用：

```bash
python3 src/main.py --show-sql "分析生产计划ID 665的投料差异"
```

需要机器读取的完整 JSON 时增加：

```bash
python3 src/main.py --json "分析生产计划ID 665的投料差异"
```

### 最终结果 JSON 字段说明

| 字段 | 类型 | 含义 | 数据来源 |
|---|---|---|---|
| `mapping_version` | 字符串 | 本次 SQL 使用的数据库映射版本，用于审计和复现 | `mapping.yaml` 的 `version` |
| `ontology_query` | 对象 | 已经通过本地校验的模型输出，保留“自然语言被理解成什么” | 大模型结构化输出 |
| `ontology_trace` | 对象 | 展示自然语言、意图命中、实体命中、关系连接、指标和 Mapping 选择 | 本地程序根据 `ontology.yaml` 展开 |
| `sql` | 字符串 | 带命名占位符的只读 SQL 模板，不包含拼接后的用户值 | `mapping.yaml` 中当前意图对应的 `sql` |
| `parameters` | 对象 | 执行 SQL 时需要单独绑定的参数；只复制 Mapping 白名单中的字段 | 从已校验的 `ontology_query` 提取 |

`ontology_trace` 内部字段说明：

| 字段 | 含义 |
|---|---|
| `natural_language` | 本次链路最初接收到的用户原文 |
| `matched_intent` | 模型命中的本体查询意图及其业务说明 |
| `entity_hits` | 从原文中提取到的实体短语、实体类型、标识类型和值 |
| `relation_connections` | 根据意图从本体中展开的实体关系；不是模型自由生成的 JOIN |
| `metrics` | 当前意图要求 SQL 实现的本体指标 |
| `sql_mapping` | 最终使用的 Mapping 版本和 SQL 模板 ID |

实体状态 `TYPE_AND_ID_EXTRACTED_NOT_DATABASE_VERIFIED` 表示：程序已经从语言中提取出 `ProductionPlan + source_id=665`，但当前边界只生成 SQL，尚未连接数据库确认计划 665 是否真实存在。

`parameters` 内部字段说明：

| 字段 | 类型 | 对应 SQL 占位符 | 说明 |
|---|---|---|---|
| `plan_id` | 正整数 | `%(plan_id)s` | 要查询的生产计划 ID |
| `threshold` | 数字或 `null` | `%(threshold)s` | 严重度过滤阈值；为 `null` 时 SQL 返回全部料差明细 |
| `limit` | 整数 | `%(limit)s` | 最大返回行数 |

最终输出中的 SQL 和参数是分开的：

```text
sql        = "... WHERE p.id = %(plan_id)s"
parameters = {"plan_id": 665}
```

程序不会生成下面这种字符串拼接：

```text
"... WHERE p.id = " + 用户输入
```

这样既方便数据库驱动做参数绑定，也避免用户输入改变 SQL 结构。

## 5. 不调用模型的离线测试

下面的命令直接读取示例本体查询，验证“本体查询 → SQL”部分：

```bash
python3 src/main.py --from-json examples/analyze-plan-665.json
```

这个模式不需要 `OPENAI_API_KEY`，适合开发 SQL 编译器时快速测试。

## 6. 连接数据库做只读测试

先在本地 `.env` 中配置：

```text
DB_HOST=数据库主机
DB_PORT=3306
DB_USER=只读用户名
DB_PASSWORD=数据库密码
DB_NAME=mom-test
```

然后执行：

```bash
python3 src/main.py --execute \
  "分析生产计划ID 665的投料差异，筛选差异率超过5%的记录"
```

`--execute` 具有以下限制：

- 只执行 `mapping.yaml` 中人工登记的 SQL；
- 使用参数绑定，不拼接用户输入；
- 数据库事务设置为只读；
- 查询结束统一回滚并关闭连接；
- 不在输出中显示主机、用户名和密码；
- SQL 模板本身包含最多200行的限制。

## 7. MVP 的安全边界

- 模型只输出 `intent` 和参数；
- 模型额外保留 `plan_mention`，用于展示实体命中证据；
- 模型不输出 SQL；
- SQL 只能来自 `mapping.yaml`；
- 所有业务值与 SQL 模板分离；
- 默认不连接数据库；只有显式传入 `--execute` 才执行已登记的只读查询；
- `plan_id`、`threshold` 和 `limit` 都经过本地二次校验；
- 不支持的问题停止生成，不自动猜测。

## 8. 下一步

完成这个最小闭环后，再按顺序增加：

1. 计划编号到真实数据库 ID 的解析；
2. 产品、车间和工序任务实体解析；
3. 更多本体查询意图；
4. 通用 Query IR，而不是一个意图对应一个 SQL 模板；
5. 数据库只读执行和结果解释。

## 9. 已完成的真实链路验证

测试输入：

```text
分析生产计划ID 665的投料差异，筛选差异率超过5%的记录
```

DeepSeek 返回并通过本地校验的本体查询：

```json
{
  "intent": "ANALYZE_MATERIAL_GAP",
  "plan_mention": "生产计划ID 665",
  "plan_id": 665,
  "threshold": 0.05,
  "limit": 100
}
```

随后程序成功展开了5条本体关系、6个指标，并命中 `mom-test@0.1.0 / ANALYZE_MATERIAL_GAP` SQL 模板。完整 SQL 见 `config/mapping.yaml`，运行命令会在 `ontology_trace` 后原样输出它。
