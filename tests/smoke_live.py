"""显式运行的在线冒烟测试；默认只读数据库，--with-llm 才测试真实自然语言链路。"""

import argparse
import json
import sys
from pathlib import Path
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from material_query.pipeline import MaterialQueryPipeline
from material_query.entity_resolver import EntityResolver
from material_query.config import load_json


def main():
    """打印紧凑结果；可选保存完整追踪，方便逐阶段检查，不保存环境密钥。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--with-llm", action="store_true")
    parser.add_argument("--output", type=Path, help="保存本地追踪文件（包含业务数据，请勿公开）")
    args = parser.parse_args()
    pipeline = MaterialQueryPipeline()
    selector = {"entityType": "ProductionPlan", "property": "sourceId", "operator": "EQ", "value": 665}
    resolved = EntityResolver(pipeline.registry, pipeline.executor).resolve(selector)
    assert resolved["status"] == "RESOLVED", "测试计划665不存在，请先调整测试数据"
    plan_no = resolved["candidates"][0]["code"]
    traces = []
    questions = ["当前有多少有效生产计划", "查询生产计划ID 665的详细信息",
                 "查询生产计划ID 665的BOM", "查询生产计划ID 665的工序任务",
                 "分析生产计划ID 665的料差，不筛选阈值", "分析生产计划ID 665的料差，差异率超过5%",
                 f"查询生产计划编号{plan_no}的详细信息", "查询名称包含MRP的生产计划的详细信息"]
    fixtures = ["query-ir-plan-count.json", "query-ir-plan-detail.json", "query-ir-plan-bom.json",
                "query-ir-list-tasks.json"]
    print("模式：" + ("真实DeepSeek + mom-test" if args.with_llm else "只读数据库；请求/IR使用固定测试输入，不代表模型端到端通过"))
    for index, question in enumerate(questions):
        if args.with_llm:
            trace = pipeline.run(question, execute=True, answer_with_llm=True)
        elif index < 4:
            trace = pipeline.run(question, execute=True, candidate_query_ir=load_json(PROJECT / "examples" / fixtures[index]))
        else:
            # 固定输入替代模型解析，仅用于验证其后的解析器、任务、编译器和执行器。
            request = {"route": "analyze_material_gap", "selector": selector,
                       "threshold": 0.05 if index == 5 else None, "comparison": "GT", "limit": 100, "clarification": ""}
            if index == 6:
                request["route"] = "dynamic_query"
                request["selector"] = {**selector, "property": "planNo", "value": plan_no}
            if index == 7:
                request["selector"] = {**selector, "property": "name", "operator": "CONTAINS", "value": "MRP"}
            with patch("material_query.pipeline.RequestRouter") as router, patch("material_query.pipeline.QueryIRPlanner") as planner:
                router.return_value.parse.return_value = request
                planner.return_value.plan.return_value = load_json(PROJECT / "examples/query-ir-plan-detail.json")
                trace = pipeline.run(question, execute=True)
            trace["test_note"] = "测试替身提供请求；非真实自然语言解析测试"
            if trace.get("planning", {}).get("source") == "LANGCHAIN_LLM":
                trace["planning"]["source"] = "TEST_FIXTURE"
        traces.append(trace)
        execution = trace["execution"]
        print(json.dumps({"question": question, "status": execution["status"],
                          "resolution": trace.get("resolution", {}).get("status"),
                          "source": trace.get("planning", {}).get("source"),
                          "rows": execution.get("row_count"), "sample": execution.get("rows", [])[:1],
                          "answer": trace.get("answer", {}).get("status")}, ensure_ascii=False, default=str), flush=True)
        assert execution["status"] == "SUCCESS" or trace.get("status") == "NEEDS_INPUT"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(traces, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


if __name__ == "__main__":
    main()
