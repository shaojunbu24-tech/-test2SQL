"""验证实体解析、固定任务、页面点击与失败分支；离线测试不访问外部服务。"""

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from material_query.pipeline import MaterialQueryPipeline
from material_query.entity_resolver import EntityResolver
from material_query.tasks import expand_material_gap
from material_query.request_router import validate_request


def request(prop="sourceId", value=665, threshold=None):
    """用于测试的有效结构化请求；正式运行由 DeepSeek 解析。"""
    return {"route": "analyze_material_gap", "selector": {
        "entityType": "ProductionPlan", "property": prop, "operator": "EQ", "value": value},
        "threshold": threshold, "comparison": "GT", "limit": 100, "clarification": ""}


class ResolutionTaskTests(unittest.TestCase):
    def setUp(self):
        self.pipeline = MaterialQueryPipeline()
        self.registry = self.pipeline.registry

    def test_registered_task_threshold_and_grain(self):
        """没有阈值应保留未计算行；GT/GTE边界由用户请求控制。"""
        for threshold in (None, 0, 0.05):
            for comparison in ("GT", "GTE"):
                parsed = {**request(threshold=threshold), "comparison": comparison}
                ir = expand_material_gap(self.registry, "本次问题", 665, parsed)
                self.pipeline.validator.validate(ir)
                sql = self.pipeline.compiler.compile(ir).sql
                self.assertEqual("本次问题", ir["goal"])
                filters = [step for step in ir["steps"] if step["op"] == "Filter"]
                self.assertEqual(threshold is not None, bool(filters))
                if filters:
                    self.assertEqual(comparison, filters[0]["predicates"][0]["operator"])
                self.assertIn("issue_agg", sql)
                self.assertIn("return_agg", sql)

    def test_resolver_does_not_guess_ambiguous_or_missing(self):
        """零条或多条候选均不能产生规范ID；唯一候选才允许进入分析。"""
        for rows, status in (([], "NOT_FOUND"), ([{"id": 665}], "RESOLVED"),
                             ([{"id": 665}, {"id": 666}], "AMBIGUOUS")):
            executor = Mock()
            executor.execute.return_value = {"rows": rows}
            result = EntityResolver(self.registry, executor).resolve(request("name", "MRP")["selector"])
            self.assertEqual(status, result["status"])
            self.assertEqual(status == "RESOLVED", "sourceId" in result)

    def test_resolver_parameterizes_and_escapes(self):
        """包含检索不让用户输入通配符扩大范围，输入引号不进入SQL。"""
        executor = Mock()
        executor.execute.return_value = {"rows": []}
        selector = {**request("name", "A%' OR 1=1 _!")["selector"], "operator": "CONTAINS"}
        result = EntityResolver(self.registry, executor).resolve(selector)
        self.assertNotIn("OR 1=1", result["sql"])
        self.assertEqual("%A!%' OR 1=1 !_!!%", result["parameters"]["selector_value"])

    def test_registry_exposes_properties_without_physical_fields_to_llm(self):
        """实体属性包括补齐的关联键和解释，物理实现只在浏览器暴露。"""
        props = self.registry.property_catalog("ProductionPlan", False)
        self.assertIn("sourceId", [prop["id"] for prop in props])
        self.assertTrue(next(prop for prop in props if prop["id"] == "planNo")["resolvable"])
        self.assertFalse(any("physicalField" in prop for prop in props))
        task = self.registry.property_catalog("ProcessTask")
        self.assertEqual("product_id", next(prop for prop in task if prop["id"] == "outputProductId")["physicalField"])

    def test_id_must_be_integer(self):
        with self.assertRaises(ValueError):
            validate_request(request(value="665"))

    def test_query_hits_include_source_property(self):
        """生产计划ID的命中应能追踪到实体属性与物理字段。"""
        from material_query.config import load_json
        ir = load_json(PROJECT / "examples/query-ir-plan-detail.json")
        hits = self.registry.query_hits(ir)
        self.assertIn({"id": "ProductionPlan.sourceId", "physicalField": "id", "table": "produce_plan"}, hits["properties"])
        self.assertEqual(["ProductionPlan"], [item["id"] for item in hits["entities"]])

    def test_resolver_failure_does_not_reach_planner(self):
        """定位失败或歧义必须停止，不能继续生成分析SQL。"""
        for rows, expected in (([], "NOT_FOUND"), ([{"id": 665}, {"id": 666}], "AMBIGUOUS")):
            with patch("material_query.pipeline.RequestRouter") as router, patch("material_query.pipeline.QueryIRPlanner") as planner:
                router.return_value.parse.return_value = request("name", "MRP")
                self.pipeline.executor = Mock()
                self.pipeline.executor.execute.return_value = {"rows": rows}
                trace = self.pipeline.run("查MRP计划料差", execute=True)
                self.assertEqual(expected, trace["resolution"]["status"])
                self.assertNotIn("compilation", trace)
                planner.assert_not_called()

    @patch("material_query.pipeline.QueryIRPlanner")
    @patch("material_query.pipeline.RequestRouter")
    def test_fixed_task_does_not_call_dynamic_planner(self, router, planner):
        router.return_value.parse.return_value = request()
        trace = self.pipeline.run("分析生产计划665料差")
        self.assertEqual("REGISTERED_TASK", trace["planning"]["source"])
        self.assertEqual("UNVERIFIED", trace["resolution"]["status"])
        self.assertEqual("NOT_EXECUTED", trace["execution"]["status"])
        planner.assert_not_called()

    @patch("material_query.pipeline.RequestRouter")
    def test_name_preview_returns_pending(self, router):
        router.return_value.parse.return_value = request("name", "MRP")
        trace = self.pipeline.run("查询MRP计划")
        self.assertEqual("REQUIRES_LOOKUP", trace["resolution"]["status"])
        self.assertNotIn("planning", trace)

    @patch("material_query.pipeline.QueryResultAnswerer")
    @patch("material_query.pipeline.RequestRouter")
    def test_answer_failure_preserves_database_results(self, router, answerer):
        router.return_value.parse.return_value = request()
        self.pipeline.executor = Mock()
        self.pipeline.executor.execute.side_effect = [
            {"rows": [{"id": 665, "code": "P-665", "name": "test"}]},
            {"status": "SUCCESS", "rows": [{"gross_issued_qty": 100}], "row_count": 1},
        ]
        answerer.side_effect = RuntimeError("模拟模型超时")
        trace = self.pipeline.run("计划665料差", execute=True, answer_with_llm=True)
        self.assertEqual("FAILED", trace["answer"]["status"])
        self.assertEqual("SUCCESS", trace["execution"]["status"])
        self.assertEqual(100, trace["execution"]["rows"][0]["gross_issued_qty"])

    def test_streamlit_click_entity_and_pending_state(self):
        """真实执行Streamlit脚本，使用图的等价选择入口，并渲染待定位状态。"""
        from streamlit.testing.v1 import AppTest
        app = AppTest.from_file(str(PROJECT / "src/ui.py"), default_timeout=20).run()
        self.assertFalse(app.exception)
        app.selectbox[0].select("entity:PlanBOMItem").run()
        self.assertEqual(("entity", "PlanBOMItem"), app.session_state["selected_ontology_object"])
        self.assertFalse(app.exception)
        app.selectbox[0].select("relation:HAS_PLAN_BOM_ITEM").run()
        self.assertEqual(("relation", "HAS_PLAN_BOM_ITEM"), app.session_state["selected_ontology_object"])
        self.assertTrue(any("编译器可以把该关系转换为 JOIN" in item.value for item in app.success))
        app.text_area[0].input("查MRP计划料差").run()
        with patch("material_query.pipeline.RequestRouter") as router:
            router.return_value.parse.return_value = request("name", "MRP")
            app.checkbox[0].uncheck().run()
            app.button[0].click().run()
        self.assertFalse(app.exception)
        self.assertEqual("NEEDS_INPUT", app.session_state["trace"]["status"])

    def test_interactive_graph_contains_entity_and_relation_links(self):
        """Graphviz 输出必须是真正带链接的 SVG 来源，而不是静态关系图。"""
        from material_query.ontology_browser import interactive_ontology_dot
        dot = interactive_ontology_dot(self.registry, "relation", "HAS_PROCESS_TASK")
        self.assertIn('URL="?ontology_entity=ProductionPlan"', dot)
        self.assertIn('URL="?ontology_relation=HAS_PROCESS_TASK"', dot)
        self.assertIn('color="#dc2626"', dot)

    def test_query_hits_highlight_full_graph(self):
        """查询命中应叠加到全量图，而不是另画一张无法浏览的子图。"""
        from material_query.ontology_browser import interactive_ontology_dot
        dot = interactive_ontology_dot(
            self.registry, highlight_entities=["ProductionPlan"],
            highlight_relations=["HAS_PROCESS_TASK"],
        )
        self.assertIn('fillcolor="#dcfce7"', dot)
        self.assertIn('color="#ea580c"', dot)

    def test_streamlit_result_and_ambiguous_selection(self):
        """用外部服务替身覆盖成功回答、候选选择和所有结果标签页渲染。"""
        from streamlit.testing.v1 import AppTest
        app = AppTest.from_file(str(PROJECT / "src/ui.py"), default_timeout=20).run()
        plan = {"id": 665, "code": "P-665", "name": "MRP"}
        with patch("material_query.pipeline.RequestRouter") as router, patch("material_query.database.ReadOnlyMySQLExecutor.execute") as execute, patch("material_query.pipeline.QueryResultAnswerer") as answerer:
            router.return_value.parse.return_value = request()
            execute.side_effect = [{"rows": [plan]}, {"status": "SUCCESS", "database": "mom-test",
                "row_count": 1, "rows": [{"gross_issued_qty": 100}], "grounded_entities": []}]
            answerer.return_value.model_name = "test-model"
            answerer.return_value.answer.return_value = "本次返回领料数量100，单位待确认。"
            app.checkbox[1].check().run()
            app.button[0].click().run()
            self.assertFalse(app.exception)
            self.assertEqual("SUCCESS", app.session_state["trace"]["answer"]["status"])
            self.assertTrue(any("单位待确认" in item.value for item in app.markdown))
            router.return_value.parse.return_value = request("name", "MRP")
            execute.side_effect = [{"rows": [plan, {**plan, "id": 666}]}]
            app.text_area[0].input("查MRP计划料差").run()
            app.button[0].click().run()
            self.assertFalse(app.exception)
            app.button(key="choose_665").click().run()
            self.assertIn("明确选择生产计划ID 665", app.text_area[0].value)
            self.assertFalse(app.exception)


if __name__ == "__main__":
    unittest.main()
