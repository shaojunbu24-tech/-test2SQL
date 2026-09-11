"""使用 LangChain 把自然语言规划为只包含本体语言的 Candidate Query IR。"""

from __future__ import annotations

import json
import os
from typing import Any

from langchain_core.output_parsers import JsonOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI

from .config import EXAMPLES_DIR, load_json, require_environment
from .ontology import OntologyRegistry


# 样例只用来教模型 Query IR 语法；每次按问题选相关样例，避免无关长路径诱导规划。
EXAMPLE_ROUTES = [
    ("query-ir-plan-bom.json", ("bom", "物料清单", "标准用量", "单耗")),
    ("query-ir-material-gap.json", ("投料差异", "料差", "差异率", "严重度", "异常")),
    ("query-ir-issued-quantity.json", ("领料", "投料量", "发料", "领用")),
    ("query-ir-returned-quantity.json", ("退料", "退回")),
    ("query-ir-reported-quantity.json", ("报工", "实际产出")),
    ("query-ir-list-tasks.json", ("工序任务", "制程任务")),
    ("query-ir-plan-count.json", ("有多少生产计划", "生产计划数量", "生产计划总数")),
    ("query-ir-plan-detail.json", ("计划详情", "计划详细", "生产计划")),
]


def select_example_names(question: str, maximum: int = 2) -> list[str]:
    """根据明确业务词选择少量相关样例。"""

    normalized = question.lower()
    selected = [
        filename
        for filename, keywords in EXAMPLE_ROUTES
        if any(keyword.lower() in normalized for keyword in keywords)
    ]
    if not selected:
        selected = ["query-ir-plan-detail.json"]
    return selected[:maximum]


class QueryIRPlanner:
    """封装提示词、模型和 JSON 解析器组成的 LangChain 规划链。"""

    def __init__(self, registry: OntologyRegistry, model_name: str | None = None) -> None:
        environment = require_environment(["OPENAI_API_KEY", "OPENAI_BASE_URL"])
        self.registry = registry
        self.model_name = model_name or os.getenv("OPENAI_MODEL")
        if not self.model_name:
            raise ValueError("缺少 OPENAI_MODEL，或请通过 --model 指定模型。")
        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "你是制造业本体查询规划器。请把用户问题规划为 Candidate Query IR。\n"
                    "你只能使用给定的实体、关系、指标和算子；不能输出 SQL、表名、字段名或 JOIN。\n"
                    "必须根据问题自由选择必要关系，不要照抄无关分支。\n"
                    "plan_id 只能来自用户明确给出的数字；未给出时必须为 null；百分比必须转换为 0 到 1。\n"
                    "用户没有给阈值时 threshold=null，没有给条数时 limit=100。\n"
                    "用户说差异率超过阈值或筛选异常时，必须计算并过滤 severityRate；"
                    "只有用户明确要求有符号方向时才过滤 signedDifferenceRate。\n"
                    "步骤必须按执行顺序编号 s1、s2……，每个 input 必须引用之前的 bind。\n"
                    "请只输出符合 JSON Schema 的一个 JSON 对象。\n\n"
                    "本体规划上下文：\n{ontology_context}\n\n"
                    "Candidate Query IR JSON Schema：\n{query_ir_schema}\n\n"
                    "以下是与当前问题最相关的合法样例，只用于理解语法；"
                    "请依据用户问题选择必要步骤：\n"
                    "{example_query_ir}",
                ),
                ("human", "用户问题：{question}"),
            ]
        )
        model = ChatOpenAI(
            model=self.model_name,
            api_key=environment["OPENAI_API_KEY"],
            base_url=environment["OPENAI_BASE_URL"],
            temperature=0,
            max_tokens=5000,
            max_retries=2,
        )
        json_model = model.bind(response_format={"type": "json_object"})
        self.chain = prompt | json_model | JsonOutputParser()

    def plan(self, question: str) -> dict[str, Any]:
        """调用模型并返回已经解析为字典、但尚未通过语义校验的 Query IR。"""

        result = self.chain.invoke(
            {
                "question": question,
                "ontology_context": json.dumps(
                    self.registry.planner_context(), ensure_ascii=False, indent=2
                ),
                "query_ir_schema": json.dumps(
                    self.registry.query_ir_schema, ensure_ascii=False, indent=2
                ),
                "example_query_ir": json.dumps(
                    [
                        load_json(EXAMPLES_DIR / filename)
                        for filename in select_example_names(question)
                    ],
                    ensure_ascii=False,
                    indent=2,
                ),
            }
        )
        if not isinstance(result, dict):
            raise ValueError("模型必须返回 JSON 对象形式的 Candidate Query IR。")
        return result
