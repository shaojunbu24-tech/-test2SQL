"""加载 ontology-v1，并构建只含本体语言的模型规划上下文。"""

from __future__ import annotations

from typing import Any

from .config import CONFIG_DIR, ONTOLOGY_DIR, load_json, load_yaml
from .query_hits import extract_query_hits


# MVP_SUPPORTED_OPERATORS 是这一版编译器已经实现的算子集合。
MVP_SUPPORTED_OPERATORS = [
    "Scan",
    "Traverse",
    "Aggregate",
    "ComputeMetric",
    "Filter",
    "Sort",
    "Project",
    "Limit",
]

# OPERATOR_GUIDE 向模型解释算子的输入和输出，而不暴露 SQL 实现。
OPERATOR_GUIDE = {
    "Scan": "从 ProductionPlan 开始；有 plan_id 时按 sourceId 精确过滤，统计全部有效计划时 filters 为空。",
    "Traverse": "沿 data-model 中登记的关系，从 input 变量到达目标实体。",
    "Aggregate": "按登记 grain 聚合基础指标，例如领料量或报工量。",
    "ComputeMetric": "基于已登记依赖计算派生指标，不能自定义公式。",
    "Filter": "使用 threshold 参数筛选某个已计算指标。",
    "Sort": "按已存在的指标或维度排序。",
    "Project": "声明最终需要返回的本体维度和指标。",
    "Limit": "使用 limit 参数限制返回行数；必须作为最后一步。",
}


class OntologyRegistry:
    """把四类本体模型和 MVP 物理映射组织成只读注册表。"""

    def __init__(self) -> None:
        self.scope = load_yaml(ONTOLOGY_DIR / "scope-model.yaml")
        self.data = load_yaml(ONTOLOGY_DIR / "data-model.yaml")
        self.behavior = load_yaml(ONTOLOGY_DIR / "behavior-model.yaml")
        self.semantic = load_yaml(ONTOLOGY_DIR / "semantic-model.mom-test.yaml")
        self.mapping = load_yaml(CONFIG_DIR / "mapping.yaml")
        self.query_ir_schema = load_json(CONFIG_DIR / "query-ir.schema.json")

    @property
    def entity_types(self) -> dict[str, Any]:
        """返回本体实体类型注册表。"""

        return self.data["entity_types"]

    @property
    def relations(self) -> dict[str, Any]:
        """返回本体关系注册表。"""

        return self.data["relations"]

    @property
    def metrics(self) -> dict[str, Any]:
        """返回 Behavior Model 中的指标注册表。"""

        return self.behavior["metrics"]

    def planner_context(self) -> dict[str, Any]:
        """构造给 LLM Planner 的本体上下文，不包含表名、字段名和 JOIN。"""

        entities = []
        for entity_id, definition in self.entity_types.items():
            if entity_id not in self.mapping["entities"]:
                continue
            entities.append(
                {
                    "id": entity_id,
                    "label": definition.get("label"),
                    "properties": sorted(definition.get("properties", {}).keys()),
                }
            )
        relations = []
        for relation_id, definition in self.relations.items():
            if relation_id in self.mapping["relations"]:
                relations.append(
                    {
                        "id": relation_id,
                        "from": definition["from"],
                        "to": definition["to"],
                        "cardinality": definition.get("cardinality"),
                    }
                )
        metrics = []
        for metric_id, mapping_definition in self.mapping["metrics"].items():
            behavior_definition = self.metrics.get(metric_id, {})
            metrics.append(
                {
                    "id": metric_id,
                    "label": behavior_definition.get("label", metric_id),
                    "kind": mapping_definition["kind"],
                    "sourceEntity": mapping_definition.get("source_entity"),
                    "dependencies": mapping_definition.get("dependencies", []),
                    "grain": mapping_definition.get("grain", []),
                }
            )
        return {
            "domain": self.scope["domain"],
            "entities": entities,
            "relations": relations,
            "metrics": metrics,
            "operators": [
                {"id": operator, "meaning": OPERATOR_GUIDE[operator]}
                for operator in MVP_SUPPORTED_OPERATORS
            ],
            "projectableDimensions": sorted(self.mapping["output_fields"].keys()),
            "planningRules": [
                "查询从 ProductionPlan 开始；用户给出计划 ID 时必须用 plan_id 精确过滤。",
                "用户询问生产计划总数时 plan_id=null、Scan.filters=[]，并 Aggregate productionPlanCount。",
                "Sort.by 只能使用 projectableDimensions 中的字段或已计算指标。",
                "只加入用户问题和指标依赖真正需要的关系分支。",
                "基础指标使用 Aggregate，派生指标使用 ComputeMetric。",
                "用户说差异率超过某阈值或异常筛选时，默认计算并过滤 severityRate；只有明确要求正负方向时才过滤 signedDifferenceRate。",
                "每个步骤只能引用前面已经 bind 的变量。",
                "最终必须经过 Project，并以 Limit 结束。",
                "不得输出 SQL、表名、字段名、JOIN 或未登记 ID。",
            ],
        }

    def query_hits(self, query_ir: dict[str, Any]) -> dict[str, Any]:
        """从本次 Query IR 中提取实际使用的本体元素。

        这里的“命中”表示 Query IR 真正引用了该实体、关系、指标或算子，
        不是规划器上下文中所有可用本体的列表。
        """

        return extract_query_hits(self, query_ir)

    def summary(self) -> dict[str, Any]:
        """返回适合 CLI 和界面展示的本体概览。"""

        return {
            "ontology_version": self.data.get("version"),
            "mapping_version": self.mapping.get("version"),
            "database": self.mapping.get("database"),
            "entity_count": len(self.entity_types),
            "executable_entity_count": len(self.mapping["entities"]),
            "relation_count": len(self.relations),
            "executable_relation_count": len(self.mapping["relations"]),
            "metric_count": len(self.mapping["metrics"]),
            "supported_operators": MVP_SUPPORTED_OPERATORS,
        }

    def overview_context(self) -> dict[str, Any]:
        """返回全部逻辑本体，并标记哪些关系已有 MVP 物理映射。"""

        planner_context = self.planner_context()
        entities = []
        for entity_id, definition in self.entity_types.items():
            entities.append(
                {
                    "id": entity_id,
                    "label": definition.get("label"),
                    "properties": sorted(definition.get("properties", {}).keys()),
                    "mvpExecutable": entity_id in self.mapping["entities"],
                }
            )
        relations = []
        for relation_id, definition in self.relations.items():
            relations.append(
                {
                    "id": relation_id,
                    "from": definition["from"],
                    "to": definition["to"],
                    "cardinality": definition.get("cardinality"),
                    "mvpExecutable": relation_id in self.mapping["relations"],
                }
            )
        return {
            "entities": entities,
            "relations": relations,
            "metrics": planner_context["metrics"],
        }
