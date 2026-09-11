"""在 mom-test 上只读执行全部 Query IR 样例，用于人工触发的冒烟测试。"""

import sys
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "src"))

from material_query.config import EXAMPLES_DIR, load_json
from material_query.pipeline import MaterialQueryPipeline


def main() -> None:
    """跳过模型，逐个校验、编译并只读执行样例。"""

    pipeline = MaterialQueryPipeline()
    failed = []
    for example_path in sorted(EXAMPLES_DIR.glob("query-ir-*.json")):
        try:
            trace = pipeline.run(
                question=f"[冒烟测试] {example_path.name}",
                execute=True,
                candidate_query_ir=load_json(example_path),
            )
            execution = trace["execution"]
            print(
                f"PASS {example_path.name}: "
                f"mode={trace['compilation']['mode']}, rows={execution['row_count']}"
            )
        except Exception as error:
            failed.append(example_path.name)
            print(f"FAIL {example_path.name}: {error}")

    if failed:
        raise SystemExit(f"只读冒烟测试失败：{failed}")


if __name__ == "__main__":
    main()
