"""离线覆盖五轮上下文改写、查询复用和 Streamlit 对话记录。"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from material_query.conversation import rewrite_question, summarize_recent_turns
from material_query.pipeline import MaterialQueryPipeline


def parsed_request(threshold=None):
    """模拟模型输出；所有真实数据库调用在测试中被只读替身覆盖。"""

    return {"route": "analyze_material_gap", "selector": {
        "entityType": "ProductionPlan", "property": "sourceId", "operator": "EQ", "value": 665},
        "threshold": threshold, "comparison": "GT", "limit": 100, "clarification": ""}


def turn(question="分析生产计划ID 665的料差", plan_id=665, status="SUCCESS"):
    """构造只有解析与执行成功才可继承的历史轮次。"""

    return {"question": question, "trace": {"execution": {"status": status},
            "resolution": {"status": "RESOLVED", "sourceId": plan_id},
            "request": {"route": "analyze_material_gap"},
            "planning": {"candidate_query_ir": {"goal": question}}}}


class ConversationTests(unittest.TestCase):
    def test_only_last_five_compact_summaries(self):
        history = [turn(f"第{i}轮计划料差") for i in range(7)]
        history[-1]["trace"]["compilation"] = {"sql": "SECRET_SQL"}
        history[-1]["trace"]["execution"]["rows"] = [{"secret": "SECRET_ROW"}]
        summaries = summarize_recent_turns(history)
        self.assertEqual(5, len(summaries))
        self.assertEqual("第2轮计划料差", summaries[0]["question"])
        self.assertNotIn("SECRET_SQL", str(summaries))
        self.assertNotIn("SECRET_ROW", str(summaries))

    def test_followup_inherits_verified_plan_not_old_threshold(self):
        history = [turn("分析生产计划ID 665的料差，超过5%")]
        context = rewrite_question("改成超过10%", history)
        self.assertEqual("REFINE", context["mode"])
        self.assertEqual(665, context["inherited_plan_id"])
        self.assertIn("生产计划ID 665", context["effective_question"])
        self.assertIn("超过10%", context["effective_question"])
        self.assertNotIn("超过5%", context["effective_question"])

    def test_explicit_new_scope_and_global_count_override_history(self):
        history = [turn()]
        for question in ("查生产计划ID 777的BOM", "查计划编号 SCJH-2025-1的BOM"):
            context = rewrite_question(question, history)
            self.assertEqual("CHANGE_SCOPE", context["mode"])
            self.assertEqual(question, context["effective_question"])
            self.assertIsNone(context["inherited_plan_id"])
        count = rewrite_question("当前有多少生产计划", history)
        self.assertEqual("NEW_QUERY", count["mode"])
        self.assertIsNone(count["inherited_plan_id"])

    def test_failed_or_global_latest_turn_is_not_anchor(self):
        failed = turn(status="NOT_EXECUTED")
        self.assertEqual("NEW_QUERY", rewrite_question("再看BOM", [failed])["mode"])
        global_turn = turn("当前有多少生产计划")
        global_turn["trace"].pop("resolution")
        self.assertEqual("NEW_QUERY", rewrite_question("再看BOM", [turn(), global_turn])["mode"])

    def test_ambiguous_and_reset_are_non_executable(self):
        self.assertEqual("CLARIFY", rewrite_question("它呢", [turn()])["mode"])
        self.assertEqual("CLARIFY", rewrite_question("换一个计划继续分析", [turn()])["mode"])
        self.assertEqual("RESET", rewrite_question("重新开始", [turn()])["mode"])

    @patch("material_query.pipeline.RequestRouter")
    def test_pipeline_records_rewrite_and_independent_task_ir(self, router):
        router.return_value.parse.return_value = parsed_request(0.1)
        pipeline = MaterialQueryPipeline()
        trace = pipeline.run("改成超过10%", conversation_history=[turn()])
        self.assertEqual("REFINE", trace["context"]["mode"])
        self.assertEqual("REGISTERED_TASK", trace["planning"]["source"])
        self.assertEqual(0.1, trace["planning"]["candidate_query_ir"]["parameters"]["threshold"])
        self.assertEqual(trace["input"]["rewritten_question"],
                         trace["planning"]["candidate_query_ir"]["goal"])
        router.return_value.parse.assert_called_once()
        self.assertEqual(1, len(router.return_value.parse.call_args.args[1]))

    @patch("material_query.pipeline.RequestRouter")
    def test_two_turns_reach_parameterized_sql_and_execution(self, router):
        """两轮均走定位、Query IR、编译和只读执行；模型换 ID 会被守卫纠正。"""

        router.return_value.parse.side_effect = lambda question, _history: {
            **parsed_request(0.1 if "超过10%" in question else 0.05),
            "selector": {"entityType": "ProductionPlan", "property": "sourceId",
                         "operator": "EQ", "value": 777},
        }
        pipeline = MaterialQueryPipeline()
        plan = {"id": 665, "code": "P-665", "name": "测试计划"}
        sql_result = {"status": "SUCCESS", "database": "mom-test", "row_count": 1,
                      "rows": [{"severity_rate": 0.12}], "grounded_entities": []}
        with patch.object(pipeline.executor, "execute", side_effect=[
            {"rows": [plan]}, sql_result, {"rows": [plan]}, sql_result,
        ]) as execute:
            first = pipeline.run("分析生产计划ID 665的料差，超过5%", execute=True)
            second = pipeline.run("改成超过10%", execute=True,
                                  conversation_history=[{"question": first["input"]["natural_language"],
                                                         "trace": first}])
        self.assertEqual(4, execute.call_count)
        self.assertEqual(665, first["request"]["selector"]["value"])
        self.assertEqual(665, second["request"]["selector"]["value"])
        self.assertIn("selector_guard", second)
        self.assertEqual("SUCCESS", second["execution"]["status"])
        self.assertEqual(0.1, second["compilation"]["parameters"]["threshold"])
        self.assertTrue(second["compilation"]["sql"].startswith("WITH"))
        self.assertIn("Scan", second["ontology"]["query_hits"]["operators"])

    def test_ui_keeps_per_turn_traces_and_can_reset(self):
        from streamlit.testing.v1 import AppTest
        app = AppTest.from_file(str(PROJECT / "src/ui.py"), default_timeout=20).run()
        app.text_area[0].input("分析生产计划ID 665的料差，超过5%").run()
        with patch("material_query.pipeline.RequestRouter") as router, \
             patch("material_query.database.ReadOnlyMySQLExecutor.execute") as execute:
            router.return_value.parse.side_effect = lambda question, _context: parsed_request(
                0.1 if "超过10%" in question else 0.05
            )
            plan = {"id": 665, "code": "P-665", "name": "测试计划"}
            sql_result = {"status": "SUCCESS", "database": "mom-test", "row_count": 1,
                          "rows": [{"severity_rate": 0.12}], "grounded_entities": []}
            execute.side_effect = [{"rows": [plan]}, sql_result, {"rows": [plan]}, sql_result]
            app.button[0].click().run()
            app.text_area[0].input("改成超过10%").run()
            app.button[0].click().run()
        self.assertFalse(app.exception)
        self.assertEqual(2, len(app.session_state["chat_turns"]))
        self.assertEqual("REFINE", app.session_state["trace"]["context"]["mode"])
        self.assertEqual(0.1, app.session_state["trace"]["compilation"]["parameters"]["threshold"])
        app.button(key="view_chat_turn_0").click().run()
        self.assertEqual(0.05, app.session_state["trace"]["compilation"]["parameters"]["threshold"])
        app.button(key="reset_chat_context").click().run()
        self.assertEqual([], app.session_state["chat_turns"])
        self.assertNotIn("trace", app.session_state)
        self.assertFalse(app.exception)

    def test_natural_language_reset_clears_previous_turns(self):
        from streamlit.testing.v1 import AppTest
        app = AppTest.from_file(str(PROJECT / "src/ui.py"), default_timeout=20).run()
        app.text_area[0].input("重新开始").run()
        app.button[0].click().run()
        self.assertEqual([], app.session_state["chat_turns"])
        self.assertNotIn("trace", app.session_state)
        self.assertFalse(app.exception)


if __name__ == "__main__":
    unittest.main()
