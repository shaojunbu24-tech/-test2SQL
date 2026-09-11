"""把完整追踪结果压缩成适合终端阅读的阶段输出。"""

import json
from typing import Any


def print_trace(
    trace: dict[str, Any],
    show_query_ir: bool = False,
    show_sql: bool = False,
    show_context: bool = False,
) -> None:
    """输出自然语言、Query IR、校验、编译和执行五个阶段。"""

    print("\n[1] 自然语言")
    print(trace["input"]["natural_language"])

    print("\n[2] 模型生成的 Query IR")
    query_ir = trace["planning"]["candidate_query_ir"]
    print(
        f"来源={trace['planning']['source']}，目标={query_ir['goal']}，"
        f"参数={json.dumps(query_ir['parameters'], ensure_ascii=False)}"
    )
    if show_query_ir:
        print(json.dumps(query_ir, ensure_ascii=False, indent=2))
    else:
        for step in query_ir["steps"]:
            detail = _step_summary(step)
            print(f"{step['stepId']}  {step['op']:<13} {detail} -> {step['bind']}")

    print("\n[3] Query IR 校验")
    validation = trace["validation"]
    print(
        f"状态={validation['status']}，步骤={validation['step_count']}，"
        f"关系={validation['relation_path']}"
    )
    print(f"指标依赖闭包={validation['metric_dependency_closure']}")

    print("\n[4] 本体到 SQL 映射")
    compilation = trace["compilation"]
    print(
        f"模式={compilation['mode']}，Mapping={compilation['mapping_version']}"
    )
    for item in compilation["operator_mapping_trace"]:
        physical = item.get("physical")
        if isinstance(physical, dict):
            physical_text = json.dumps(physical, ensure_ascii=False)
        else:
            physical_text = str(physical)
        print(f"{item['stepId']} {item['op']} -> {physical_text}")

    print("\n[5] 参数化 SQL")
    if show_sql:
        print(compilation["sql"])
    else:
        print("SQL 已动态生成；使用 --show-sql 展开。")
    print(f"参数={json.dumps(compilation['parameters'], ensure_ascii=False)}")

    print("\n[6] 执行结果")
    execution = trace["execution"]
    if execution["status"] != "SUCCESS":
        print(f"状态={execution['status']}，{execution.get('reason', '')}")
    else:
        entity = execution["grounded_entities"][0]
        print(
            f"状态=SUCCESS，数据库={execution['database']}，"
            f"实体={entity['entityRef']}，返回={execution['row_count']}行"
        )
        for row in execution["rows"]:
            print(json.dumps(row, ensure_ascii=False, default=str))

    if show_context:
        print("\n[7] 提供给模型的本体上下文")
        print(json.dumps(trace["ontology"]["planner_context"], ensure_ascii=False, indent=2))

    print(f"\n耗时(ms)={json.dumps(trace['timings_ms'], ensure_ascii=False)}")


def _step_summary(step: dict[str, Any]) -> str:
    """把一个 Query IR 步骤整理成单行摘要。"""

    operator = step["op"]
    if operator == "Scan":
        return f"{step['entityType']} where sourceId=:plan_id"
    if operator == "Traverse":
        return f"{step['input']} --{step['relation']}-->"
    if operator in {"Aggregate", "ComputeMetric"}:
        return f"{step['input']} metrics={step['metrics']}"
    if operator == "Filter":
        return f"{step['input']} predicates={step['predicates']}"
    if operator == "Sort":
        return f"{step['input']} by={step['by']} {step['direction']}"
    if operator == "Project":
        return f"{step['input']} fields={len(step['dimensions']) + len(step['metrics'])}"
    if operator == "Limit":
        return f"{step['input']} limit=:limit"
    return str(step.get("input", ""))

