"""解析用户提及的实体、定位属性和任务参数；不让模型猜数据库主键。"""

import json
import os

from jsonschema import validate
from langchain_core.output_parsers import JsonOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI

from .config import require_environment


# 路由契约独立于 Query IR；名称解析成功后，查询层仍使用规范主键。
REQUEST_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["route", "selector", "threshold", "comparison", "limit", "clarification"],
    "properties": {
        "route": {"enum": ["analyze_material_gap", "dynamic_query", "clarify"]},
        "selector": {"oneOf": [{"type": "null"}, {
            "type": "object", "additionalProperties": False,
            "required": ["entityType", "property", "operator", "value"],
            "properties": {
                "entityType": {"const": "ProductionPlan"},
                "property": {"enum": ["sourceId", "planNo", "name"]},
                "operator": {"enum": ["EQ", "CONTAINS"]},
                "value": {"type": ["integer", "string"]},
            },
        }]},
        "threshold": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
        "comparison": {"enum": ["GT", "GTE"]},
        "limit": {"type": "integer", "minimum": 1, "maximum": 200},
        "clarification": {"type": "string"},
    },
}


def validate_request(request):
    """结构校验之外，禁止把名称中的数字当成主键或对编号模糊匹配。"""
    validate(request, REQUEST_SCHEMA)
    selector = request["selector"]
    if selector:
        value = selector["value"]
        if selector["property"] == "sourceId":
            if type(value) is not int or value < 1 or selector["operator"] != "EQ":
                raise ValueError("生产计划 ID 必须是正整数并精确匹配。")
        elif not isinstance(value, str) or not value.strip():
            raise ValueError("计划编号或名称不能为空。")
        if selector["property"] == "planNo" and selector["operator"] != "EQ":
            raise ValueError("计划编号只允许精确匹配。")
    return request


class RequestRouter:
    """通过 LangChain 生成可校验的实体定位条件和任务调用。"""

    def __init__(self, registry, model_name=None):
        env = require_environment(["OPENAI_API_KEY", "OPENAI_BASE_URL"])
        model = ChatOpenAI(model=model_name or os.getenv("OPENAI_MODEL"),
                           api_key=env["OPENAI_API_KEY"], base_url=env["OPENAI_BASE_URL"],
                           temperature=0, max_tokens=1200, timeout=60, max_retries=1)
        prompt = ChatPromptTemplate.from_messages([
            ("system", "解析制造业问数请求，只返回符合契约的JSON。实体定位值只能摘自当前改写后的问题，不能猜ID。"
             "明确生产计划ID或生产计划665这种未标注编号的数字指代使用sourceId整数；"
             "明确说计划编号或计划编码（即使全是数字）使用planNo字符串；SCJH等业务编号也是planNo；"
             "用户明确选择生产计划ID时，以该选择替代后文的旧定位词。"
             "计划名称使用name，明确包含用CONTAINS，其余EQ。名称或编号中的数字不能当ID。"
             "明确要求料差、投料差异分析时使用analyze_material_gap；其他查询用dynamic_query。"
             "只有异常筛选而未给阈值时threshold=0.05；普通料差分析不筛选则threshold=null。"
             "超过/大于用GT，至少/不低于/异常用GTE，百分之五转0.05。limit默认100。"
             "可执行的普通问数包括：单计划详情、该计划的BOM/物料清单、工序任务、领料量、"
             "退料量、报工量，以及有效生产计划总数；这些都使用dynamic_query，不得误判为不支持。"
             "料差/投料差异才使用analyze_material_gap。缺少单计划定位、相对日期、多计划分析、"
             "净耗料或自定义公式等不支持请求用clarify并说明需要的信息或限制。"
             "查询全部有效计划数量允许selector=null。clarification正常时为空。"
             "下面的最近五轮摘要只用于理解当前问题；只可信任已验证的生产计划ID。"
             "当前改写后的问题中明确的新定位词和筛选条件覆盖历史摘要，"
             "不得从失败轮次继承实体，不得沿用旧阈值。"
             "最近五轮摘要：{conversation_context}\n"
             "任务定义：{tasks}\n实体属性：{properties}\n契约：{schema}"),
            ("human", "{question}"),
        ])
        self.context = {"tasks": json.dumps(registry.tasks, ensure_ascii=False),
                        "properties": json.dumps(registry.property_catalog("ProductionPlan", False), ensure_ascii=False),
                        "schema": json.dumps(REQUEST_SCHEMA, ensure_ascii=False)}
        self.chain = prompt | model.bind(response_format={"type": "json_object"}) | JsonOutputParser()

    def parse(self, question, conversation_context=None):
        """输出的是定位条件，具体数据库实例由 EntityResolver 验证。"""
        return validate_request(self.chain.invoke({**self.context, "question": question,
                                                   "conversation_context": json.dumps(
                                                       conversation_context or [], ensure_ascii=False)}))
