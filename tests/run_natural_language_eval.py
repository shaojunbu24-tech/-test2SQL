"""调用真实模型评估自然语言到 Query IR 的核心场景成功率。"""

import sys
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "src"))

from material_query.pipeline import MaterialQueryPipeline


# expected_relations 只声明场景必须命中的关系，允许模型在有依据时补充其他已登记关系。
CASES = [
    {
        "name": "plan-detail",
        "question": "查询生产计划ID 665的详细信息",
        "expected_mode": "DYNAMIC_ENTITY_SCAN",
        "expected_relations": set(),
    },
    {
        "name": "plan-count",
        "question": "当前有多少生产计划",
        "expected_mode": "DYNAMIC_ENTITY_COUNT",
        "expected_relations": set(),
    },
    {
        "name": "plan-bom",
        "question": "分析生产计划ID 665的对应的BOM",
        "expected_mode": "DYNAMIC_ENTITY_TRAVERSAL",
        "expected_relations": {"HAS_PLAN_BOM_ITEM"},
    },
    {
        "name": "process-tasks",
        "question": "查询生产计划ID 665下的工序任务",
        "expected_mode": "DYNAMIC_ENTITY_TRAVERSAL",
        "expected_relations": {"HAS_PROCESS_TASK"},
    },
    {
        "name": "issued-quantity",
        "question": "查询生产计划ID 665各任务的领料量",
        "expected_mode": "DYNAMIC_METRIC_QUERY",
        "expected_relations": {"HAS_PROCESS_TASK", "HAS_MATERIAL_ISSUE"},
    },
    {
        "name": "returned-quantity",
        "question": "查询生产计划ID 665各任务的退料量",
        "expected_mode": "DYNAMIC_METRIC_QUERY",
        "expected_relations": {"HAS_PROCESS_TASK", "HAS_MATERIAL_RETURN"},
    },
    {
        "name": "reported-quantity",
        "question": "查询生产计划ID 665各任务的实际报工数量",
        "expected_mode": "DYNAMIC_METRIC_QUERY",
        "expected_relations": {"HAS_PROCESS_TASK", "HAS_PRODUCTION_REPORT"},
    },
    {
        "name": "material-gap",
        "question": "分析生产计划ID 665的投料差异，筛选差异率超过5%的记录",
        "expected_mode": "DYNAMIC_METRIC_QUERY",
        "expected_relations": {
            "HAS_PROCESS_TASK",
            "HAS_PLAN_BOM_ITEM",
            "HAS_MATERIAL_ISSUE",
            "HAS_PRODUCTION_REPORT",
        },
    },
]


def main() -> None:
    """逐条运行规划、校验和编译，输出可计数的通过率。"""

    pipeline = MaterialQueryPipeline()
    passed = 0
    for case in CASES:
        try:
            trace = pipeline.run(case["question"], execute=False)
            actual_relations = set(trace["validation"]["relation_path"])
            if trace["compilation"]["mode"] != case["expected_mode"]:
                raise AssertionError(
                    f"模式应为 {case['expected_mode']}，"
                    f"实际为 {trace['compilation']['mode']}"
                )
            if not case["expected_relations"] <= actual_relations:
                raise AssertionError(
                    f"缺少关系 {sorted(case['expected_relations'] - actual_relations)}"
                )
            passed += 1
            print(
                f"PASS {case['name']}: mode={trace['compilation']['mode']}, "
                f"relations={sorted(actual_relations)}"
            )
        except Exception as error:
            print(f"FAIL {case['name']}: {error}")

    print(f"RESULT {passed}/{len(CASES)} = {passed / len(CASES):.0%}")
    if passed != len(CASES):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
