"""把通过校验的本体 Query IR 动态编译成参数化 MySQL 查询。"""

from dataclasses import dataclass
from typing import Any

from .ontology import OntologyRegistry


# METRIC_SQL_ALIASES 是本体指标 ID 到编译器内部结果列名的白名单。
METRIC_SQL_ALIASES = {
    "productionPlanCount": "production_plan_count",
    "grossIssuedQty": "gross_issued_qty",
    "returnedQty": "returned_qty",
    "actualReportedQty": "actual_reported_qty",
    "ratedOutputQty": "rated_output_qty",
    "signedDifferenceQty": "signed_difference_qty",
    "signedDifferenceRate": "signed_difference_rate",
    "severityRate": "severity_rate",
}

# COMPARISON_SQL 把本体过滤运算符安全映射为固定 SQL 运算符。
COMPARISON_SQL = {"EQ": "=", "GT": ">", "GTE": ">=", "LT": "<", "LTE": "<="}


@dataclass
class CompiledQuery:
    """保存 SQL、独立参数以及 Query IR 到物理操作的映射轨迹。"""

    sql: str
    parameters: dict[str, Any]
    trace: list[dict[str, Any]]
    mode: str
    mapping_version: str


class QueryIRCompiler:
    """使用结构化 Mapping Registry 组合 SQL，而不是选择整段 SQL 模板。"""

    def __init__(self, registry: OntologyRegistry) -> None:
        self.registry = registry
        self.mapping = registry.mapping
        if self.mapping.get("database") != "mom-test":
            raise ValueError("MVP 编译器只允许使用 mom-test 物理映射。")

    def compile(self, query_ir: dict[str, Any]) -> CompiledQuery:
        """根据 Query IR 实际选择的算子和关系编译查询。"""

        steps = query_ir["steps"]
        project = next(step for step in steps if step["op"] == "Project")
        relations = [step["relation"] for step in steps if step["op"] == "Traverse"]
        requested_metrics = list(project["metrics"])
        dimensions = list(project["dimensions"])
        metric_closure = self._metric_closure(set(requested_metrics))
        trace = [self._compile_trace(step) for step in steps]

        if requested_metrics == ["productionPlanCount"]:
            sql = self._compile_plan_count_query(query_ir, dimensions, relations)
            mode = "DYNAMIC_ENTITY_COUNT"
        elif "productionPlanCount" in requested_metrics:
            raise ValueError("productionPlanCount 不能与其他指标混合编译。")
        elif requested_metrics:
            sql = self._compile_metric_query(
                query_ir=query_ir,
                dimensions=dimensions,
                requested_metrics=requested_metrics,
                metric_closure=metric_closure,
                relations=relations,
            )
            mode = "DYNAMIC_METRIC_QUERY"
        elif "HAS_PLAN_BOM_ITEM" in relations:
            sql = self._compile_bom_query(query_ir, dimensions, relations)
            mode = "DYNAMIC_ENTITY_TRAVERSAL"
        elif "HAS_PROCESS_TASK" in relations:
            sql = self._compile_task_query(query_ir, dimensions)
            mode = "DYNAMIC_ENTITY_TRAVERSAL"
        else:
            sql = self._compile_plan_query(query_ir, dimensions)
            mode = "DYNAMIC_ENTITY_SCAN"

        return CompiledQuery(
            sql=sql,
            parameters=dict(query_ir["parameters"]),
            trace=trace,
            mode=mode,
            mapping_version=self.mapping["version"],
        )

    def _compile_plan_count_query(
        self,
        query_ir: dict[str, Any],
        dimensions: list[str],
        relations: list[str],
    ) -> str:
        """编译无关系遍历的有效生产计划数量查询。"""

        if dimensions or relations:
            raise ValueError("productionPlanCount 只支持无维度、无关系的生产计划计数。")
        plan = self.mapping["entities"]["ProductionPlan"]
        return (
            "SELECT\n"
            f"    COUNT(p.{plan['properties']['sourceId']}) AS production_plan_count\n"
            f"FROM {self._table(plan['table'])} AS p\n"
            f"WHERE p.{plan['active_filter']['column']} = 0\n"
            "LIMIT %(limit)s;"
        )

    def _compile_plan_query(
        self, query_ir: dict[str, Any], dimensions: list[str]
    ) -> str:
        """编译只读取生产计划实体的动态投影查询。"""

        allowed_prefix = "ProductionPlan."
        if any(not dimension.startswith(allowed_prefix) for dimension in dimensions):
            raise ValueError("计划扫描查询只能投影 ProductionPlan 属性。")
        select_items = [self._simple_dimension_expression(value, {"ProductionPlan": "p"}) for value in dimensions]
        plan = self.mapping["entities"]["ProductionPlan"]
        sort_sql = self._sort_clause(query_ir, self.mapping["output_fields"])
        return (
            "SELECT\n    "
            + ",\n    ".join(select_items)
            + f"\nFROM {self._table(plan['table'])} AS p\n"
            + f"WHERE p.{plan['active_filter']['column']} = 0\n"
            + f"  AND p.{plan['properties']['sourceId']} = %(plan_id)s"
            + sort_sql
            + "\nLIMIT %(limit)s;"
        )

    def _compile_task_query(
        self, query_ir: dict[str, Any], dimensions: list[str]
    ) -> str:
        """编译 ProductionPlan 到 ProcessTask 的动态遍历查询。"""

        aliases = {"ProductionPlan": "p", "ProcessTask": "t"}
        if any(value.split(".", 1)[0] not in aliases for value in dimensions):
            raise ValueError("工序任务查询只能投影 ProductionPlan 或 ProcessTask 属性。")
        select_items = [self._simple_dimension_expression(value, aliases) for value in dimensions]
        plan = self.mapping["entities"]["ProductionPlan"]
        task = self.mapping["entities"]["ProcessTask"]
        sort_sql = self._sort_clause(query_ir, self.mapping["output_fields"])
        return (
            "SELECT\n    "
            + ",\n    ".join(select_items)
            + f"\nFROM {self._table(plan['table'])} AS p\n"
            + f"JOIN {self._table(task['table'])} AS t\n"
            + f"  ON t.{task['properties']['planId']} = p.{plan['properties']['sourceId']}\n"
            + f" AND t.{task['active_filter']['column']} = 0\n"
            + f"WHERE p.{plan['active_filter']['column']} = 0\n"
            + f"  AND p.{plan['properties']['sourceId']} = %(plan_id)s"
            + sort_sql
            + "\nLIMIT %(limit)s;"
        )

    def _compile_bom_query(
        self,
        query_ir: dict[str, Any],
        dimensions: list[str],
        relations: list[str],
    ) -> str:
        """编译 ProductionPlan 到计划 BOM 明细及物料的实体遍历。"""

        aliases = {"ProductionPlan": "p", "PlanBOMItem": "bom", "Product": "product"}
        dimension_entities = {value.split(".", 1)[0] for value in dimensions}
        if not dimension_entities <= set(aliases):
            raise ValueError("计划 BOM 查询只能投影 ProductionPlan、PlanBOMItem 或 Product 属性。")
        needs_product = "Product" in dimension_entities
        if needs_product and "BOM_ITEM_MATERIAL" not in relations:
            raise ValueError("投影 Product 属性时必须 Traverse BOM_ITEM_MATERIAL。")

        select_items = [
            self._simple_dimension_expression(value, aliases) for value in dimensions
        ]
        plan = self.mapping["entities"]["ProductionPlan"]
        bom = self.mapping["entities"]["PlanBOMItem"]
        product_join = ""
        if needs_product:
            product = self.mapping["entities"]["Product"]
            product_join = (
                f"\nLEFT JOIN {self._table(product['table'])} AS product"
                f"\n  ON product.{product['properties']['sourceId']} = bom.{bom['properties']['productId']}"
                f"\n AND product.{product['active_filter']['column']} = 0"
            )
        sort_sql = self._sort_clause(query_ir, self.mapping["output_fields"])
        return (
            "SELECT\n    "
            + ",\n    ".join(select_items)
            + f"\nFROM {self._table(plan['table'])} AS p\n"
            + f"JOIN {self._table(bom['table'])} AS bom\n"
            + f"  ON bom.{bom['properties']['planId']} = p.{plan['properties']['sourceId']}\n"
            + f" AND bom.{bom['active_filter']['column']} = 0"
            + product_join
            + f"\nWHERE p.{plan['active_filter']['column']} = 0\n"
            + f"  AND p.{plan['properties']['sourceId']} = %(plan_id)s"
            + sort_sql
            + "\nLIMIT %(limit)s;"
        )

    def _compile_metric_query(
        self,
        query_ir: dict[str, Any],
        dimensions: list[str],
        requested_metrics: list[str],
        metric_closure: set[str],
        relations: list[str],
    ) -> str:
        """按指标依赖和 Query IR 关系分支组合物料分析 CTE。"""

        needs_bom = "ratedOutputQty" in metric_closure
        needs_issue = "grossIssuedQty" in metric_closure
        needs_return = "returnedQty" in metric_closure
        needs_report = "actualReportedQty" in metric_closure
        required_relations = {"HAS_PROCESS_TASK"}
        if needs_bom:
            required_relations.add("HAS_PLAN_BOM_ITEM")
        if needs_issue:
            required_relations.add("HAS_MATERIAL_ISSUE")
        if needs_return:
            required_relations.add("HAS_MATERIAL_RETURN")
        if needs_report:
            required_relations.add("HAS_PRODUCTION_REPORT")
        missing_relations = required_relations - set(relations)
        if missing_relations:
            raise ValueError(f"指标查询缺少必要 Traverse 关系：{sorted(missing_relations)}")

        ctes = [self._task_scope_cte()]
        if needs_bom:
            ctes.append(self._bom_material_cte())
        if needs_issue:
            ctes.append(self._issue_aggregate_cte())
        if needs_return:
            ctes.append(self._return_aggregate_cte())
        if needs_report:
            ctes.append(self._report_aggregate_cte())
        ctes.append(
            self._metric_base_cte(
                needs_bom=needs_bom,
                needs_issue=needs_issue,
                needs_return=needs_return,
                needs_report=needs_report,
            )
        )

        final_cte = "metric_base"
        if "ratedOutputQty" in metric_closure:
            ctes.append(self._rated_output_cte(final_cte))
            final_cte = "rated_output"
        if "signedDifferenceQty" in metric_closure:
            ctes.append(self._difference_cte(final_cte))
            final_cte = "difference_result"
        if "signedDifferenceRate" in metric_closure:
            ctes.append(self._difference_rate_cte(final_cte))
            final_cte = "difference_rate_result"
        if "severityRate" in metric_closure:
            ctes.append(self._severity_cte(final_cte))
            final_cte = "severity_result"

        select_items = [self._metric_dimension_expression(value) for value in dimensions]
        select_items.extend(
            f"metric.{METRIC_SQL_ALIASES[metric_id]} AS {METRIC_SQL_ALIASES[metric_id]}"
            for metric_id in requested_metrics
        )
        needs_product = any(value.startswith("Product.") for value in dimensions)
        product_join = ""
        if needs_product:
            product = self.mapping["entities"]["Product"]
            product_join = (
                f"\nLEFT JOIN {self._table(product['table'])} AS product"
                "\n  ON product.id = metric.material_id"
                f"\n AND product.{product['active_filter']['column']} = 0"
            )

        filter_sql = self._metric_filter_clause(query_ir)
        sort_sql = self._sort_clause(
            query_ir,
            {**self.mapping["output_fields"], **METRIC_SQL_ALIASES},
        )
        return (
            "WITH\n"
            + ",\n".join(ctes)
            + "\nSELECT\n    "
            + ",\n    ".join(select_items)
            + f"\nFROM {final_cte} AS metric"
            + product_join
            + filter_sql
            + sort_sql
            + "\nLIMIT %(limit)s;"
        )

    def _task_scope_cte(self) -> str:
        """生成计划和工序任务的基础作用域 CTE。"""

        plan = self.mapping["entities"]["ProductionPlan"]
        task = self.mapping["entities"]["ProcessTask"]
        return f"""task_scope AS (
    SELECT
        p.{plan['properties']['sourceId']} AS plan_id,
        p.{plan['properties']['planNo']} AS plan_code,
        p.{plan['properties']['name']} AS plan_name,
        p.{plan['properties']['plannedQty']} AS planned_quantity,
        p.{plan['properties']['statusCode']} AS plan_status_code,
        p.{plan['properties']['planStartTime']} AS plan_start_time,
        p.{plan['properties']['planEndTime']} AS plan_end_time,
        t.{task['properties']['sourceId']} AS task_id,
        t.{task['properties']['taskNo']} AS task_code,
        t.{task['properties']['plannedQty']} AS task_planned_quantity,
        t.{task['properties']['statusCode']} AS task_status_code,
        t.{task['properties']['startTime']} AS task_start_time,
        t.{task['properties']['finishTime']} AS task_finish_time,
        t.{task['properties']['outputProductId']} AS output_product_id
    FROM {self._table(plan['table'])} AS p
    JOIN {self._table(task['table'])} AS t
      ON t.{task['properties']['planId']} = p.{plan['properties']['sourceId']}
     AND t.{task['active_filter']['column']} = 0
    WHERE p.{plan['active_filter']['column']} = 0
      AND p.{plan['properties']['sourceId']} = %(plan_id)s
)"""

    def _bom_material_cte(self) -> str:
        """按计划 BOM 的任务输出节点查找直接子物料和标准用量。"""

        bom = self.mapping["entities"]["PlanBOMItem"]
        table = self._table(bom["table"])
        return f"""bom_material AS (
    SELECT DISTINCT
        task.task_id,
        child.{bom['properties']['productId']} AS material_id,
        child.{bom['properties']['standardUsage']} AS standard_usage
    FROM task_scope AS task
    JOIN {table} AS output_node
      ON output_node.{bom['properties']['planId']} = task.plan_id
     AND output_node.{bom['properties']['productId']} = task.output_product_id
     AND output_node.{bom['active_filter']['column']} = 0
    JOIN {table} AS child
      ON child.{bom['properties']['planId']} = task.plan_id
     AND child.{bom['properties']['parentId']} = output_node.{bom['properties']['sourceId']}
     AND child.{bom['active_filter']['column']} = 0
)"""

    def _issue_aggregate_cte(self) -> str:
        """生成按工序任务和物料聚合的领料指标 CTE。"""

        source = self.mapping["entities"]["MaterialIssue"]
        return f"""issue_agg AS (
    SELECT
        {source['properties']['taskId']} AS task_id,
        {source['properties']['materialId']} AS material_id,
        SUM(COALESCE({source['properties']['quantity']}, 0)) AS gross_issued_qty
    FROM {self._table(source['header_table'])} AS {source['header_alias']}
    JOIN {self._table(source['detail_table'])} AS {source['detail_alias']}
      ON {source['detail_join']['left']} = {source['detail_join']['right']}
    WHERE {source['active_filters'][0]['alias']}.{source['active_filters'][0]['column']} = 0
      AND {source['active_filters'][1]['alias']}.{source['active_filters'][1]['column']} = 0
    GROUP BY {source['properties']['taskId']}, {source['properties']['materialId']}
)"""

    def _return_aggregate_cte(self) -> str:
        """生成按工序任务和物料聚合的退料指标 CTE。"""

        source = self.mapping["entities"]["MaterialReturn"]
        return f"""return_agg AS (
    SELECT
        {source['properties']['taskId']} AS task_id,
        {source['properties']['materialId']} AS material_id,
        SUM(COALESCE({source['properties']['quantity']}, 0)) AS returned_qty
    FROM {self._table(source['header_table'])} AS {source['header_alias']}
    JOIN {self._table(source['detail_table'])} AS {source['detail_alias']}
      ON {source['detail_join']['left']} = {source['detail_join']['right']}
    WHERE {source['active_filters'][0]['alias']}.{source['active_filters'][0]['column']} = 0
      AND {source['active_filters'][1]['alias']}.{source['active_filters'][1]['column']} = 0
    GROUP BY {source['properties']['taskId']}, {source['properties']['materialId']}
)"""

    def _report_aggregate_cte(self) -> str:
        """生成按工序任务聚合的实际报工指标 CTE。"""

        source = self.mapping["entities"]["ProductionReport"]
        return f"""report_agg AS (
    SELECT
        report.{source['properties']['taskId']} AS task_id,
        SUM(
            COALESCE(report.{source['properties']['selfQualifiedQty']}, 0)
            + COALESCE(report.{source['properties']['selfNonconformingQty']}, 0)
        ) AS actual_reported_qty
    FROM {self._table(source['table'])} AS report
    WHERE report.{source['active_filter']['column']} = 0
    GROUP BY report.{source['properties']['taskId']}
)"""

    def _metric_base_cte(
        self,
        needs_bom: bool,
        needs_issue: bool,
        needs_return: bool,
        needs_report: bool,
    ) -> str:
        """把独立聚合结果在稳定粒度上对齐，避免事实表直接连接造成数量放大。"""

        if needs_bom:
            material_source = "bom_material"
            standard_usage = "material.standard_usage"
        elif needs_issue:
            material_source = "issue_agg"
            standard_usage = "NULL"
        elif needs_return:
            material_source = "return_agg"
            standard_usage = "NULL"
        else:
            material_source = "(SELECT task_id, output_product_id AS material_id FROM task_scope)"
            standard_usage = "NULL"

        issue_select = "COALESCE(issue.gross_issued_qty, 0)" if needs_issue else "NULL"
        return_select = "COALESCE(ret.returned_qty, 0)" if needs_return else "NULL"
        report_select = "COALESCE(report.actual_reported_qty, 0)" if needs_report else "NULL"
        joins = []
        if needs_issue:
            joins.append(
                "LEFT JOIN issue_agg AS issue\n"
                "      ON issue.task_id = task.task_id\n"
                "     AND issue.material_id = material.material_id"
            )
        if needs_return:
            joins.append(
                "LEFT JOIN return_agg AS ret\n"
                "      ON ret.task_id = task.task_id\n"
                "     AND ret.material_id = material.material_id"
            )
        if needs_report:
            joins.append(
                "LEFT JOIN report_agg AS report\n"
                "      ON report.task_id = task.task_id"
            )
        join_sql = "\n    ".join(joins)
        if join_sql:
            join_sql = "\n    " + join_sql
        return f"""metric_base AS (
    SELECT
        task.*,
        material.material_id,
        {standard_usage} AS standard_usage,
        {issue_select} AS gross_issued_qty,
        {return_select} AS returned_qty,
        {report_select} AS actual_reported_qty
    FROM task_scope AS task
    JOIN {material_source} AS material
      ON material.task_id = task.task_id{join_sql}
)"""

    @staticmethod
    def _rated_output_cte(input_cte: str) -> str:
        """生成额定产出指标。"""

        return f"""rated_output AS (
    SELECT
        base.*,
        CASE
            WHEN base.standard_usage IS NULL OR base.standard_usage = 0 THEN NULL
            ELSE base.gross_issued_qty / base.standard_usage
        END AS rated_output_qty
    FROM {input_cte} AS base
)"""

    @staticmethod
    def _difference_cte(input_cte: str) -> str:
        """生成有符号差异量指标。"""

        return f"""difference_result AS (
    SELECT
        base.*,
        base.rated_output_qty - base.actual_reported_qty AS signed_difference_qty
    FROM {input_cte} AS base
)"""

    @staticmethod
    def _difference_rate_cte(input_cte: str) -> str:
        """生成有符号差异率指标并处理除零。"""

        return f"""difference_rate_result AS (
    SELECT
        base.*,
        CASE
            WHEN base.rated_output_qty IS NULL OR base.rated_output_qty = 0 THEN NULL
            ELSE base.signed_difference_qty / base.rated_output_qty
        END AS signed_difference_rate
    FROM {input_cte} AS base
)"""

    @staticmethod
    def _severity_cte(input_cte: str) -> str:
        """生成用于阈值和排序的绝对严重度指标。"""

        return f"""severity_result AS (
    SELECT
        base.*,
        ABS(base.signed_difference_rate) AS severity_rate
    FROM {input_cte} AS base
)"""

    def _simple_dimension_expression(
        self, dimension: str, aliases: dict[str, str]
    ) -> str:
        """把本体属性投影转换为实体表别名和列名。"""

        entity_type, property_name = dimension.split(".", 1)
        entity = self.mapping["entities"][entity_type]
        column = entity["properties"][property_name]
        output_alias = self.mapping["output_fields"][dimension]
        return f"{aliases[entity_type]}.{column} AS {output_alias}"

    def _metric_dimension_expression(self, dimension: str) -> str:
        """把指标查询中的维度转换为最终 CTE 或 Product 表表达式。"""

        output_alias = self.mapping["output_fields"][dimension]
        entity_type, property_name = dimension.split(".", 1)
        if entity_type == "Product":
            if property_name == "sourceId":
                return "metric.material_id AS material_id"
            product = self.mapping["entities"]["Product"]
            return f"product.{product['properties'][property_name]} AS {output_alias}"
        return f"metric.{output_alias} AS {output_alias}"

    def _metric_filter_clause(self, query_ir: dict[str, Any]) -> str:
        """把 Query IR 的指标过滤条件转换为参数化 WHERE。"""

        predicates = []
        for step in query_ir["steps"]:
            if step["op"] != "Filter":
                continue
            for predicate in step["predicates"]:
                metric_column = METRIC_SQL_ALIASES[predicate["metric"]]
                sql_operator = COMPARISON_SQL[predicate["operator"]]
                predicates.append(
                    f"(%({predicate['parameter']})s IS NULL OR "
                    f"metric.{metric_column} {sql_operator} %({predicate['parameter']})s)"
                )
        return "" if not predicates else "\nWHERE " + "\n  AND ".join(predicates)

    def _sort_clause(
        self, query_ir: dict[str, Any], allowed_aliases: dict[str, str]
    ) -> str:
        """把一个受白名单保护的 Sort 步骤转换为 ORDER BY。"""

        sort_step = next((step for step in query_ir["steps"] if step["op"] == "Sort"), None)
        if sort_step is None:
            return ""
        sort_key = sort_step["by"]
        if sort_key not in allowed_aliases:
            raise ValueError(f"没有可编译的排序字段：{sort_key}")
        return f"\nORDER BY {allowed_aliases[sort_key]} {sort_step['direction']}"

    def _compile_trace(self, step: dict[str, Any]) -> dict[str, Any]:
        """解释每个 Query IR 算子在物理规划阶段做了什么。"""

        operator = step["op"]
        detail: dict[str, Any] = {"stepId": step["stepId"], "op": operator}
        if operator == "Scan":
            entity = self.mapping["entities"][step["entityType"]]
            detail["physical"] = f"扫描 {self.mapping['database']}.{entity['table']}"
        elif operator == "Traverse":
            relation = self.mapping["relations"][step["relation"]]
            detail["physical"] = {
                "relation": step["relation"],
                "from": relation["from"],
                "to": relation["to"],
                "joins": relation["joins"],
            }
        elif operator in {"Aggregate", "ComputeMetric"}:
            detail["physical"] = {
                metric: self.mapping["metrics"][metric] for metric in step["metrics"]
            }
        elif operator == "Filter":
            detail["physical"] = "参数化 WHERE 指标条件"
        elif operator == "Sort":
            detail["physical"] = f"ORDER BY {step['by']} {step['direction']}"
        elif operator == "Project":
            detail["physical"] = "生成白名单 SELECT 投影"
        elif operator == "Limit":
            detail["physical"] = "LIMIT %(limit)s"
        return detail

    def _metric_closure(self, metric_ids: set[str]) -> set[str]:
        """递归展开物理编译需要的指标依赖。"""

        closure = set(metric_ids)
        pending = list(metric_ids)
        while pending:
            metric_id = pending.pop()
            metric = self.mapping["metrics"].get(metric_id, {})
            for dependency in metric.get("dependencies", []):
                if "." not in dependency and dependency not in closure:
                    closure.add(dependency)
                    pending.append(dependency)
        return closure

    def _table(self, table_name: str) -> str:
        """使用固定 Mapping 中的库名和表名生成限定表引用。"""

        return f"`{self.mapping['database']}`.`{table_name}`"
