"""覆盖 Query IR 校验和动态 SQL 编译的离线单元测试。"""

import sys
import unittest
from copy import deepcopy
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "src"))

from material_query.compiler import QueryIRCompiler
from material_query.config import EXAMPLES_DIR, load_json
from material_query.ontology import OntologyRegistry
from material_query.planner import select_example_names
from material_query.query_hits import extract_query_hits
from material_query.validator import QueryIRValidator


class DynamicQueryTests(unittest.TestCase):
    """验证不同 Query IR 会生成不同 SQL，并拒绝非法关系。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = OntologyRegistry()
        cls.validator = QueryIRValidator(cls.registry)
        cls.compiler = QueryIRCompiler(cls.registry)

    def test_material_gap_query_is_valid_and_dynamic(self) -> None:
        query_ir = load_json(EXAMPLES_DIR / "query-ir-material-gap.json")
        validation = self.validator.validate(query_ir)
        compiled = self.compiler.compile(query_ir)
        self.assertEqual("VALID", validation["status"])
        self.assertEqual("DYNAMIC_METRIC_QUERY", compiled.mode)
        self.assertIn("issue_agg AS", compiled.sql)
        self.assertIn("severity_result AS", compiled.sql)
        self.assertIn("%(threshold)s", compiled.sql)

    def test_task_query_omits_unrequested_material_branches(self) -> None:
        query_ir = load_json(EXAMPLES_DIR / "query-ir-list-tasks.json")
        self.validator.validate(query_ir)
        compiled = self.compiler.compile(query_ir)
        self.assertEqual("DYNAMIC_ENTITY_TRAVERSAL", compiled.mode)
        self.assertIn("produce_order_make_process_task", compiled.sql)
        self.assertNotIn("produce_pick", compiled.sql)
        self.assertNotIn("produce_report", compiled.sql)
        self.assertNotIn("produce_plan_bom_detail", compiled.sql)

    def test_query_hits_only_include_used_ontology_elements(self) -> None:
        query_ir = load_json(EXAMPLES_DIR / "query-ir-list-tasks.json")
        hits = self.registry.query_hits(query_ir)
        self.assertEqual(
            ["ProductionPlan", "ProcessTask"],
            [entity["id"] for entity in hits["entities"]],
        )
        self.assertEqual(
            ["HAS_PROCESS_TASK"],
            [relation["id"] for relation in hits["relations"]],
        )
        self.assertEqual([], hits["metrics"])
        self.assertEqual(["Scan", "Traverse", "Project", "Limit"], hits["operators"])

    def test_query_hits_supports_legacy_registry_without_new_method(self) -> None:
        query_ir = load_json(EXAMPLES_DIR / "query-ir-list-tasks.json")

        class LegacyRegistry:
            """模拟 Streamlit 缓存的旧注册表实例。"""

            entity_types = self.registry.entity_types
            relations = self.registry.relations
            metrics = self.registry.metrics
            mapping = self.registry.mapping

        legacy_registry = LegacyRegistry()
        self.assertFalse(hasattr(legacy_registry, "query_hits"))
        hits = extract_query_hits(legacy_registry, query_ir)
        self.assertEqual(
            ["ProductionPlan", "ProcessTask"],
            [entity["id"] for entity in hits["entities"]],
        )

    def test_invalid_relation_start_type_is_rejected(self) -> None:
        query_ir = load_json(EXAMPLES_DIR / "query-ir-list-tasks.json")
        invalid = deepcopy(query_ir)
        invalid["steps"][1]["relation"] = "HAS_MATERIAL_ISSUE"
        with self.assertRaisesRegex(ValueError, "必须从 ProcessTask 出发"):
            self.validator.validate(invalid)

    def test_threshold_must_use_absolute_severity(self) -> None:
        query_ir = load_json(EXAMPLES_DIR / "query-ir-material-gap.json")
        invalid = deepcopy(query_ir)
        invalid["steps"][10]["predicates"][0]["metric"] = "signedDifferenceRate"
        with self.assertRaisesRegex(ValueError, "severityRate"):
            self.validator.validate(invalid)

    def test_every_query_ir_example_validates_and_compiles(self) -> None:
        example_paths = sorted(EXAMPLES_DIR.glob("query-ir-*.json"))
        self.assertGreaterEqual(len(example_paths), 8)
        for example_path in example_paths:
            with self.subTest(example=example_path.name):
                query_ir = load_json(example_path)
                self.assertEqual("VALID", self.validator.validate(query_ir)["status"])
                self.assertTrue(self.compiler.compile(query_ir).sql.startswith(("SELECT", "WITH")))

    def test_plan_bom_query_maps_layer_and_material(self) -> None:
        query_ir = load_json(EXAMPLES_DIR / "query-ir-plan-bom.json")
        validation = self.validator.validate(query_ir)
        compiled = self.compiler.compile(query_ir)
        self.assertEqual(
            ["HAS_PLAN_BOM_ITEM", "BOM_ITEM_MATERIAL"],
            validation["relation_path"],
        )
        self.assertIn("produce_plan_bom_detail", compiled.sql)
        self.assertIn("design_product", compiled.sql)
        self.assertIn("ORDER BY bom_layer ASC", compiled.sql)

    def test_plan_count_uses_only_production_plan(self) -> None:
        query_ir = load_json(EXAMPLES_DIR / "query-ir-plan-count.json")
        self.validator.validate(query_ir)
        compiled = self.compiler.compile(query_ir)
        self.assertEqual("DYNAMIC_ENTITY_COUNT", compiled.mode)
        self.assertIn("COUNT(p.id) AS production_plan_count", compiled.sql)
        self.assertIn("produce_plan", compiled.sql)
        self.assertNotIn("produce_order_make_process_task", compiled.sql)
        self.assertNotIn("produce_plan_bom_detail", compiled.sql)

    def test_planner_selects_domain_specific_examples(self) -> None:
        self.assertEqual(
            "query-ir-plan-bom.json",
            select_example_names("分析生产计划ID 665的对应的BOM")[0],
        )
        self.assertEqual(
            "query-ir-plan-count.json",
            select_example_names("当前有多少生产计划")[0],
        )
        self.assertEqual(
            "query-ir-returned-quantity.json",
            select_example_names("查询生产计划ID 665的退料量")[0],
        )

    def test_overview_distinguishes_logical_and_executable_relations(self) -> None:
        summary = self.registry.summary()
        overview = self.registry.overview_context()
        self.assertEqual(10, summary["entity_count"])
        self.assertEqual(7, summary["executable_entity_count"])
        self.assertEqual(10, len(overview["entities"]))
        self.assertEqual(
            7,
            sum(entity["mvpExecutable"] for entity in overview["entities"]),
        )
        self.assertEqual(13, summary["relation_count"])
        self.assertEqual(6, summary["executable_relation_count"])
        self.assertEqual(13, len(overview["relations"]))
        self.assertEqual(
            6,
            sum(relation["mvpExecutable"] for relation in overview["relations"]),
        )


if __name__ == "__main__":
    unittest.main()
