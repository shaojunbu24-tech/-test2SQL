"""对模型生成的 Query IR 做结构、类型、关系和指标依赖校验。"""

from copy import deepcopy
from typing import Any

from jsonschema import Draft202012Validator

from .ontology import OntologyRegistry


class QueryIRValidator:
    """维护 Query IR 符号表，拒绝无法由本体和映射支持的查询计划。"""

    def __init__(self, registry: OntologyRegistry) -> None:
        self.registry = registry
        self.schema_validator = Draft202012Validator(registry.query_ir_schema)

    def validate(self, query_ir: dict[str, Any]) -> dict[str, Any]:
        """校验 Query IR，并返回可用于追踪的类型推导和依赖结果。"""

        schema_errors = sorted(
            self.schema_validator.iter_errors(query_ir), key=lambda error: list(error.path)
        )
        if schema_errors:
            error = schema_errors[0]
            path = ".".join(str(part) for part in error.absolute_path) or "$"
            raise ValueError(f"Query IR Schema 校验失败：{path}: {error.message}")

        scan_steps = [step for step in query_ir["steps"] if step["op"] == "Scan"]
        if len(scan_steps) != 1:
            raise ValueError("当前 MVP 必须且只能包含一个 Scan 步骤。")

        parameters = query_ir["parameters"]
        scan_step = scan_steps[0]
        has_plan_filter = bool(scan_step["filters"])
        if parameters["plan_id"] is None and has_plan_filter:
            raise ValueError("plan_id=null 时 Scan 不能引用 plan_id 过滤。")
        if parameters["plan_id"] is not None and not has_plan_filter:
            raise ValueError("用户给出 plan_id 时 Scan 必须按 sourceId 精确过滤。")
        if parameters["plan_id"] is None:
            requested_metrics = {
                metric
                for step in query_ir["steps"]
                for metric in step.get("metrics", [])
            }
            if requested_metrics != {"productionPlanCount"}:
                raise ValueError("当前 MVP 只允许 productionPlanCount 使用无 plan_id 扫描。")
        if parameters["threshold"] is not None:
            threshold_predicates = [
                predicate
                for step in query_ir["steps"]
                if step["op"] == "Filter"
                for predicate in step["predicates"]
            ]
            if not threshold_predicates:
                raise ValueError("Query IR 提供了 threshold，但没有 Filter 步骤使用它。")
            if not any(
                predicate["metric"] == "severityRate"
                for predicate in threshold_predicates
            ):
                raise ValueError("MVP 的异常阈值必须过滤绝对严重度 severityRate。")

        symbols: dict[str, dict[str, Any]] = {}
        step_trace: list[dict[str, Any]] = []
        relation_path: list[str] = []
        available_metrics: set[str] = set()
        seen_step_ids: set[str] = set()
        seen_binds: set[str] = set()

        for index, step in enumerate(query_ir["steps"], start=1):
            expected_step_id = f"s{index}"
            if step["stepId"] != expected_step_id:
                raise ValueError(
                    f"步骤必须连续编号：第 {index} 步应为 {expected_step_id}，实际为 {step['stepId']}。"
                )
            if step["stepId"] in seen_step_ids:
                raise ValueError(f"重复的 stepId：{step['stepId']}")
            if step["bind"] in seen_binds:
                raise ValueError(f"重复的 bind：{step['bind']}")
            seen_step_ids.add(step["stepId"])
            seen_binds.add(step["bind"])

            input_names = step.get("input", [])
            if isinstance(input_names, str):
                input_names = [input_names]
            for input_name in input_names:
                if input_name not in symbols:
                    raise ValueError(
                        f"{step['stepId']} 引用了尚未生成的输入变量：{input_name}"
                    )

            output_symbol = self._validate_step(step, symbols, available_metrics)
            symbols[step["bind"]] = output_symbol
            available_metrics.update(output_symbol.get("metrics", []))
            if step["op"] == "Traverse":
                relation_path.append(step["relation"])
            step_trace.append(
                {
                    "stepId": step["stepId"],
                    "op": step["op"],
                    "input": step.get("input"),
                    "bind": step["bind"],
                    "outputKind": output_symbol["kind"],
                    "outputEntities": sorted(output_symbol.get("entities", [])),
                    "availableMetrics": sorted(output_symbol.get("metrics", [])),
                }
            )

        if query_ir["steps"][0]["op"] != "Scan":
            raise ValueError("Query IR 第一步必须是 Scan。")
        if query_ir["steps"][-1]["op"] != "Limit":
            raise ValueError("Query IR 最后一步必须是 Limit。")
        if not any(step["op"] == "Project" for step in query_ir["steps"]):
            raise ValueError("Query IR 必须包含 Project，以明确最终返回内容。")

        required_metric_closure = self._metric_closure(available_metrics)
        return {
            "status": "VALID",
            "schema": self.registry.query_ir_schema["$id"],
            "step_count": len(query_ir["steps"]),
            "relation_path": relation_path,
            "metric_dependency_closure": sorted(required_metric_closure),
            "symbol_table": deepcopy(symbols),
            "step_trace": step_trace,
        }

    def _validate_step(
        self,
        step: dict[str, Any],
        symbols: dict[str, dict[str, Any]],
        available_metrics: set[str],
    ) -> dict[str, Any]:
        """按算子类型检查一步，并推导该步骤的输出类型。"""

        operator = step["op"]
        if operator == "Scan":
            entity_type = step["entityType"]
            if entity_type not in self.registry.entity_types:
                raise ValueError(f"未登记的 Scan 实体：{entity_type}")
            return {"kind": "entity", "entities": {entity_type}, "metrics": set()}

        if operator == "Traverse":
            input_symbol = symbols[step["input"]]
            relation_id = step["relation"]
            relation = self.registry.relations.get(relation_id)
            if relation is None or relation_id not in self.registry.mapping["relations"]:
                raise ValueError(f"未登记或没有物理映射的关系：{relation_id}")
            if relation["from"] not in input_symbol.get("entities", set()):
                raise ValueError(
                    f"{relation_id} 必须从 {relation['from']} 出发，"
                    f"但 {step['input']} 的类型是 {sorted(input_symbol.get('entities', []))}。"
                )
            return {
                "kind": "entity",
                "entities": set(input_symbol.get("entities", set())) | {relation["to"]},
                "metrics": set(input_symbol.get("metrics", set())),
            }

        if operator == "Aggregate":
            input_symbol = symbols[step["input"]]
            output_metrics: set[str] = set()
            for metric_id in step["metrics"]:
                metric = self.registry.mapping["metrics"].get(metric_id)
                if metric is None or not metric["kind"].startswith("aggregate"):
                    raise ValueError(f"Aggregate 只能使用已登记的基础聚合指标：{metric_id}")
                if metric["source_entity"] not in input_symbol.get("entities", set()):
                    raise ValueError(
                        f"指标 {metric_id} 需要 {metric['source_entity']}，"
                        f"但输入 {step['input']} 类型不匹配。"
                    )
                if set(step["groupBy"]) != set(metric["grain"]):
                    raise ValueError(
                        f"指标 {metric_id} 的粒度必须是 {metric['grain']}，"
                        f"实际为 {step['groupBy']}。"
                    )
                output_metrics.add(metric_id)
            return {
                "kind": "aggregate",
                "entities": set(step["groupBy"]),
                "metrics": output_metrics,
            }

        if operator == "ComputeMetric":
            input_names = step["input"] if isinstance(step["input"], list) else [step["input"]]
            input_entities: set[str] = set()
            input_metrics: set[str] = set()
            for input_name in input_names:
                input_entities.update(symbols[input_name].get("entities", set()))
                input_metrics.update(symbols[input_name].get("metrics", set()))
            requested_metrics = set(step["metrics"])
            computed_metrics: set[str] = set(input_metrics) | requested_metrics
            for metric_id in step["metrics"]:
                metric = self.registry.mapping["metrics"].get(metric_id)
                if metric is None or metric["kind"] != "derived":
                    raise ValueError(f"ComputeMetric 只能使用已登记的派生指标：{metric_id}")
                for dependency in metric.get("dependencies", []):
                    if "." in dependency:
                        dependency_entity = dependency.split(".", 1)[0]
                        if dependency_entity not in input_entities:
                            raise ValueError(
                                f"指标 {metric_id} 缺少属性依赖对应实体：{dependency_entity}"
                            )
                    elif dependency not in input_metrics and dependency not in requested_metrics:
                        raise ValueError(f"指标 {metric_id} 缺少指标依赖：{dependency}")
            return {
                "kind": "metrics",
                "entities": input_entities,
                "metrics": computed_metrics,
            }

        input_symbol = symbols[step["input"]]
        if operator == "Filter":
            for predicate in step["predicates"]:
                if predicate["metric"] not in input_symbol.get("metrics", set()):
                    raise ValueError(f"Filter 引用了尚未计算的指标：{predicate['metric']}")
            return deepcopy(input_symbol)

        if operator == "Sort":
            sort_key = step["by"]
            allowed_keys = set(input_symbol.get("metrics", set())) | set(
                self.registry.mapping["output_fields"]
            )
            if sort_key not in allowed_keys:
                raise ValueError(f"Sort 引用了不可排序的字段或指标：{sort_key}")
            if "." in sort_key:
                sort_entity = sort_key.split(".", 1)[0]
                if sort_entity not in input_symbol.get("entities", set()):
                    raise ValueError(f"Sort 的输入中不存在实体类型：{sort_entity}")
            return deepcopy(input_symbol)

        if operator == "Project":
            for dimension in step["dimensions"]:
                if dimension not in self.registry.mapping["output_fields"]:
                    raise ValueError(f"Project 引用了未登记维度：{dimension}")
                entity_type = dimension.split(".", 1)[0]
                if entity_type not in input_symbol.get("entities", set()):
                    raise ValueError(f"Project 的输入中不存在实体类型：{entity_type}")
            for metric_id in step["metrics"]:
                if metric_id not in input_symbol.get("metrics", set()):
                    raise ValueError(f"Project 引用了尚未计算的指标：{metric_id}")
            return {
                "kind": "projection",
                "entities": {value.split(".", 1)[0] for value in step["dimensions"]},
                "dimensions": set(step["dimensions"]),
                "metrics": set(step["metrics"]),
            }

        if operator == "Limit":
            if input_symbol["kind"] != "projection":
                raise ValueError("Limit 的输入必须来自 Project 或其后续结果。")
            return deepcopy(input_symbol)

        raise ValueError(f"当前 MVP 尚未实现算子：{operator}")

    def _metric_closure(self, metric_ids: set[str]) -> set[str]:
        """递归展开指标依赖，便于追踪和物理规划。"""

        closure = set(metric_ids)
        pending = list(metric_ids)
        while pending:
            metric_id = pending.pop()
            metric = self.registry.mapping["metrics"].get(metric_id, {})
            for dependency in metric.get("dependencies", []):
                if "." not in dependency and dependency not in closure:
                    closure.add(dependency)
                    pending.append(dependency)
        return closure
