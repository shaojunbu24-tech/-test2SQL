"""这个文件实现 MVP 的完整边界：自然语言输入到参数化 SQL 输出。"""

# argparse 用于读取命令行中的自然语言问题和调试参数。
import argparse
# json 用于解析模型返回的结构化 JSON，并把最终结果打印为 JSON。
import json
# os 用于读取 OPENAI_MODEL 等环境变量。
import os
# sys 用于把错误信息输出到标准错误流。
import sys
# Path 用于稳定地定位项目中的 YAML 和示例文件。
from pathlib import Path
# Any 为字典中的动态 JSON 值提供简洁的类型标注。
from typing import Any

# yaml 用于加载人可以直接维护的本体和数据库映射文件。
import yaml
# PROJECT_DIR 指向 mvp-text-to-sql 目录，不依赖程序从哪里启动。
PROJECT_DIR = Path(__file__).resolve().parent.parent
# ONTOLOGY_PATH 指向会完整发送给模型的 MVP 本体文件。
ONTOLOGY_PATH = PROJECT_DIR / "config" / "ontology.yaml"
# MAPPING_PATH 指向只供本地 SQL 编译器使用、不会发送给模型的映射文件。
MAPPING_PATH = PROJECT_DIR / "config" / "mapping.yaml"
# ENV_PATH 指向本地运行配置，API 密钥只保存在这个被忽略的文件中。
ENV_PATH = PROJECT_DIR / ".env"

# ONTOLOGY_QUERY_SCHEMA 定义兼容模型必须返回、且本地程序会再次校验的结构。
ONTOLOGY_QUERY_SCHEMA: dict[str, Any] = {
    # type=object 表示模型最外层必须输出 JSON 对象。
    "type": "object",
    # additionalProperties=false 禁止模型增加 SQL、表名等未登记字段。
    "additionalProperties": False,
    # required 要求五个字段始终存在，便于后端使用固定逻辑处理。
    "required": ["intent", "plan_mention", "plan_id", "threshold", "limit"],
    # properties 定义每个输出字段允许的类型和值域。
    "properties": {
        # intent 只能从 MVP 已登记的四个意图中选择。
        "intent": {
            # enum 从结构层面阻止模型发明新意图。
            "enum": [
                # GET_PRODUCTION_PLAN 表示查询计划基本信息。
                "GET_PRODUCTION_PLAN",
                # LIST_PROCESS_TASKS 表示查询计划下的工序任务。
                "LIST_PROCESS_TASKS",
                # ANALYZE_MATERIAL_GAP 表示生成计划料差分析 SQL。
                "ANALYZE_MATERIAL_GAP",
                # UNSUPPORTED 表示问题超出当前本体能力。
                "UNSUPPORTED",
            ]
        },
        # plan_mention 保存用户原文中表示计划的短语，便于显示实体命中过程。
        "plan_mention": {"type": ["string", "null"]},
        # plan_id 只能是正整数或 null，且必须来自用户明确输入。
        "plan_id": {"type": ["integer", "null"], "minimum": 1},
        # threshold 使用 0 到 1 的小数；用户未提供时返回 null。
        "threshold": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
        # limit 用来限制最终 SQL 返回行数，避免无界查询。
        "limit": {"type": "integer", "minimum": 1, "maximum": 200},
    },
}


# load_local_env 读取简单的 KEY=VALUE 配置，不覆盖终端已经设置的环境变量。
def load_local_env(path: Path) -> None:
    # 本地配置文件不存在时直接返回，让部署环境继续使用系统环境变量。
    if not path.exists():
        # return 表示这里不是错误，调用方稍后会检查真正必需的变量。
        return
    # splitlines 把 UTF-8 文件安全拆成逐行配置。
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        # strip 去掉行首尾空格，方便识别空行和注释。
        line = raw_line.strip()
        # 空行和以井号开头的说明行不包含环境变量。
        if not line or line.startswith("#"):
            # continue 继续读取下一行。
            continue
        # 没有等号的行不是当前 MVP 支持的配置格式。
        if "=" not in line:
            # ValueError 指出配置文件中存在无法解析的行。
            raise ValueError(f".env 中存在无效配置行：{raw_line}")
        # split 只切分第一个等号，避免密钥或地址中存在等号时被截断。
        key, value = line.split("=", 1)
        # strip 清理变量名周围可能存在的空格。
        key = key.strip()
        # strip 和 strip 引号让常见的 KEY=value 与 KEY="value" 都能工作。
        value = value.strip().strip('"').strip("'")
        # setdefault 保证终端显式设置的变量优先于本地 .env。
        os.environ.setdefault(key, value)


# load_text 读取需要原样发送给模型的 UTF-8 文本文件。
def load_text(path: Path) -> str:
    # read_text 会在文件不存在或编码错误时立即抛出明确异常。
    return path.read_text(encoding="utf-8")


# load_yaml 读取只供本地程序使用的 YAML，并确保最外层是字典。
def load_yaml(path: Path) -> dict[str, Any]:
    # with 会确保文件读取完成后及时关闭文件句柄。
    with path.open("r", encoding="utf-8") as file:
        # safe_load 禁止 YAML 构造任意 Python 对象，适合读取配置。
        value = yaml.safe_load(file)
    # 配置最外层必须是字典，否则后续按键读取会产生难理解的错误。
    if not isinstance(value, dict):
        # ValueError 明确指出是配置结构错误，而不是模型或数据库错误。
        raise ValueError(f"YAML 文件最外层必须是对象：{path}")
    # 返回已经解析的普通 Python 字典。
    return value


# parse_with_model 把用户问题和完整 MVP 本体交给模型并获取本体查询 JSON。
def parse_with_model(question: str, ontology_text: str, model: str) -> dict[str, Any]:
    # try 让离线 --from-json 模式不必提前安装 OpenAI SDK。
    try:
        # OpenAI 是官方 Python SDK 提供的客户端入口，只在实际调用模型时导入。
        from openai import OpenAI
    # ImportError 表示还没有安装 requirements.txt 中声明的 SDK。
    except ImportError as error:
        # RuntimeError 给出明确安装方式，比原始模块缺失错误更容易操作。
        raise RuntimeError("缺少 openai 包，请先执行：python3 -m pip install -r requirements.txt") from error
    # api_key 从本地 .env 或终端环境变量读取，不会进入本体和模型提示词。
    api_key = os.getenv("OPENAI_API_KEY")
    # 缺少密钥时在发起网络请求前返回清晰错误。
    if not api_key:
        # ValueError 不会输出任何真实密钥内容。
        raise ValueError("请在 .env 或环境变量中设置 OPENAI_API_KEY。")
    # base_url 允许使用 DeepSeek 等 OpenAI 兼容服务；未配置时使用 OpenAI 默认地址。
    base_url = os.getenv("OPENAI_BASE_URL")
    # OpenAI 客户端接收密钥和可选兼容服务地址。
    client = OpenAI(api_key=api_key, base_url=base_url)
    # instructions 固定模型职责，明确它只能做语义解析而不能生成 SQL。
    instructions = (
        "你是生产制造领域的语义解析器。"
        "请根据给定的完整本体，把用户问题转换成符合 Schema 的本体查询。"
        "plan_id 只能提取用户明确说出的数字，不能猜测。"
        "plan_mention 必须保留用户原文中表示生产计划的短语；没有计划短语时返回 null。"
        "不要输出 SQL、表名、字段名、解释文字或 Schema 之外的字段。"
        "如果问题不受本体支持，intent 必须选择 UNSUPPORTED。"
        "必须只输出一个 JSON 对象。"
    )
    # input_text 把稳定的完整本体放在前面，把每次变化的用户问题放在最后。
    input_text = f"完整本体如下：\n\n{ontology_text}\n\n用户问题：\n{question}"
    # schema_text 把字段约束明确提供给仅支持 JSON mode 的兼容服务。
    schema_text = json.dumps(ONTOLOGY_QUERY_SCHEMA, ensure_ascii=False)
    # system_message 同时包含职责、JSON Schema 和输出示例，提升兼容模型的稳定性。
    system_message = (
        f"{instructions}\n\n"
        f"输出必须符合下面的 JSON Schema：\n{schema_text}\n\n"
        "JSON 示例："
        '{"intent":"ANALYZE_MATERIAL_GAP","plan_mention":"生产计划ID 665",'
        '"plan_id":665,"threshold":0.05,"limit":100}'
    )
    # chat.completions.create 使用 DeepSeek 官方支持的 OpenAI 兼容 Chat Completions 接口。
    response = client.chat.completions.create(
        # model 使用启动参数或 OPENAI_MODEL 指定，避免把模型名称写死在代码里。
        model=model,
        # messages 分别传递高优先级规则和包含完整本体的用户输入。
        messages=[
            # system 消息约束输出必须是指定 JSON。
            {"role": "system", "content": system_message},
            # user 消息包含完整本体和当前自然语言问题。
            {"role": "user", "content": input_text},
        ],
        # response_format=json_object 开启兼容服务的 JSON 输出模式。
        response_format={"type": "json_object"},
        # max_tokens 给小型结构化输出留出足够空间，同时避免无界输出。
        max_tokens=1000,
    )
    # content 是 Chat Completions 返回的最终 JSON 字符串。
    content = response.choices[0].message.content
    # 空输出不能进入 SQL 编译阶段，否则错误位置会变得不清晰。
    if not content:
        # 空输出不能进入 SQL 编译阶段，否则错误位置会变得不清晰。
        raise ValueError("模型没有返回本体查询结果。")
    # json.loads 把已经符合 Schema 的 JSON 文本转换成 Python 字典。
    parsed = json.loads(content)
    # 防御性检查确保解析结果仍然是对象，而不是其他合法 JSON 类型。
    if not isinstance(parsed, dict):
        # ValueError 阻止异常结构继续流入 SQL 编译器。
        raise ValueError("模型输出必须是 JSON 对象。")
    # 返回尚未接触任何数据库表名的本体查询对象。
    return parsed


# validate_query 在模型 Schema 之外再次检查 MVP 的业务边界。
def validate_query(query: dict[str, Any], mapping: dict[str, Any]) -> None:
    # expected_fields 是模型输出允许存在的完整字段集合。
    expected_fields = {"intent", "plan_mention", "plan_id", "threshold", "limit"}
    # 字段缺失或出现额外字段都说明兼容服务没有完全遵守约定。
    if set(query) != expected_fields:
        # ValueError 同时显示缺少和多出的字段，方便调试提示词。
        raise ValueError(
            f"本体查询字段不符合约定；缺少={expected_fields - set(query)}，多出={set(query) - expected_fields}"
        )
    # intent 是本体查询到 SQL 模板之间唯一允许使用的路由键。
    intent = query.get("intent")
    # UNSUPPORTED 明确停止链路，不能为了返回 SQL 而猜测用户意图。
    if intent == "UNSUPPORTED":
        # ValueError 会被命令行入口转成清晰的错误消息。
        raise ValueError("当前 MVP 本体不支持这个问题。")
    # queries 是 Mapping 中人工登记、允许生成的全部物理查询。
    queries = mapping.get("queries", {})
    # 未登记意图不能进入 SQL 编译阶段。
    if intent not in queries:
        # 该检查同时防止模型输出被篡改后选择任意查询。
        raise ValueError(f"没有找到意图对应的 SQL 映射：{intent}")
    # selected_mapping 取出当前意图对应的参数白名单和 SQL 模板。
    selected_mapping = queries[intent]
    # required_parameters 是当前 SQL 必须具备的业务参数列表。
    required_parameters = selected_mapping.get("required_parameters", [])
    # 逐个检查必填参数，当前 MVP 最重要的是 plan_id。
    for parameter_name in required_parameters:
        # null 和缺失都不能满足必填参数要求。
        if query.get(parameter_name) is None:
            # 错误信息直接指出用户还需要补充什么。
            raise ValueError(f"查询缺少必填参数：{parameter_name}")
    # plan_id 存在时必须是正整数，不能接受布尔值或字符串拼接内容。
    if query.get("plan_id") is not None and (
        # bool 是 int 的子类，因此这里显式排除 True 和 False。
        isinstance(query["plan_id"], bool)
        # 非整数或非正数都不是有效的计划源 ID。
        or not isinstance(query["plan_id"], int)
        # 小于一的数值在当前数据库主键约定下无效。
        or query["plan_id"] < 1
    ):
        # ValueError 阻止错误 ID 被绑定进 SQL 参数。
        raise ValueError("plan_id 必须是大于 0 的整数。")
    # threshold 存在时必须是普通数字并且位于 0 到 1 之间。
    if query.get("threshold") is not None and (
        # bool 同样不能作为阈值使用。
        isinstance(query["threshold"], bool)
        # int 和 float 都允许作为数值阈值。
        or not isinstance(query["threshold"], (int, float))
        # 小于零的绝对差异率阈值没有业务意义。
        or query["threshold"] < 0
        # 大于一的值不符合本 MVP 的百分比小数约定。
        or query["threshold"] > 1
    ):
        # ValueError 提醒调用方阈值应使用例如 0.05 的形式。
        raise ValueError("threshold 必须是 0 到 1 之间的小数或 null。")
    # limit 必须是 1 到 200 之间的整数，以控制查询规模。
    if (
        # bool 不能被当成行数限制。
        isinstance(query.get("limit"), bool)
        # 非整数不能直接绑定到 LIMIT。
        or not isinstance(query.get("limit"), int)
        # 最少需要允许返回一行。
        or query["limit"] < 1
        # 当前 MVP 最多允许返回二百行。
        or query["limit"] > 200
    ):
        # ValueError 阻止无界或异常大的输出。
        raise ValueError("limit 必须是 1 到 200 之间的整数。")


# build_ontology_trace 展开实体命中、关系连接和 SQL 映射三个可观察阶段。
def build_ontology_trace(
    # question 是最初的自然语言输入，用于保留链路起点。
    question: str,
    # query 是模型生成并通过本地校验的本体查询。
    query: dict[str, Any],
    # ontology 保存意图、实体和关系定义。
    ontology: dict[str, Any],
    # mapping 保存意图到 SQL 模板的物理映射。
    mapping: dict[str, Any],
) -> dict[str, Any]:
    # intent_id 是模型命中的本体意图 ID。
    intent_id = query["intent"]
    # intent_definition 提供查询起点、关系路径和指标集合。
    intent_definition = ontology["intents"][intent_id]
    # relation_connections 用于保存已经从本体定义展开的关系边。
    relation_connections: list[dict[str, Any]] = []
    # 逐个读取意图登记的关系路径，模型不能修改这个列表。
    for relation_id in intent_definition.get("relation_path", []):
        # relation_definition 包含当前关系的起点类型和终点类型。
        relation_definition = ontology["relations"][relation_id]
        # append 生成一条便于人和程序共同阅读的本体关系记录。
        relation_connections.append(
            {
                # relation 保存稳定的本体关系 ID。
                "relation": relation_id,
                # from 保存关系的起点实体类型。
                "from": relation_definition["from"],
                # to 保存关系的终点实体类型。
                "to": relation_definition["to"],
            }
        )
    # entity_hits 保存从自然语言中提取出的实体类型和标识。
    entity_hits: list[dict[str, Any]] = []
    # 当前三个受支持意图都需要用户明确提供生产计划 ID。
    if query.get("plan_id") is not None:
        # append 记录语义层已经完成的实体类型和 ID 提取。
        entity_hits.append(
            {
                # mention 尽量保留用户原文中的实体短语。
                "mention": query.get("plan_mention"),
                # entity_type 来自当前意图登记的查询起点。
                "entity_type": intent_definition.get("root_entity"),
                # identifier_kind 明确 665 是源系统 ID，而不是内部规范 ID。
                "identifier_kind": "source_id",
                # identifier_value 是从用户问题中提取出的数字。
                "identifier_value": query["plan_id"],
                # status 明确当前边界尚未连接数据库验证这条实例是否真实存在。
                "status": "TYPE_AND_ID_EXTRACTED_NOT_DATABASE_VERIFIED",
            }
        )
    # 返回完整、可观察的本体编译轨迹。
    return {
        # natural_language 保存链路最初输入。
        "natural_language": question,
        # matched_intent 说明模型最终选择了哪个本体能力。
        "matched_intent": {
            # id 是稳定的意图标识。
            "id": intent_id,
            # description 是本体 YAML 中对意图的业务说明。
            "description": intent_definition["description"],
        },
        # entity_hits 展示语言中的实体命中结果。
        "entity_hits": entity_hits,
        # relation_connections 展示意图在本体中需要连接的关系。
        "relation_connections": relation_connections,
        # metrics 展示该意图登记并由 SQL 实现的业务指标。
        "metrics": intent_definition.get("metrics", []),
        # sql_mapping 展示最终选中的物理 Mapping，但不包含密钥。
        "sql_mapping": {
            # mapping_version 用于复现本次本体到数据库的转换。
            "mapping_version": mapping.get("version"),
            # template_id 当前直接使用稳定意图 ID 作为 SQL 模板 ID。
            "template_id": intent_id,
        },
    }


# compile_sql 把已经校验的本体查询映射成固定 SQL 和参数字典。
def compile_sql(
    # question 是用户原始问题，用于构建可观察轨迹。
    question: str,
    # query 是已经校验的模型结构化输出。
    query: dict[str, Any],
    # ontology 用于从意图展开实体、关系和指标。
    ontology: dict[str, Any],
    # mapping 用于选择固定 SQL 模板。
    mapping: dict[str, Any],
) -> dict[str, Any]:
    # intent 已经通过 validate_query 确认存在于 Mapping 中。
    intent = query["intent"]
    # selected_mapping 是这次本体查询唯一允许使用的物理查询定义。
    selected_mapping = mapping["queries"][intent]
    # parameter_names 是该 SQL 模板允许绑定的参数白名单。
    parameter_names = selected_mapping.get("parameters", [])
    # parameters 只从白名单复制值，其他模型输出无法进入 SQL 执行参数。
    parameters = {name: query.get(name) for name in parameter_names}
    # 返回 SQL 模板、绑定参数以及映射版本，供下一阶段审计或执行。
    return {
        # mapping_version 记录物理映射版本。
        "mapping_version": mapping.get("version"),
        # ontology_query 保留模型生成且已经校验的本体查询。
        "ontology_query": query,
        # ontology_trace 展示语言、实体、关系和 SQL Mapping 的完整转换过程。
        "ontology_trace": build_ontology_trace(question, query, ontology, mapping),
        # sql 完全来自本地 Mapping，模型没有修改它的机会。
        "sql": selected_mapping["sql"],
        # parameters 与 SQL 分离，避免通过字符串拼接产生注入风险。
        "parameters": parameters,
    }


# build_argument_parser 定义启动程序时可以使用的最小参数集合。
def build_argument_parser() -> argparse.ArgumentParser:
    # ArgumentParser 自动生成 --help 文档。
    parser = argparse.ArgumentParser(
        # description 说明默认只生成 SQL，显式指定参数后才执行只读查询。
        description="把自然语言转换为本体查询，再编译成参数化 SQL；--execute 可执行只读查询。"
    )
    # question 是正常运行时输入的自然语言问题。
    parser.add_argument(
        # 参数名会映射到 args.question。
        "question",
        # nargs=? 允许在使用 --from-json 调试时省略自然语言问题。
        nargs="?",
        # help 会显示在 python src/main.py --help 的输出中。
        help="例如：分析生产计划ID 665的投料差异，筛选超过百分之五的记录",
    )
    # --model 允许临时覆盖 OPENAI_MODEL 环境变量。
    parser.add_argument(
        # 命令行参数名称。
        "--model",
        # default 优先读取环境变量，不在代码中写死模型名称。
        default=os.getenv("OPENAI_MODEL"),
        # help 说明该值必须对应账号中可用的模型。
        help="OpenAI 模型名称；默认读取 OPENAI_MODEL。",
    )
    # --from-json 允许绕过模型，直接测试本体查询到 SQL 的后半段。
    parser.add_argument(
        # 命令行参数名称。
        "--from-json",
        # type=Path 会把字符串路径转换为 Path 对象。
        type=Path,
        # help 说明这个参数用于离线冒烟测试。
        help="读取一个本体查询 JSON，跳过模型调用。",
    )
    # --json 保留原来的完整机器可读输出，默认不再打印转义后的大段 JSON。
    parser.add_argument(
        # 命令行参数名称。
        "--json",
        # action=store_true 表示出现该参数时值为 True。
        action="store_true",
        # help 说明该模式主要用于接口联调和保存调试结果。
        help="输出完整 JSON；默认输出便于阅读的分段结果。",
    )
    # --show-sql 在默认简洁模式下额外显示完整格式化 SQL。
    parser.add_argument(
        # 命令行参数名称。
        "--show-sql",
        # action=store_true 表示只有显式指定时才打印长 SQL。
        action="store_true",
        # help 说明默认模式只显示 SQL 模板和参数摘要。
        help="在简洁输出中展开完整 SQL。",
    )
    # --execute 使用 .env 中的数据库配置执行已经生成的只读 SQL。
    parser.add_argument(
        # 命令行参数名称。
        "--execute",
        # action=store_true 表示默认仍然只生成 SQL，不连接数据库。
        action="store_true",
        # help 明确该功能只用于已登记的只读 SQL 模板。
        help="连接 MySQL 并执行生成的只读 SQL。",
    )
    # 返回已经配置完成的命令行解析器。
    return parser


# run 串联自然语言、本体查询校验和 SQL 编译三个阶段。
def run(args: argparse.Namespace) -> dict[str, Any]:
    # ontology 既用于本地展开关系，也会以原始文本形式完整交给模型。
    ontology = load_yaml(ONTOLOGY_PATH)
    # mapping 只在本地加载，绝不会拼进发送给模型的输入中。
    mapping = load_yaml(MAPPING_PATH)
    # 如果指定了 --from-json，就直接读取结构化查询进行离线测试。
    if args.from_json:
        # read_text 读取 UTF-8 示例文件，json.loads 负责解析内容。
        query = json.loads(args.from_json.read_text(encoding="utf-8"))
        # 调试文件最外层同样必须是 JSON 对象。
        if not isinstance(query, dict):
            # ValueError 保持线上和离线入口的错误语义一致。
            raise ValueError("--from-json 文件最外层必须是 JSON 对象。")
    else:
        # 正常模式必须提供一个非空自然语言问题。
        if not args.question or not args.question.strip():
            # ValueError 比模型空输入报错更容易理解。
            raise ValueError("请提供自然语言问题，或使用 --from-json。")
        # 正常模式必须明确模型名称，避免默默调用错误模型。
        if not args.model:
            # 错误信息给出两种设置模型名称的方式。
            raise ValueError("请设置 OPENAI_MODEL，或通过 --model 指定模型名称。")
        # ontology_text 是当前 MVP 的完整本体文本。
        ontology_text = load_text(ONTOLOGY_PATH)
        # 模型只负责把自然语言转换成本体查询 JSON。
        query = parse_with_model(args.question.strip(), ontology_text, args.model)
    # 本地程序再次校验本体查询和 Mapping 的契约。
    validate_query(query, mapping)
    # SQL 编译器从固定 Mapping 中选模板并绑定白名单参数。
    # --from-json 模式没有真实自然语言，因此使用清晰的离线测试标记。
    source_question = args.question or "[OFFLINE_JSON_TEST]"
    # SQL 编译同时返回完整 ontology_trace。
    result = compile_sql(source_question, query, ontology, mapping)
    # 只有用户显式指定 --execute 时才连接数据库。
    if args.execute:
        # execution 保存真实查询的字段、行数和结果行。
        result["execution"] = execute_readonly_query(result["sql"], result["parameters"])
    # 返回生成结果以及可能存在的真实查询结果。
    return result


# execute_readonly_query 使用本地环境变量连接 MySQL 并执行已登记 SQL。
def execute_readonly_query(sql: str, parameters: dict[str, Any]) -> dict[str, Any]:
    # statement_sql 去掉开头空白，准备识别 YAML 模板中的说明注释。
    statement_sql = sql.lstrip()
    # 循环跳过语句最前面的单行 SQL 注释，但不会修改真正执行的原 SQL。
    while statement_sql.startswith("--"):
        # partition 按第一个换行拆出当前注释行和剩余 SQL。
        _comment_line, separator, statement_sql = statement_sql.partition("\n")
        # 没有剩余内容说明模板中只有注释，不允许执行。
        if not separator:
            # ValueError 在建立连接前中止空 SQL 模板。
            raise ValueError("SQL 模板不包含可执行的查询语句。")
        # lstrip 清理下一行前的缩进，继续判断是否仍是开头注释。
        statement_sql = statement_sql.lstrip()
    # normalized_sql 转成大写，只用于检查注释后的第一条语句类型。
    normalized_sql = statement_sql.upper()
    # 当前执行器只允许普通 SELECT 或以 WITH 开头的 CTE 查询。
    if not normalized_sql.startswith(("SELECT", "WITH")):
        # ValueError 在建立数据库连接前拒绝任何写入或管理语句。
        raise ValueError("真实查询模式只允许执行 SELECT 或 WITH 查询。")
    # required_keys 是连接真实数据库必须提供的环境变量。
    required_keys = ["DB_HOST", "DB_USER", "DB_PASSWORD", "DB_NAME"]
    # missing_keys 收集没有配置或配置为空的变量名称，不输出任何密码值。
    missing_keys = [key for key in required_keys if not os.getenv(key)]
    # 缺少任一连接信息时不发起网络连接。
    if missing_keys:
        # ValueError 只显示变量名，避免敏感值进入日志。
        raise ValueError(f"缺少数据库连接配置：{', '.join(missing_keys)}")
    # try 让不使用 --execute 的开发者不必提前安装 MySQL Connector。
    try:
        # mysql.connector 是官方 MySQL Python 驱动的连接入口。
        import mysql.connector
    # ImportError 表示虚拟环境还没有安装新增依赖。
    except ImportError as error:
        # RuntimeError 给出明确的依赖安装命令。
        raise RuntimeError("缺少 mysql-connector-python，请重新安装 requirements.txt") from error
    # connection 初始设为 None，便于 finally 中安全判断是否需要关闭。
    connection = None
    # cursor 初始设为 None，便于 finally 中安全关闭游标。
    cursor = None
    # try 确保查询成功或失败时都关闭数据库资源。
    try:
        # connect 使用 .env 或终端环境变量建立 MySQL 连接。
        connection = mysql.connector.connect(
            # host 是数据库主机名或 IP。
            host=os.environ["DB_HOST"],
            # port 转换为整数；未配置时使用 MySQL 默认端口 3306。
            port=int(os.getenv("DB_PORT", "3306")),
            # user 必须使用只读账号。
            user=os.environ["DB_USER"],
            # password 只传给驱动，不会打印到结果。
            password=os.environ["DB_PASSWORD"],
            # database 限制默认数据库范围。
            database=os.environ["DB_NAME"],
            # connection_timeout 防止网络不可达时长期阻塞。
            connection_timeout=10,
            # autocommit=false 保证查询可以在只读事务中执行并最终回滚。
            autocommit=False,
        )
        # start_transaction(readonly=true) 在数据库会话层禁止写操作。
        connection.start_transaction(readonly=True)
        # dictionary=true 让每一行结果使用“字段名到值”的字典表示。
        cursor = connection.cursor(dictionary=True)
        # SELECT DATABASE() 只读取当前库名，不枚举或访问其他数据库。
        cursor.execute("SELECT DATABASE() AS current_database")
        # current_database 保存数据库服务器确认的当前逻辑库。
        current_database = cursor.fetchone()["current_database"]
        # 实际库必须与配置精确一致，防止连接参数落到默认库或其他库。
        if current_database != os.environ["DB_NAME"]:
            # ValueError 会进入 finally，连接随即关闭且事务回滚。
            raise ValueError(
                f"数据库范围校验失败：期望 {os.environ['DB_NAME']}，实际 {current_database}"
            )
        # execute 把固定 SQL 与独立参数交给驱动绑定，不进行字符串拼接。
        cursor.execute(sql, parameters)
        # rows 读取当前 SQL 返回的全部结果；SQL 模板本身已经包含 LIMIT。
        rows = cursor.fetchall()
        # columns 从游标描述中提取最终结果字段名。
        columns = [column[0] for column in cursor.description or []]
        # rollback 结束只读事务，不留下任何数据库状态变化。
        connection.rollback()
        # 返回不包含连接信息和密码的查询结果。
        return {
            # status 明确表示数据库确实执行成功。
            "status": "SUCCESS",
            # database 只记录逻辑库名，便于确认查询目标。
            "database": os.environ["DB_NAME"],
            # row_count 是实际返回的行数。
            "row_count": len(rows),
            # columns 按 SQL 返回顺序列出字段。
            "columns": columns,
            # rows 保存真实查询数据，日期和 Decimal 在打印时由 default=str 转换。
            "rows": rows,
        }
    # finally 无论中间是否出现异常都会执行。
    finally:
        # 已创建游标时先关闭游标。
        if cursor is not None:
            # close 释放服务端结果集和客户端游标资源。
            cursor.close()
        # 已创建连接且仍保持连接时关闭连接。
        if connection is not None and connection.is_connected():
            # close 释放数据库网络连接。
            connection.close()


# print_human_result 把完整结果整理成适合开发调试的简洁分段文本。
def print_human_result(result: dict[str, Any], show_sql: bool) -> None:
    # query 保存模型最终输出的本体查询。
    query = result["ontology_query"]
    # trace 保存实体、关系、指标和 Mapping 的展开过程。
    trace = result["ontology_trace"]
    # 打印第一阶段标题和用户原始问题。
    print("\n[1] 自然语言")
    # natural_language 是本次处理的原始输入。
    print(trace["natural_language"])
    # 打印第二阶段标题。
    print("\n[2] 本体命中")
    # matched_intent 展示模型选择的稳定业务意图。
    print(f"意图：{trace['matched_intent']['id']}")
    # 逐个展示从自然语言中提取出的实体。
    for entity in trace["entity_hits"]:
        # 每个实体一行展示原文、类型、标识和值。
        print(
            f"实体：{entity['mention']} -> {entity['entity_type']}"
            f"({entity['identifier_kind']}={entity['identifier_value']})"
        )
    # threshold 为 null 时显示“不筛选”，否则按百分比显示。
    threshold_text = "不筛选" if query["threshold"] is None else f"{query['threshold']:.2%}"
    # 打印阈值和最大返回行数。
    print(f"条件：严重度阈值={threshold_text}，最多返回={query['limit']}行")
    # 打印第三阶段标题。
    print("\n[3] 关系连接")
    # 逐条显示本体关系，避免用户阅读 JSON 对象数组。
    for relation in trace["relation_connections"]:
        # 使用箭头直观显示起点实体、关系和终点实体。
        print(f"{relation['from']} --{relation['relation']}--> {relation['to']}")
    # 没有关系的简单查询明确显示“无跨实体关系”。
    if not trace["relation_connections"]:
        # 该提示主要用于单计划基本信息查询。
        print("无跨实体关系")
    # metrics 使用逗号连接成一行，减少纵向输出长度。
    print(f"指标：{', '.join(trace['metrics']) if trace['metrics'] else '无'}")
    # 打印第四阶段标题和 Mapping 选择。
    print("\n[4] SQL 映射")
    # mapping 保存当前数据库映射版本和模板 ID。
    sql_mapping = trace["sql_mapping"]
    # 一行展示本体意图最终落到了哪个物理模板。
    print(f"{sql_mapping['mapping_version']} / {sql_mapping['template_id']}")
    # 打印第五阶段标题。
    print("\n[5] SQL")
    # 显式指定 --show-sql 时才展开完整 SQL。
    if show_sql:
        # SQL 原样打印，因此换行和缩进保持可读，不再显示 JSON 转义符。
        print(result["sql"].rstrip())
    # 默认只显示模板 ID 和查看完整 SQL 的方法。
    else:
        # 简洁提示避免数百行 SQL 淹没实体和关系过程。
        print(f"已生成模板 {sql_mapping['template_id']}；使用 --show-sql 查看完整 SQL。")
    # 打印最后的参数标题。
    print("\n[6] 参数")
    # 参数仍以紧凑 JSON 显示，方便复制给数据库驱动。
    print(json.dumps(result["parameters"], ensure_ascii=False))
    # execution 只在 --execute 成功完成后存在。
    if "execution" in result:
        # 打印真实数据库查询结果标题。
        print("\n[7] 查询结果")
        # execution 保存数据库、行数、字段和数据行。
        execution = result["execution"]
        # 一行显示执行状态、数据库和返回行数。
        print(
            f"状态={execution['status']}，数据库={execution['database']}，"
            f"返回={execution['row_count']}行"
        )
        # 逐行输出紧凑 JSON，避免宽表格在终端中错位。
        for row in execution["rows"]:
            # default=str 负责转换 Decimal 和 datetime 等数据库类型。
            print(json.dumps(row, ensure_ascii=False, default=str))


# main 是命令行程序的唯一入口。
def main() -> None:
    # 先读取项目本地 .env，随后命令行解析器才能获得默认模型名称。
    load_local_env(ENV_PATH)
    # parser 负责解析用户传入的自然语言和可选参数。
    parser = build_argument_parser()
    # args 保存已经解析的命令行参数。
    args = parser.parse_args()
    # try 把预期异常转换为简洁的命令行错误，而不是打印长堆栈。
    try:
        # result 是本程序边界内的最终产物：本体查询、SQL 和参数。
        result = run(args)
    # Exception 在 MVP 阶段统一捕获配置、模型和校验错误。
    except Exception as error:
        # print 将错误写入 stderr，便于脚本调用方区分正常输出。
        print(f"错误：{error}", file=sys.stderr)
        # SystemExit(1) 使用非零状态码表示生成失败。
        raise SystemExit(1) from error
    # --json 用于需要机器读取完整结果的场景。
    if args.json:
        # ensure_ascii=false 让中文在完整 JSON 中保持可读。
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    # 默认模式使用分段文本，避免 SQL 被转义后挤在一个 JSON 字符串中。
    else:
        # print_human_result 依次展示自然语言、本体命中、关系、Mapping 和 SQL 摘要。
        print_human_result(result, args.show_sql)


# 只有直接执行本文件时才启动命令行程序，被测试代码导入时不会自动运行。
if __name__ == "__main__":
    # 调用 main 开始自然语言到 SQL 的完整链路。
    main()
