"""把最近五轮的已验证上下文压缩成当前查询可用的独立问题。"""

from __future__ import annotations

import re
from typing import Any


MAX_CONTEXT_TURNS = 5

# 明确的新定位词优先于历史计划；没有定位词的追问才允许继承规范 ID。
EXPLICIT_PLAN = re.compile(
    r"(?:生产计划|计划)\s*(?:ID|id)\s*[:：#]?\s*\d{1,10}"
    r"|(?:生产计划|计划)\s*(?:编号|编码|名称)\s*[:：#]?\s*[^\s，。；]{1,40}"
    r"|(?:生产计划|计划)\s*[:：#]?\s*\d{1,10}"
)
EXPLICIT_SOURCE_ID = re.compile(
    r"(?:生产计划|计划)\s*(?:ID|id)\s*[:：#]?\s*(\d{1,10})"
    r"|(?:生产计划|计划)\s*[:：#]?\s*(\d{1,10})"
)
GLOBAL_QUERY = re.compile(
    r"(?:全部|所有|总共|总数|当前有多少)\s*(?:有效)?\s*(?:生产计划|计划)"
    r"|(?:上个月|本月|去年|今年)"
)
RESET_QUERY = re.compile(r"^(?:清空上下文|重新开始|开始新话题|重置对话)")
UNKNOWN_SCOPE = re.compile(r"(?:换一个|另一个|其他计划|不同计划)")
REFINEMENT = re.compile(r"(?:只看|改成|换成|筛选|超过|大于|至少|不低于|继续|同样条件)")
DOMAIN_TARGET = re.compile(r"(?:BOM|物料清单|工序|领料|退料|报工|料差|投料差异|计划详情|详细信息)", re.I)


def _previous_target(anchor: dict[str, Any]) -> str:
    """沿用分析方向而不沿用旧的计划号、阈值或条数。"""

    if anchor["route"] == "analyze_material_gap":
        return "分析投料差异"
    goal = anchor["goal"]
    for keyword, target in (("BOM", "查询BOM"), ("物料清单", "查询BOM"),
                            ("工序", "查询工序任务"), ("领料", "查询领料"),
                            ("退料", "查询退料"), ("报工", "查询报工"),
                            ("详情", "查询生产计划详情"), ("详细", "查询生产计划详情")):
        if keyword.lower() in goal.lower():
            return target
    return "查询生产计划详情"


def summarize_recent_turns(history: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """只传问题、路由、已验证主键和状态；绝不复制 SQL、密码或结果行。"""

    summaries = []
    for turn in (history or [])[-MAX_CONTEXT_TURNS:]:
        trace = turn.get("trace") or {}
        execution = trace.get("execution") or {}
        resolution = trace.get("resolution") or {}
        verified = execution.get("status") == "SUCCESS" and resolution.get("status") == "RESOLVED"
        summaries.append({
            "question": str(turn.get("question", ""))[:300],
            "status": "SUCCESS" if execution.get("status") == "SUCCESS" else "NOT_GROUNDED",
            "route": (trace.get("request") or {}).get("route"),
            "verified_plan_id": resolution.get("sourceId") if verified else None,
            "goal": str((trace.get("planning") or {}).get("candidate_query_ir", {}).get("goal", ""))[:300]
            if verified else "",
        })
    return summaries


def rewrite_question(question: str, history: list[dict[str, Any]] | None) -> dict[str, Any]:
    """明确条件覆盖历史；模糊追问只沿用最近一次成功定位的单计划。"""

    latest = question.strip()
    recent = summarize_recent_turns(history)
    result = {"mode": "NEW_QUERY", "original_question": latest,
              "effective_question": latest, "recent_turns": recent,
              "inherited_plan_id": None, "inherited_from_turn": None,
              "explicit_source_id": None, "clarification": ""}
    explicit_match = EXPLICIT_SOURCE_ID.search(latest)
    if explicit_match:
        result["explicit_source_id"] = int(explicit_match.group(1) or explicit_match.group(2))
        if result["explicit_source_id"] < 1:
            result.update(mode="CLARIFY", clarification="生产计划 ID 必须是正整数。")
            return result
    if RESET_QUERY.search(latest):
        result.update(mode="RESET", recent_turns=[], clarification="上下文已重置，请提出新的查询问题。")
        return result
    if not recent or EXPLICIT_PLAN.search(latest) or GLOBAL_QUERY.search(latest):
        if recent and EXPLICIT_PLAN.search(latest):
            result["mode"] = "CHANGE_SCOPE"
        return result
    if UNKNOWN_SCOPE.search(latest):
        result.update(mode="CLARIFY", clarification="请明确新生产计划的ID、编号或名称。")
        return result

    # 失败轮次不成为新锚点；成功的全局查询也不会让更早的单计划被悄悄沿用。
    latest_success = next(((index, item) for index, item in reversed(list(enumerate(recent)))
                           if item["status"] == "SUCCESS"), None)
    if latest_success is None or latest_success[1]["verified_plan_id"] is None:
        return result
    index, anchor = latest_success
    plan_id = anchor["verified_plan_id"]
    if not isinstance(plan_id, int) or plan_id < 1:
        return result
    result.update(inherited_plan_id=plan_id, inherited_from_turn=index + 1)
    if REFINEMENT.search(latest) and not DOMAIN_TARGET.search(latest):
        if not anchor["goal"]:
            result.update(mode="CLARIFY", clarification="请说明本轮要继续查询哪项内容。")
            return result
        result["mode"] = "REFINE"
        target = f"沿用上一轮分析方向：{_previous_target(anchor)}；本轮明确条件优先：{latest}"
    elif DOMAIN_TARGET.search(latest):
        result["mode"] = "DRILL_DOWN"
        target = latest
    else:
        result.update(mode="CLARIFY", clarification="请说明本轮要查询生产计划的哪项内容。")
        return result
    result["effective_question"] = (
        f"明确选择生产计划ID {plan_id}，按这个ID执行以下查询要求（忽略旧定位词）：{target}"
    )
    return result
