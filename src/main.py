"""料查分析动态 Query IR MVP 的命令行入口。"""

import argparse
import json
import sys
from pathlib import Path

from material_query import MaterialQueryPipeline
from material_query.config import load_json
from material_query.presentation import print_trace


def build_parser() -> argparse.ArgumentParser:
    """定义自然语言、离线 Query IR、追踪展示和真实执行参数。"""

    parser = argparse.ArgumentParser(
        description="自然语言 → Query IR → 校验 → 动态 SQL → mom-test 只读执行"
    )
    parser.add_argument(
        "question",
        nargs="?",
        help="例如：分析生产计划ID 665的投料差异，筛选超过百分之五的记录",
    )
    parser.add_argument("--model", help="临时覆盖 .env 中的 OPENAI_MODEL。")
    parser.add_argument(
        "--from-ir",
        type=Path,
        help="从本地 Candidate Query IR JSON 运行，跳过模型调用。",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="在严格只读事务中连接并查询 mom-test。",
    )
    parser.add_argument("--show-ir", action="store_true", help="展开完整 Query IR JSON。")
    parser.add_argument("--show-sql", action="store_true", help="展开完整参数化 SQL。")
    parser.add_argument(
        "--show-context",
        action="store_true",
        help="展开实际提供给模型的本体规划上下文。",
    )
    parser.add_argument("--json", action="store_true", help="输出完整机器可读追踪 JSON。")
    return parser


def main() -> None:
    """运行 Pipeline，并将预期错误转换为简洁的命令行信息。"""

    parser = build_parser()
    args = parser.parse_args()
    if not args.question and not args.from_ir:
        parser.error("请提供自然语言问题，或使用 --from-ir。")

    question = args.question or "[OFFLINE_QUERY_IR_TEST]"
    try:
        candidate_query_ir = load_json(args.from_ir) if args.from_ir else None
        pipeline = MaterialQueryPipeline(model_name=args.model)
        trace = pipeline.run(
            question=question,
            execute=args.execute,
            candidate_query_ir=candidate_query_ir,
        )
    except Exception as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1) from error

    if args.json:
        print(json.dumps(trace, ensure_ascii=False, indent=2, default=str))
    else:
        print_trace(
            trace,
            show_query_ir=args.show_ir,
            show_sql=args.show_sql,
            show_context=args.show_context,
        )


if __name__ == "__main__":
    main()
