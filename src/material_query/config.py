"""集中管理项目路径、本地环境变量和结构化配置加载。"""

import json
import os
from pathlib import Path
from typing import Any

import yaml


# PROJECT_DIR 始终指向 mvp-text-to-sql，不依赖命令从哪里启动。
PROJECT_DIR = Path(__file__).resolve().parents[2]
# ONTOLOGY_DIR 指向只读使用的 ontology-v1；本次重构不会修改它。
ONTOLOGY_DIR = PROJECT_DIR.parent / "ontology-v1"
# CONFIG_DIR 保存 MVP 自己的 Query IR 契约和物理映射。
CONFIG_DIR = PROJECT_DIR / "config"
# EXAMPLES_DIR 保存可重复运行的离线 Query IR 示例。
EXAMPLES_DIR = PROJECT_DIR / "examples"
# ENV_PATH 保存模型和测试数据库连接信息，并已经被 Git 忽略。
ENV_PATH = PROJECT_DIR / ".env"


def load_local_env(path: Path = ENV_PATH) -> None:
    """读取简单的 KEY=VALUE 文件，并保留终端中显式设置的值。"""

    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f".env 中存在无效配置行：{raw_line}")
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_yaml(path: Path) -> dict[str, Any]:
    """安全读取最外层必须为对象的 YAML 文件。"""

    with path.open("r", encoding="utf-8") as file:
        value = yaml.safe_load(file)
    if not isinstance(value, dict):
        raise ValueError(f"YAML 文件最外层必须是对象：{path}")
    return value


def load_json(path: Path) -> dict[str, Any]:
    """读取最外层必须为对象的 JSON 文件。"""

    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 文件最外层必须是对象：{path}")
    return value


def require_environment(names: list[str]) -> dict[str, str]:
    """读取必需环境变量，只在错误中报告变量名而不报告敏感值。"""

    missing = [name for name in names if not os.getenv(name)]
    if missing:
        raise ValueError(f"缺少环境配置：{', '.join(missing)}")
    return {name: os.environ[name] for name in names}

