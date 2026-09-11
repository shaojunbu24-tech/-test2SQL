"""从 Query IR 提取本次查询实际使用的本体元素。"""

from __future__ import annotations

from typing import Any


def extract_query_hits(registry: Any, query_ir: dict[str, Any]) -> dict[str, Any]:
    """返回 Query IR 命中的实体、关系、指标和算子。

    这是无状态函数，只使用注册表已有的数据属性；即使 Streamlit
    还缓存着旧版 OntologyRegistry 实例，也不需要实例拥有新方法。
    """

    entity_ids: list[str] = []
    relation_ids: list[str] = []
    metric_ids: list[str] = []
    operator_ids: list[str] = []

    def append_once(values: list[str], value: str | None) -> None:
        """按 Query IR 中的首次出现顺序去重。"""

        if value and value not in values:
            values.append(value)

    for step in query_ir["steps"]:
        operator = step["op"]
        append_once(operator_ids, operator)

        if operator == "Scan":
            append_once(entity_ids, step["entityType"])

        if operator == "Traverse":
            relation_id = step["relation"]
            append_once(relation_ids, relation_id)
            relation = registry.relations.get(relation_id, {})
            append_once(entity_ids, relation.get("from"))
            append_once(entity_ids, relation.get("to"))

        for metric_id in step.get("metrics", []):
            append_once(metric_ids, metric_id)

        for predicate in step.get("predicates", []):
            append_once(metric_ids, predicate.get("metric"))

        if operator == "Sort" and step.get("by") in registry.mapping["metrics"]:
            append_once(metric_ids, step["by"])

        for dimension in step.get("dimensions", []):
            append_once(entity_ids, dimension.split(".", 1)[0])

    return {
        "entities": [
            {
                "id": entity_id,
                "label": registry.entity_types.get(entity_id, {}).get("label", entity_id),
            }
            for entity_id in entity_ids
        ],
        "relations": [
            {
                "id": relation_id,
                "from": registry.relations[relation_id]["from"],
                "to": registry.relations[relation_id]["to"],
            }
            for relation_id in relation_ids
        ],
        "metrics": [
            {
                "id": metric_id,
                "label": registry.metrics.get(metric_id, {}).get("label", metric_id),
            }
            for metric_id in metric_ids
        ],
        "operators": operator_ids,
    }
