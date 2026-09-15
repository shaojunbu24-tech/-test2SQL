"""把生产计划的名称、编号或ID解析到真实数据库记录。"""


class EntityResolver:
    """所有候选检索均经过现有 mom-test 只读执行器。"""

    def __init__(self, registry, executor):
        self.registry, self.executor = registry, executor

    def resolve(self, selector):
        """只允许已登记的定位属性；模糊匹配把通配符当作普通字符处理。"""
        mapping = self.registry.mapping["entities"]["ProductionPlan"]
        prop = selector["property"]
        if prop not in {"sourceId", "planNo", "name"}:
            raise ValueError("不支持的生产计划定位属性。")
        column = mapping["properties"][prop]
        operator = "="
        value = selector["value"]
        if selector["operator"] == "CONTAINS":
            operator = "LIKE"
            value = "%" + str(value).replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"
        sql = (f"SELECT id, code, name FROM `mom-test`.`{mapping['table']}` "
               f"WHERE deleted = 0 AND `{column}` {operator} %(selector_value)s"
               + (" ESCAPE '!'" if operator == "LIKE" else "") + " LIMIT 21")
        rows = self.executor.execute(sql, {"selector_value": value})["rows"]
        status = "RESOLVED" if len(rows) == 1 else "NOT_FOUND" if not rows else "AMBIGUOUS"
        result = {"status": status, "selector": selector, "physical_property": f"{mapping['table']}.{column}",
                  "sql": sql, "parameters": {"selector_value": value}, "candidates": rows[:20],
                  "candidates_truncated": len(rows) > 20}
        if status == "RESOLVED":
            result.update(sourceId=rows[0]["id"], entityRef=f"mom:production-plan:{rows[0]['id']}")
        return result
