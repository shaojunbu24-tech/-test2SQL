"""编排自然语言、Query IR、校验、SQL 编译和只读执行的完整链路。"""

from __future__ import annotations

import os
from time import perf_counter
from typing import Any

from .compiler import QueryIRCompiler
from .config import load_local_env
from .database import ReadOnlyMySQLExecutor
from .ontology import OntologyRegistry
from .planner import QueryIRPlanner
from .result_answerer import QueryResultAnswerer
from .validator import QueryIRValidator
from .request_router import RequestRouter
from .entity_resolver import EntityResolver
from .tasks import expand_material_gap
from .conversation import rewrite_question


class MaterialQueryPipeline:
    """对 CLI 和 Streamlit 暴露同一个可追踪查询入口。"""

    def __init__(self, model_name: str | None = None) -> None:
        load_local_env()
        self.registry = OntologyRegistry()
        self.model_name = model_name or os.getenv("OPENAI_MODEL")
        self.validator = QueryIRValidator(self.registry)
        self.compiler = QueryIRCompiler(self.registry)
        self.executor = ReadOnlyMySQLExecutor(self.registry.mapping["database"])

    def run(
        self,
        question: str,
        execute: bool = False,
        answer_with_llm: bool = False,
        candidate_query_ir: dict[str, Any] | None = None,
        conversation_history: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """运行完整链路；candidate_query_ir 用于不调用模型的离线调试。"""

        trace: dict[str, Any] = {
            "input": {"natural_language": question},
            "ontology": {
                "summary": self.registry.summary(),
                "planner_context": self.registry.planner_context(),
            },
            "timings_ms": {},
        }

        context = rewrite_question(question, conversation_history) if candidate_query_ir is None else None
        if context is not None:
            trace["context"] = context
            trace["input"]["rewritten_question"] = context["effective_question"]
            if context["mode"] in {"CLARIFY", "RESET"}:
                return self._pending(trace, context["clarification"])
        effective_question = context["effective_question"] if context else question

        started = perf_counter()
        if candidate_query_ir is None:
            request = RequestRouter(self.registry, self.model_name).parse(
                effective_question, context["recent_turns"]
            )
            locked_plan_id = context["explicit_source_id"] or context["inherited_plan_id"]
            if locked_plan_id is not None:
                expected_selector = {"entityType": "ProductionPlan", "property": "sourceId",
                                     "operator": "EQ", "value": locked_plan_id}
                if request["selector"] != expected_selector:
                    trace["selector_guard"] = {"model_selector": request["selector"],
                                               "enforced_selector": expected_selector,
                                               "reason": "本轮显式ID或最近成功轮次的规范ID优先"}
                    request["selector"] = expected_selector
            trace["request"] = request
            trace["timings_ms"]["request_parsing"] = self._elapsed_ms(started)
            selector = request["selector"]
            plan_id = None
            if selector:
                started = perf_counter()
                if execute:
                    resolution = EntityResolver(self.registry, self.executor).resolve(selector)
                elif selector["property"] == "sourceId":
                    resolution = {"status": "UNVERIFIED", "selector": selector,
                                  "sourceId": selector["value"], "physical_property": "produce_plan.id"}
                else:
                    resolution = {"status": "REQUIRES_LOOKUP", "selector": selector}
                trace["resolution"] = resolution
                trace["timings_ms"]["entity_resolution"] = self._elapsed_ms(started)
                if resolution["status"] not in {"RESOLVED", "UNVERIFIED"}:
                    reasons = {"AMBIGUOUS": "匹配到多个生产计划，请从候选中选择后重新查询。",
                               "NOT_FOUND": "未找到符合该条件的有效生产计划，请检查编号或名称。",
                               "REQUIRES_LOOKUP": "按编号或名称定位需要勾选真实只读查询。"}
                    return self._pending(trace, reasons[resolution["status"]])
                plan_id = resolution["sourceId"]
            # 目标不明确也可以先确认用户明确指定的实体；仅定位，不生成 Query IR/SQL。
            if request["route"] == "clarify":
                return self._pending(trace, request["clarification"] or "请明确要分析的内容。")
            started = perf_counter()
            if request["route"] == "analyze_material_gap":
                if plan_id is None:
                    return self._pending(trace, "料差分析需要指定一个生产计划ID、编号或名称。")
                candidate_query_ir = expand_material_gap(self.registry, effective_question, plan_id, request)
                trace["task"] = {"id": "analyze_material_gap", **self.registry.tasks["analyze_material_gap"],
                                 "arguments": candidate_query_ir["parameters"], "comparison": request["comparison"]}
                source = "REGISTERED_TASK"
            else:
                planner = QueryIRPlanner(self.registry, self.model_name)
                grounded_question = (effective_question + f"\n已解析的生产计划主键 plan_id={plan_id}。"
                                     "只使用此已解析主键，不要从原名称或编号中重新提取数字。")
                candidate_query_ir = planner.plan(grounded_question)
                # 解析器确认的定位条件具有优先级，模型不能在规划时换一个计划。
                candidate_query_ir["parameters"]["plan_id"] = plan_id
                candidate_query_ir["goal"] = effective_question
                source = "LANGCHAIN_LLM"
                # 只修复一次不合法的动态计划，并保留原计划和校验原因用于审计。
                try:
                    self.validator.validate(candidate_query_ir)
                except ValueError as error:
                    trace["planning_repair"] = {"error": str(error), "original_ir": candidate_query_ir}
                    candidate_query_ir = planner.plan(grounded_question + "\n上次校验失败，请修正：" + str(error))
                    candidate_query_ir["parameters"]["plan_id"] = plan_id
                    candidate_query_ir["goal"] = effective_question
        else:
            source = "OFFLINE_QUERY_IR"
        trace["timings_ms"]["planning"] = self._elapsed_ms(started)
        trace["planning"] = {
            "source": source,
            "model": self.model_name if source == "LANGCHAIN_LLM" else None,
            "candidate_query_ir": candidate_query_ir,
        }

        started = perf_counter()
        validation = self.validator.validate(candidate_query_ir)
        trace["timings_ms"]["validation"] = self._elapsed_ms(started)
        trace["validation"] = validation
        trace["ontology"]["query_hits"] = self.registry.query_hits(candidate_query_ir)

        started = perf_counter()
        compiled = self.compiler.compile(candidate_query_ir)
        trace["timings_ms"]["compilation"] = self._elapsed_ms(started)
        trace["compilation"] = {
            "mode": compiled.mode,
            "mapping_version": compiled.mapping_version,
            "operator_mapping_trace": compiled.trace,
            "sql": compiled.sql,
            "parameters": compiled.parameters,
        }

        if execute:
            started = perf_counter()
            trace["execution"] = self.executor.execute(compiled.sql, compiled.parameters)
            trace["timings_ms"]["execution"] = self._elapsed_ms(started)
            self._answer_after_execution(trace, answer_with_llm)
        else:
            trace["execution"] = {
                "status": "NOT_EXECUTED",
                "reason": "使用 --execute 或在界面勾选真实执行后才连接数据库。",
            }
            trace["answer"] = {
                "status": "NOT_AVAILABLE",
                "reason": "尚未执行 SQL，因此没有可供模型回答的实际结果。",
            }

        trace["timings_ms"]["total"] = round(
            sum(trace["timings_ms"].values()), 2
        )
        return trace

    def _pending(self, trace, reason):
        """返回可展示的未完成定位状态，禁止在歧义条件下猜测并继续查询。"""
        trace["status"] = "NEEDS_INPUT"
        trace["execution"] = {"status": "NOT_EXECUTED", "reason": reason}
        trace["answer"] = {"status": "NOT_AVAILABLE", "reason": reason}
        trace["timings_ms"]["total"] = round(sum(trace["timings_ms"].values()), 2)
        return trace

    def _answer_after_execution(
        self, trace: dict[str, Any], answer_with_llm: bool) -> None:
        """仅在只读 SQL 成功后调用模型，失败不影响原始查询结果展示。"""

        execution = trace["execution"]
        if execution["status"] != "SUCCESS":
            trace["answer"] = {
                "status": "NOT_AVAILABLE",
                "reason": "SQL 未成功执行，不能基于不完整结果生成回答。",
            }
            return
        if not answer_with_llm:
            trace["answer"] = {
                "status": "SKIPPED",
                "reason": "本次运行未启用大模型结果解读。",
            }
            return
        started = perf_counter()
        try:
            answerer = QueryResultAnswerer(self.model_name)
            trace["answer"] = {
                "status": "SUCCESS",
                "source": "LANGCHAIN_LLM",
                "model": answerer.model_name,
                "based_on_row_count": execution["row_count"],
                "text": answerer.answer(
                    trace["input"]["natural_language"],
                    trace["planning"]["candidate_query_ir"]["goal"],
                    execution["rows"],
                    {"parameters": trace["compilation"]["parameters"],
                     "steps": trace["planning"]["candidate_query_ir"]["steps"],
                     "task": trace.get("task"), "row_count_is_returned_rows_only": True},
                ),
            }
        except Exception as error:
            trace["answer"] = {
                "status": "FAILED",
                "reason": str(error),
            }
        trace["timings_ms"]["answering"] = self._elapsed_ms(started)

    @staticmethod
    def _elapsed_ms(started: float) -> float:
        """把一个阶段的耗时转换成便于展示的毫秒。"""

        return round((perf_counter() - started) * 1000, 2)
