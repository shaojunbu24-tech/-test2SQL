"""将注册分析任务的参数展开为经过验证的本体查询步骤。"""

from .config import EXAMPLES_DIR, load_json


def expand_material_gap(registry, question, plan_id, request):
    """固定业务步骤，自由绑定计划ID、阈值、比较符和返回条数。"""
    task = registry.tasks["analyze_material_gap"]
    ir = load_json(EXAMPLES_DIR / task["recipe"])
    ir["goal"] = question
    ir["parameters"] = {"plan_id": plan_id, "threshold": request["threshold"], "limit": request["limit"]}
    if request["threshold"] is None:
        # 不筛选时删除 Filter 并修复消费其结果的 Sort，保留零分母行供解释。
        filtered = next(step for step in ir["steps"] if step["op"] == "Filter")
        ir["steps"] = [step for step in ir["steps"] if step is not filtered]
        for step in ir["steps"]:
            if step.get("input") == filtered["bind"]:
                step["input"] = filtered["input"]
    else:
        for step in ir["steps"]:
            if step["op"] == "Filter":
                step["predicates"][0]["operator"] = request["comparison"]
    for index, step in enumerate(ir["steps"], 1):
        step["stepId"] = f"s{index}"
    return ir
