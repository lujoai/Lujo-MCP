#!/bin/bash
set -e

echo "Initializing database..."

if [ -z "$PG_HOST" ]; then
    PG_HOST="localhost"
fi
if [ -z "$PG_PORT" ]; then
    PG_PORT="5432"
fi
if [ -z "$PG_USER" ]; then
    PG_USER="postgres"
fi
if [ -z "$PG_DATABASE" ]; then
    PG_DATABASE="lujo_mcp"
fi

# 迁移文件说明（pg_store.py 通过 DDL 常量自动建表，此处 SQL 仅供手动初始化/参考）：
#   活跃表（pg_store.py 有完整 CRUD）：
#     20260710_create_sessions_table.sql
#     20260710_create_traces_table.sql
#     20260711_create_errors_table.sql
#     20260711_create_specs_table.sql
#   已删除（M11 清理：废弃表，pg_store.py 无任何 CRUD，数据经 traces 表 step 字段存储）：
#     20260712_create_network_records_table.sql
#     20260712_create_ui_events_table.sql
for file in $(ls migrations/*.sql 2>/dev/null | sort); do
    echo "Executing $file..."
    PGPASSWORD="$PG_PASSWORD" psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$PG_DATABASE" -f "$file"
done

echo ""
echo "Database initialization completed."