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
from .validator import QueryIRValidator


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
        candidate_query_ir: dict[str, Any] | None = None,
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

        started = perf_counter()
        if candidate_query_ir is None:
            planner = QueryIRPlanner(self.registry, self.model_name)
            candidate_query_ir = planner.plan(question)
            source = "LANGCHAIN_LLM"
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
        else:
            trace["execution"] = {
                "status": "NOT_EXECUTED",
                "reason": "使用 --execute 或在界面勾选真实执行后才连接数据库。",
            }

        trace["timings_ms"]["total"] = round(
            sum(trace["timings_ms"].values()), 2
        )
        return trace

    @staticmethod
    def _elapsed_ms(started: float) -> float:
        """把一个阶段的耗时转换成便于展示的毫秒。"""

        return round((perf_counter() - started) * 1000, 2)
