"""Streamlit 本体浏览器：把全量关系图作为可点击的元数据导航器。"""

import json
from urllib.parse import quote

import pandas as pd
import streamlit as st
from streamlit_agraph import Config, Edge, Node, agraph


def _select_object(kind, object_id):
    """由原生控件在重跑前更新选择，避免控件事件后再次调用 st.rerun。"""

    st.session_state["selected_ontology_object"] = (kind, object_id)
    st.session_state["ontology_picker"] = f"{kind}:{object_id}"


def _select_from_picker():
    """把备用下拉框的值同步为统一的本体选择状态。"""

    chosen = st.session_state.get("ontology_picker", "entity:ProductionPlan")
    kind, object_id = chosen.split(":", 1)
    st.session_state["selected_ontology_object"] = (kind, object_id)


def interactive_ontology_dot(
    registry, selected_type=None, selected_id=None,
    highlight_entities=None, highlight_relations=None,
):
    """生成带链接的 SVG 图；实体节点和关系连线都可改变当前详情对象。"""
    lines = [
        "digraph ontology {", "rankdir=LR;",
        'graph [bgcolor="transparent", pad="0.2", nodesep="0.45", ranksep="0.8"];',
        'node [shape=box, style="rounded,filled", fontname="Arial", fillcolor="#f8fafc", color="#94a3b8"];',
        'edge [fontname="Arial", fontsize=10, color="#64748b", fontcolor="#334155"];',
    ]
    highlight_entities = set(highlight_entities or [])
    highlight_relations = set(highlight_relations or [])
    for entity_id, entity in registry.entity_types.items():
        selected = selected_type == "entity" and selected_id == entity_id
        hit = entity_id in highlight_entities
        url = "?ontology_entity=" + quote(entity_id)
        lines.append(
            f'{json.dumps(entity_id)} [label={json.dumps(entity.get("label", entity_id), ensure_ascii=False)}, '
            f'URL={json.dumps(url)}, target="_top", '
            f'tooltip={json.dumps("点击查看实体：" + entity.get("label", entity_id), ensure_ascii=False)}, '
            f'color="{"#2563eb" if selected else "#16a34a" if hit else "#94a3b8"}", '
            f'fillcolor="{"#dbeafe" if selected else "#dcfce7" if hit else "#f8fafc"}"];'
        )
    for relation_id, relation in registry.relations.items():
        selected = selected_type == "relation" and selected_id == relation_id
        description = registry.property_semantics["relations"].get(relation_id, relation_id)
        hit = relation_id in highlight_relations
        color = "#dc2626" if selected else "#ea580c" if hit else "#64748b"
        url = "?ontology_relation=" + quote(relation_id)
        lines.append(
            f'{json.dumps(relation["from"])} -> {json.dumps(relation["to"])} '
            f'[label={json.dumps(description, ensure_ascii=False)}, URL={json.dumps(url)}, target="_top", '
            f'tooltip={json.dumps("点击查看关系：" + relation_id, ensure_ascii=False)}, '
            f'color="{color}", fontcolor="{color}", penwidth={"5" if selected else "4" if hit else "2.5"}];'
        )
    lines.append("}")
    return "\n".join(lines)


def selected_ontology(registry):
    """优先读取组件内选择，再兼容旧图URL；非法参数安全回退。"""
    selected = st.session_state.get("selected_ontology_object")
    if selected and selected[0] == "relation" and selected[1] in registry.relations:
        return selected
    if selected and selected[0] == "entity" and selected[1] in registry.entity_types:
        return selected
    relation_id = st.query_params.get("ontology_relation")
    entity_id = st.query_params.get("ontology_entity")
    if relation_id in registry.relations:
        return "relation", relation_id
    if entity_id in registry.entity_types:
        return "entity", entity_id
    return "entity", "ProductionPlan"


def _agraph_data(registry, selected_type, selected_id, query_hits):
    """将关系转换成菱形节点，扩大关系点击区域并支持命中高亮。"""
    hit_entities = {item["id"] for item in (query_hits or {}).get("entities", [])}
    hit_relations = {item["id"] for item in (query_hits or {}).get("relations", [])}
    nodes, edges = [], []
    for entity_id, entity in registry.entity_types.items():
        selected = selected_type == "entity" and selected_id == entity_id
        hit = entity_id in hit_entities
        background = "#dbeafe" if selected else "#dcfce7" if hit else "#f8fafc"
        border = "#2563eb" if selected else "#16a34a" if hit else "#94a3b8"
        nodes.append(Node(
            id="entity::" + entity_id, label=entity["label"], shape="box", size=28,
            title=f"实体 {entity_id}\n点击查看属性和映射",
            color={"background": background, "border": border,
                   "highlight": {"background": background, "border": border}},
            borderWidth=4 if selected else 2 if hit else 1,
        ))
    for relation_id, relation in registry.relations.items():
        selected = selected_type == "relation" and selected_id == relation_id
        hit = relation_id in hit_relations
        description = registry.property_semantics["relations"].get(relation_id, relation_id)
        background = "#fee2e2" if selected else "#ffedd5" if hit else "#f1f5f9"
        border = "#dc2626" if selected else "#ea580c" if hit else "#94a3b8"
        relation_node = "relation::" + relation_id
        nodes.append(Node(
            id=relation_node, label=description, shape="diamond", size=32,
            title=f"关系 {relation_id}\n{relation['from']} → {relation['to']}\n点击查看JOIN映射",
            color={"background": background, "border": border,
                   "highlight": {"background": background, "border": border}},
            borderWidth=5 if selected else 3 if hit else 1,
        ))
        edge_color = "#ea580c" if hit else "#94a3b8"
        edges.append(Edge("entity::" + relation["from"], relation_node, color=edge_color, width=2 if hit else 1))
        edges.append(Edge(relation_node, "entity::" + relation["to"], color=edge_color, width=2 if hit else 1))
    return nodes, edges


def _entity_detail(registry, entity_id):
    """显示一个实体的定义、属性能力、相关指标和物理映射。"""
    entity = registry.entity_types[entity_id]
    st.markdown(f"### {entity['label']}")
    st.caption(f"实体ID：{entity_id} · 类型：{entity['kind']}")
    st.write("规范ID格式")
    st.code(entity["canonical_id_pattern"], language=None)
    attributes, metrics, mapping = st.tabs(["属性", "指标与任务", "物理映射"])
    with attributes:
        st.dataframe(pd.DataFrame(registry.property_catalog(entity_id)), hide_index=True, width="stretch")
        st.caption("projectable：可输出；resolvable：可定位；filterableInQueryIR：可直接筛选；"
                   "executableMapping：已接入当前MVP。")
    with metrics:
        definitions = {
            key: value for key, value in registry.mapping["metrics"].items()
            if entity_id == value.get("source_entity") or entity_id in value.get("grain", [])
            or any(dep.startswith(entity_id + ".") for dep in value.get("dependencies", []))
        }
        st.write("相关指标")
        st.json(definitions)
        st.write("已注册分析任务")
        st.json(registry.tasks)
    with mapping:
        st.json({"当前执行映射": registry.mapping["entities"].get(entity_id),
                 "本体设计映射": registry.semantic["entity_mappings"].get(entity_id)})


def _relation_detail(registry, relation_id):
    """显示关系语义、两端实体、可执行状态和 JOIN 定义。"""
    relation = registry.relations[relation_id]
    catalog = next(item for item in registry.relation_catalog(relation["from"])
                   if item["id"] == relation_id)
    st.markdown(f"### {catalog['description']}")
    st.caption(f"关系ID：{relation_id}")
    st.write(f"**方向：** {relation['from']} → {relation['to']}")
    st.write(f"**基数：** {relation.get('cardinality', '未定义')}")
    if catalog["executable"]:
        st.success("当前 MVP 可执行：编译器可以把该关系转换为 JOIN。")
    else:
        st.warning("当前仅有逻辑/设计定义，尚不能由 MVP 编译执行。")
    st.write("当前执行映射")
    if catalog["mapping"] is None:
        st.caption("暂无当前执行映射。")
    else:
        st.json(catalog["mapping"])
    st.write("本体设计映射")
    if catalog["designMapping"] is None:
        st.caption("暂无本体设计映射。")
    else:
        st.json(catalog["designMapping"])
    left, right = st.columns(2)
    with left:
        st.button(f"查看起点：{registry.entity_types[relation['from']]['label']}",
                  key="relation_from", on_click=_select_object,
                  args=("entity", relation["from"]))
    with right:
        st.button(f"查看终点：{registry.entity_types[relation['to']]['label']}",
                  key="relation_to", on_click=_select_object,
                  args=("entity", relation["to"]))


def show_ontology_browser(registry, query_hits=None):
    """左侧全量交互图高亮查询命中，右侧随对象选择显示详细信息。"""
    selected_type, selected_id = selected_ontology(registry)
    st.subheader("全量本体关系图与本次查询命中")
    st.caption("矩形是实体，菱形是关系。绿色/橙色表示本次查询命中，蓝色/红色表示当前选中；支持拖拽、缩放和点击。")
    graph, detail = st.columns([1.25, 1], gap="large")
    with graph:
        nodes, edges = _agraph_data(registry, selected_type, selected_id, query_hits)
        clicked = agraph(
            nodes=nodes, edges=edges,
            config=Config(
                height=680, width=900, directed=True, physics=True,
                hierarchical=False, nodeHighlightBehavior=False,
                interaction={"hover": True, "navigationButtons": True, "keyboard": True},
            ),
        )
        if clicked and "::" in clicked:
            clicked_type, clicked_id = clicked.split("::", 1)
            if (clicked_type, clicked_id) != (selected_type, selected_id):
                st.session_state["selected_ontology_object"] = (clicked_type, clicked_id)
                # 自定义组件的点击已经触发了本轮重跑。直接更新本轮局部值，
                # 右侧详情即可立即显示；不要再 st.rerun() 造成 iframe 双重卸载。
                selected_type, selected_id = clicked_type, clicked_id
                st.session_state["ontology_picker"] = f"{clicked_type}:{clicked_id}"
        if query_hits:
            st.write("本次命中实体", [item["label"] for item in query_hits.get("entities", [])] or ["无"])
            st.write("本次命中关系", [item["id"] for item in query_hits.get("relations", [])] or ["无"])
        with st.expander("关系快捷入口（连线难点时使用）"):
            relation_columns = st.columns(2)
            for index, (relation_id, relation) in enumerate(registry.relations.items()):
                label = registry.property_semantics["relations"].get(relation_id, relation_id)
                with relation_columns[index % 2]:
                    st.button(label, key=f"quick_relation_{relation_id}", width="stretch",
                              help=relation_id, on_click=_select_object,
                              args=("relation", relation_id))
        options = ([f"entity:{key}" for key in registry.entity_types]
                   + [f"relation:{key}" for key in registry.relations])
        current = f"{selected_type}:{selected_id}"
        if st.session_state.get("ontology_picker") not in options:
            st.session_state["ontology_picker"] = current
        st.selectbox(
            "选择实体或关系（图形点击的备用入口）", options,
            key="ontology_picker", on_change=_select_from_picker,
            format_func=lambda value: (
                "实体 · " + registry.entity_types[value.split(":", 1)[1]]["label"]
                if value.startswith("entity:")
                else "关系 · " + registry.property_semantics["relations"].get(
                    value.split(":", 1)[1], value)
            ),
        )
    with detail:
        st.subheader("选中对象详情")
        if selected_type == "entity":
            _entity_detail(registry, selected_id)
        else:
            _relation_detail(registry, selected_id)
