"""使用 Streamlit 展示本体、Query IR、SQL 编译轨迹和数据库结果。"""

import json

import pandas as pd
import streamlit as st

from material_query import MaterialQueryPipeline
from material_query.query_hits import extract_query_hits


st.set_page_config(page_title="料查分析 Query IR MVP", page_icon="🔎", layout="wide")


# 能力版本参与 Streamlit 缓存键；映射或编译能力升级时会创建新 Pipeline。
PIPELINE_CACHE_VERSION = "mom-test@0.3.0"


@st.cache_resource
def get_pipeline(_capability_version: str) -> MaterialQueryPipeline:
    """缓存不含单次查询状态的 Pipeline 和本体注册表。"""

    return MaterialQueryPipeline()


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


pipeline = get_pipeline(PIPELINE_CACHE_VERSION)
summary = pipeline.registry.summary()
executable_relation_count = summary.get(
    "executable_relation_count", len(pipeline.registry.mapping["relations"])
)
executable_entity_count = summary.get(
    "executable_entity_count", len(pipeline.registry.mapping["entities"])
)

st.title("料查分析：自然语言 → Query IR → SQL → 执行")
st.caption("模型只规划本体语言；校验、物理映射、SQL 生成和数据库执行由确定性程序完成。")

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
    st.warning("执行器只允许 mom-test、SELECT/WITH 和只读事务。")

question = st.text_area(
    "用户问题",
    value="分析生产计划ID 665的投料差异，筛选差异率超过5%的记录",
    height=90,
)
run_clicked = st.button("生成并运行", type="primary", width="stretch")

if run_clicked:
    # 新请求开始时先清理旧结果，避免失败后继续展示上一次 Query IR。
    st.session_state.pop("trace", None)
    if not question.strip():
        st.error("请输入自然语言问题。")
    else:
        with st.spinner("正在生成 Query IR、编译 SQL 并执行……"):
            try:
                st.session_state["trace"] = pipeline.run(question.strip(), execute=execute)
            except Exception as error:
                st.error(str(error))

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

ontology_tab, query_ir_tab, mapping_tab, sql_tab, result_tab = st.tabs(
    ["本体概览", "Query IR", "校验与映射", "SQL", "执行结果"]
)

with ontology_tab:
    left, right = st.columns([1, 1])
    with left:
        st.subheader("本次查询命中的本体")
        if trace:
            hits = trace.get("ontology", {}).get(
                "query_hits",
                {"entities": [], "relations": [], "metrics": [], "operators": []},
            )
            st.graphviz_chart(
                ontology_dot(hits["entities"], hits["relations"]), width="stretch"
            )
            st.write("命中实体", [item["id"] for item in hits["entities"]] or ["无"])
            st.write("命中关系", [item["id"] for item in hits["relations"]] or ["无"])
            st.write("命中指标", [item["id"] for item in hits["metrics"]] or ["无"])
            st.write("使用算子", hits["operators"] or ["无"])
        else:
            st.info("运行一次查询后，这里只显示本次 Query IR 实际命中的本体。")

        st.divider()
        st.subheader("全部逻辑本体关系图")
        context = ontology_overview(pipeline.registry)
        try:
            st.graphviz_chart(
                ontology_dot(context["entities"], context["relations"]), width="stretch"
            )
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
    if trace:
        st.subheader("模型生成的 Candidate Query IR")
        st.json(trace["planning"]["candidate_query_ir"])
        st.caption(
            f"来源：{trace['planning']['source']} · 模型：{trace['planning'].get('model')} · "
            f"规划耗时：{trace['timings_ms']['planning']} ms"
        )
    else:
        st.info("运行一次查询后显示 Query IR。")

with mapping_tab:
    if trace:
        st.subheader("Query IR 校验")
        st.json(trace["validation"])
        st.subheader("算子到物理映射")
        st.json(trace["compilation"]["operator_mapping_trace"])
    else:
        st.info("运行一次查询后显示校验和物理映射。")

with sql_tab:
    if trace:
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
