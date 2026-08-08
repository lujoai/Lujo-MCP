"""P0-5 回归：migrations/ 迁移 SQL 与代码 DDL（ddl.py）一致性断言。

历史背景：迁移文件与 pg_store.py / async_pg_store.py 的 DDL 双源分叉，
列不一致导致 PG 后端 errors/specs 静默失效。P0-5 抽取 ddl.py 为唯一来源
后，本测试防止两处再次漂移。
"""
import re
from pathlib import Path

from app.runtime.core.storage.ddl import DDL_ERRORS, DDL_SPECS

_MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"


def _extract_columns(ddl: str) -> set[str]:
    """提取 CREATE TABLE 块中的列名（顶层缩进 + name TYPE 格式）。"""
    match = re.search(r"CREATE TABLE IF NOT EXISTS \w+ \((.*?)\);", ddl, re.S)
    assert match, f"无法解析 CREATE TABLE 块: {ddl[:80]}"
    body = match.group(1)
    cols: set[str] = set()
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.upper().startswith(
            ("PRIMARY KEY", "UNIQUE", "INDEX", "CONSTRAINT", "FOREIGN KEY", "CHECK")
        ):
            continue
        m = re.match(r"(\w+)\s+\S+", line)
        if m:
            cols.add(m.group(1).lower())
    return cols


def _read_migration(name: str) -> str:
    return (_MIGRATIONS / name).read_text(encoding="utf-8")


def _migration_create_table(sql_text: str) -> str:
    match = re.search(
        r"CREATE TABLE IF NOT EXISTS \w+ \((.*?)\);", sql_text, re.S
    )
    assert match, "迁移文件缺少 CREATE TABLE IF NOT EXISTS 块"
    return match.group(0)


def test_errors_migration_matches_code_ddl():
    sql = _read_migration("20260711_create_errors_table.sql")
    code_cols = _extract_columns(DDL_ERRORS)
    sql_cols = _extract_columns(_migration_create_table(sql))
    assert sql_cols == code_cols, (
        "errors 迁移与 ddl.py DDL_ERRORS 列不一致\n"
        f"code={sorted(code_cols)}\nsql={sorted(sql_cols)}"
    )


def test_specs_migration_matches_code_ddl():
    sql = _read_migration("20260711_create_specs_table.sql")
    code_cols = _extract_columns(DDL_SPECS)
    sql_cols = _extract_columns(_migration_create_table(sql))
    assert sql_cols == code_cols, (
        "specs 迁移与 ddl.py DDL_SPECS 列不一致\n"
        f"code={sorted(code_cols)}\nsql={sorted(sql_cols)}"
    )
