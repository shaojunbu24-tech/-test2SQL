"""只读连接 mom-test，完成实体落地校验和参数化 SQL 执行。"""

import os
from typing import Any

from .config import require_environment


class ReadOnlyMySQLExecutor:
    """在数据库会话和程序逻辑两层限制真实查询只能读取 mom-test。"""

    def __init__(self, expected_database: str) -> None:
        if expected_database != "mom-test":
            raise ValueError("MVP 数据库执行器只允许连接 mom-test。")
        self.expected_database = expected_database

    def execute(self, sql: str, parameters: dict[str, Any]) -> dict[str, Any]:
        """解析计划实体并在同一个只读事务中执行动态编译的查询。"""

        statement = self._strip_leading_comments(sql)
        if not statement.upper().startswith(("SELECT", "WITH")):
            raise ValueError("真实执行只允许 SELECT 或 WITH 查询。")

        environment = require_environment(
            ["DB_HOST", "DB_USER", "DB_PASSWORD", "DB_NAME"]
        )
        if environment["DB_NAME"] != self.expected_database:
            raise ValueError(
                f"数据库范围不匹配：只允许 {self.expected_database}，配置为 {environment['DB_NAME']}。"
            )

        try:
            import mysql.connector
        except ImportError as error:
            raise RuntimeError("缺少 mysql-connector-python，请安装 requirements.txt。") from error

        connection = None
        cursor = None
        try:
            connection = mysql.connector.connect(
                host=environment["DB_HOST"],
                port=int(os.getenv("DB_PORT", "3306")),
                user=environment["DB_USER"],
                password=environment["DB_PASSWORD"],
                database=environment["DB_NAME"],
                connection_timeout=10,
                autocommit=False,
            )
            connection.start_transaction(readonly=True)
            cursor = connection.cursor(dictionary=True)
            cursor.execute("SELECT DATABASE() AS current_database")
            current_database = cursor.fetchone()["current_database"]
            if current_database != self.expected_database:
                raise ValueError(
                    f"数据库范围校验失败：期望 {self.expected_database}，实际 {current_database}。"
                )

            plan_id = parameters.get("plan_id")
            grounded_entities = []
            if plan_id is not None:
                cursor.execute(
                    "SELECT id, code, name FROM `mom-test`.`produce_plan` "
                    "WHERE deleted = 0 AND id = %(plan_id)s LIMIT 1",
                    {"plan_id": plan_id},
                )
                plan = cursor.fetchone()
                if plan is None:
                    raise ValueError(f"mom-test 中不存在有效生产计划 ID {plan_id}。")
                grounded_entities.append(
                    {
                        "ontologyClass": "ProductionPlan",
                        "entityRef": f"mom:production-plan:{plan['id']}",
                        "sourceId": plan["id"],
                        "sourceCode": plan["code"],
                        "label": plan["name"],
                        "resolution": "SOURCE_ID_EXACT",
                    }
                )
            else:
                grounded_entities.append(
                    {
                        "ontologyClass": "ProductionPlan",
                        "entityRef": "mom:production-plan:*",
                        "resolution": "ACTIVE_TYPE_SCAN",
                    }
                )

            cursor.execute(sql, parameters)
            rows = cursor.fetchall()
            columns = [column[0] for column in cursor.description or []]
            connection.rollback()
            return {
                "status": "SUCCESS",
                "database": current_database,
                "grounded_entities": grounded_entities,
                "row_count": len(rows),
                "columns": columns,
                "rows": rows,
            }
        finally:
            if cursor is not None:
                cursor.close()
            if connection is not None and connection.is_connected():
                connection.close()

    @staticmethod
    def _strip_leading_comments(sql: str) -> str:
        """去掉开头单行注释，以便在连接前检查真正的 SQL 类型。"""

        statement = sql.lstrip()
        while statement.startswith("--"):
            _comment, separator, statement = statement.partition("\n")
            if not separator:
                return ""
            statement = statement.lstrip()
        return statement
