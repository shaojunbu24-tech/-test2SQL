"""使用 Streamlit 展示本体、Query IR、SQL 编译轨迹和数据库结果。"""

import json

import pandas as pd
import streamlit as st

from material_query import MaterialQueryPipeline
from material_query.query_hits import extract_query_hits
from material_query.ontology_browser import interactive_ontology_dot, show_ontology_browser
from material_query.conversation import MAX_CONTEXT_TURNS


st.set_page_config(page_title="料查分析 Query IR MVP", page_icon="🔎", layout="wide")


def ontology_dot(entities: list[dict], relations: list[dict]) -> str:
    """把实体和关系转换成 Graphviz DOT，并保留没有边的实体节点。"""

    lines = ["digraph ontology {", "rankdir=LR;", "node [shape=box, style=rounded];"]
    for entity in entities:
        lines.append(f'"{entity["id"]}" [label="{entity.get("label") or entity["id"]}"];')
    for relation in relations:
        lines.append(
            f'"{relation["from"]}" -> "{relation["to"]}" '
            f'[label="{relation["id"]}"];'
        )
    lines.append("}")
    return "\n".join(lines)


def ontology_overview(registry) -> dict:
    """从注册表数据构建全量本体概览，兼容 Streamlit 缓存的旧实例。"""

    planner_context = registry.planner_context()
    entities = [
        {
            "id": entity_id,
            "label": definition.get("label"),
            "properties": sorted(definition.get("properties", {}).keys()),
            "mvpExecutable": entity_id in registry.mapping["entities"],
        }
        for entity_id, definition in registry.entity_types.items()
    ]
    relations = [
        {
            "id": relation_id,
            "from": definition["from"],
            "to": definition["to"],
            "cardinality": definition.get("cardinality"),
            "mvpExecutable": relation_id in registry.mapping["relations"],
        }
        for relation_id, definition in registry.relations.items()
    ]
    return {
        "entities": entities,
        "relations": relations,
        "metrics": planner_context["metrics"],
    }


# Pipeline 只加载本地 YAML 配置，不持有数据库连接；不缓存可避免代码热更新后
# 界面仍调用旧版 run() 方法，导致新增参数与旧实例的签名不一致。
pipeline = MaterialQueryPipeline()
summary = pipeline.registry.summary()
executable_relation_count = summary.get(
    "executable_relation_count", len(pipeline.registry.mapping["relations"])
)
executable_entity_count = summary.get(
    "executable_entity_count", len(pipeline.registry.mapping["entities"])
)

st.title("料查分析：自然语言 → Query IR → SQL → 执行")
st.caption("模型只规划本体语言；校验、物理映射、SQL 生成和数据库执行由确定性程序完成。")
st.session_state.setdefault("chat_turns", [])

with st.sidebar:
    st.subheader("运行范围")
    st.metric("数据库", summary["database"])
    st.write(f"本体版本：`{summary['ontology_version']}`")
    st.write(f"映射版本：`{summary['mapping_version']}`")
    st.write(
        f"逻辑实体 {summary['entity_count']}（MVP 可执行 {executable_entity_count}） · "
        f"逻辑关系 {summary['relation_count']} "
        f"（MVP 可执行 {executable_relation_count}） · "
        f"指标 {summary['metric_count']}"
    )
    execute = st.checkbox("执行真实只读查询", value=True)
    answer_with_llm = st.checkbox("由大模型基于结果回答（会发送本次结果）", value=False)
    st.warning("执行器只允许 mom-test、SELECT/WITH 和只读事务。")

st.text_area(
    "当前提问（可继续追问上一轮）",
    value="分析生产计划ID 665的投料差异，筛选差异率超过5%的记录",
    key="question_input",
    height=90,
)


def queue_question():
    """提交前保存本轮文本，再清空输入框，便于直接输入下一轮追问。"""

    st.session_state["submitted_question"] = st.session_state.get("question_input", "")
    st.session_state["question_input"] = ""


run_clicked = st.button("生成并运行", type="primary", width="stretch", on_click=queue_question)

if run_clicked:
    question = st.session_state.pop("submitted_question", "")
    # 新请求开始时先清理旧结果，避免失败后继续展示上一次 Query IR。
    st.session_state.pop("trace", None)
    if not question.strip():
        st.error("请输入自然语言问题。")
    else:
        with st.spinner("正在生成 Query IR、编译 SQL 并执行……"):
            try:
                new_trace = pipeline.run(
                    question.strip(),
                    execute=execute,
                    answer_with_llm=answer_with_llm,
                    conversation_history=st.session_state["chat_turns"],
                )
            except Exception as error:
                st.session_state["chat_turns"].append(
                    {"question": question.strip(), "trace": None, "error": str(error)}
                )
                st.error(str(error))
            else:
                if (new_trace.get("context") or {}).get("mode") == "RESET":
                    st.session_state["chat_turns"] = []
                    st.session_state.pop("trace", None)
                    st.success("对话上下文已清空，请输入新的问题。")
                else:
                    st.session_state["chat_turns"].append(
                        {"question": question.strip(), "trace": new_trace, "error": None}
                    )
                    st.session_state["trace"] = new_trace
            # 对话窗口最多保存五轮完整追踪，避免业务结果在页面会话中无限累积。
            st.session_state["chat_turns"] = st.session_state["chat_turns"][-MAX_CONTEXT_TURNS:]
            if st.session_state["chat_turns"]:
                st.session_state["selected_chat_turn"] = len(st.session_state["chat_turns"]) - 1
            else:
                st.session_state.pop("selected_chat_turn", None)

if st.button("清空对话上下文", key="reset_chat_context"):
    st.session_state["chat_turns"] = []
    st.session_state.pop("selected_chat_turn", None)
    st.session_state.pop("trace", None)
    st.success("最近五轮上下文已清空；本体浏览与七个展示页保持不变。")

if st.session_state["chat_turns"]:
    st.subheader("对话记录")
    st.caption("仅最近五轮摘要进入下一次语义解析；历史 SQL 和数据库结果不会作为上下文发送给模型。")
    for index, turn in enumerate(st.session_state["chat_turns"]):
        with st.chat_message("user"):
            st.write(turn["question"])
        with st.chat_message("assistant"):
            turn_trace = turn.get("trace") or {}
            if turn.get("error"):
                st.error(turn["error"])
            elif (turn_trace.get("answer") or {}).get("status") == "SUCCESS":
                st.write(turn_trace["answer"]["text"])
            elif (turn_trace.get("execution") or {}).get("status") == "SUCCESS":
                st.write(f"只读查询返回 {turn_trace['execution']['row_count']} 行；可在下方查看本轮完整链路。")
            else:
                st.write((turn_trace.get("execution") or {}).get("reason", "本轮未产生查询结果。"))
            st.button("查看本轮 Query IR / SQL / 结果", key=f"view_chat_turn_{index}",
                      on_click=lambda chosen=index: st.session_state.update(selected_chat_turn=chosen))

selected_turn = st.session_state.get("selected_chat_turn")
if selected_turn is not None and 0 <= selected_turn < len(st.session_state["chat_turns"]):
    selected_trace = st.session_state["chat_turns"][selected_turn].get("trace")
    if selected_trace is None:
        st.session_state.pop("trace", None)
    else:
        st.session_state["trace"] = selected_trace

trace = st.session_state.get("trace")

# Streamlit 热更新会保留旧版代码产生的 trace。旧 trace 没有 query_hits，
# 因此在渲染前根据其 Candidate Query IR 补齐，避免页面升级后 KeyError。
if trace and "query_hits" not in trace.get("ontology", {}):
    candidate_query_ir = trace.get("planning", {}).get("candidate_query_ir")
    query_hits = (
        extract_query_hits(pipeline.registry, candidate_query_ir)
        if candidate_query_ir
        else {"entities": [], "relations": [], "metrics": [], "operators": []}
    )
    trace.setdefault("ontology", {})["query_hits"] = query_hits

current_hits = trace.get("ontology", {}).get("query_hits") if trace else None

browser_tab, ontology_tab, resolution_tab, query_ir_tab, mapping_tab, sql_tab, result_tab = st.tabs(
    ["本体浏览", "本体概览", "实体定位与任务", "Query IR", "校验与映射", "SQL", "执行结果"]
)

with browser_tab:
    show_ontology_browser(pipeline.registry, current_hits)

with resolution_tab:
    if trace:
        if trace.get("context"):
            st.subheader("最近五轮上下文 → 本轮独立问题")
            st.json(trace["context"])
        st.subheader("用户表达 → 本体属性 → 数据库记录")
        st.json(trace.get("request", {"说明": "离线Query IR未经过自然语言解析。"}))
        resolution = trace.get("resolution", {})
        st.json(resolution)
        if resolution.get("status") == "AMBIGUOUS":
            st.warning("名称不唯一，选择具体生产计划后再查询。")
            for plan in resolution["candidates"]:
                # 回调发生在下一轮渲染前，因此可安全修改文本框的 session_state。
                def choose_plan(plan_id=plan["id"], original=trace["input"].get("rewritten_question", trace["input"]["natural_language"])):
                    st.session_state["question_input"] = f"明确选择生产计划ID {plan_id}，按这个ID执行以下查询要求（忽略原定位词）：{original}"
                st.button(f"选择 ID {plan['id']} · {plan['code']}", key=f"choose_{plan['id']}", on_click=choose_plan)
        if trace.get("task"):
            st.subheader("固定分析任务及参数")
            st.json(trace["task"])
        if trace.get("planning_repair"):
            st.write("动态计划修复记录")
            st.json(trace["planning_repair"])
    else:
        st.info("运行查询后可查看定位属性、候选记录、规范ID和任务参数。")

with ontology_tab:
    left, right = st.columns([1, 1])
    with left:
        st.subheader("本次查询命中明细")
        if trace:
            hits = current_hits or {"entities": [], "relations": [], "metrics": [], "operators": []}
            st.info("本次命中已经高亮在「本体浏览」的全量关系图中；图中实体、关系均可点击查看详情。")
            st.write("命中实体", [item["id"] for item in hits["entities"]] or ["无"])
            st.write("命中关系", [item["id"] for item in hits["relations"]] or ["无"])
            st.write("命中指标", [item["id"] for item in hits["metrics"]] or ["无"])
            st.write("使用算子", hits["operators"] or ["无"])
            st.write("IR 显式引用的属性及命中字段")
            st.dataframe(pd.DataFrame(hits.get("properties", [])), hide_index=True, width="stretch")
            st.caption("名称/编号的定位字段见「实体定位与任务」；JOIN键和指标依赖见「校验与映射」。")
        else:
            st.info("运行一次查询后，这里只显示本次 Query IR 实际命中的本体。")

        st.divider()
        st.subheader("全部逻辑本体关系图（链接模式）")
        context = ontology_overview(pipeline.registry)
        try:
            st.graphviz_chart(
                interactive_ontology_dot(
                    pipeline.registry,
                    highlight_entities=[item["id"] for item in (current_hits or {}).get("entities", [])],
                    highlight_relations=[item["id"] for item in (current_hits or {}).get("relations", [])],
                ), width="stretch"
            )
            st.caption("主要交互入口位于「本体浏览」；这里保留轻量链接图用于概览。")
        except Exception:
            st.info("当前环境未渲染 Graphviz，右侧仍可查看完整关系表。")
    with right:
        st.subheader("全部逻辑实体、关系与指标")
        st.write("实体（mvpExecutable 表示当前是否可编译执行）")
        st.dataframe(pd.DataFrame(context["entities"]), width="stretch")
        st.write("关系（mvpExecutable 表示当前是否可编译执行）")
        st.dataframe(pd.DataFrame(context["relations"]), width="stretch")
        st.write("指标")
        st.dataframe(pd.DataFrame(context["metrics"]), width="stretch")

with query_ir_tab:
    if trace and "planning" in trace:
        st.subheader("本次 Query IR（模型规划或注册任务展开）")
        st.json(trace["planning"]["candidate_query_ir"])
        st.caption(
            f"来源：{trace['planning']['source']} · 模型：{trace['planning'].get('model')} · "
            f"规划耗时：{trace['timings_ms']['planning']} ms"
        )
    else:
        st.info("运行一次查询后显示 Query IR。")

with mapping_tab:
    if trace and "validation" in trace:
        st.subheader("Query IR 校验")
        st.json(trace["validation"])
        st.subheader("算子到物理映射")
        st.json(trace["compilation"]["operator_mapping_trace"])
    else:
        st.info("运行一次查询后显示校验和物理映射。")

with sql_tab:
    if trace and "compilation" in trace:
        st.subheader("动态生成的参数化 SQL")
        st.code(trace["compilation"]["sql"], language="sql")
        st.write("绑定参数")
        st.json(trace["compilation"]["parameters"])
    else:
        st.info("运行一次查询后显示 SQL。")

with result_tab:
    if trace:
        execution = trace["execution"]
        if execution["status"] == "SUCCESS":
            st.success(
                f"只读查询成功：{execution['database']}，返回 {execution['row_count']} 行。"
            )
            answer = trace.get("answer", {})
            st.subheader("基于查询结果的回答")
            if answer.get("status") == "SUCCESS":
                st.write(answer["text"])
                st.caption(
                    f"模型：{answer['model']} · 基于本次 SQL 返回的 {answer['based_on_row_count']} 行结果生成"
                )
            else:
                st.info(answer.get("reason", "未生成大模型回答。"))
            st.subheader("结构化查询结果")
            st.write("规范实体")
            st.json(execution["grounded_entities"])
            st.dataframe(pd.DataFrame(execution["rows"]), width="stretch")
        else:
            st.info(execution["reason"])
        st.write("阶段耗时")
        st.json(trace["timings_ms"])
        with st.expander("完整追踪 JSON"):
            st.code(json.dumps(trace, ensure_ascii=False, indent=2, default=str), language="json")
    else:
        st.info("运行一次查询后显示数据库结果。")
