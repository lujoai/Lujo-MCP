"""PG kb_entries → SQLite「笔记本」一次性迁移 CLI（Step 3 WP2；WP5 随迁移入口同批删除）。

零数据库代码边界（Architecture Frozen 第 3 条 + Step 3 设计文档 §3.2，作者已撤回
「脚本层豁免」）：本脚本只做参数解析、调用 factory.migrate_knowledge_entries()、
打印 report、返回退出码。不导入任何数据库驱动，不执行任何 SQL，不感知任何表结构，
不出现任何连接串；PG 连接参数完全来自既有 Settings（环境变量 / .env），本脚本不新增
任何数据库配置面。

用法：
    python scripts/migrate_pg_kb_to_sqlite.py --target-path <SQLite 文件路径> --dry-run
    python scripts/migrate_pg_kb_to_sqlite.py --target-path <SQLite 文件路径>

可从项目根目录直接执行，无需手动设置 PYTHONPATH（脚本自行引导项目根）。

仅迁移 kb_entries；traces / errors / sessions / specs 无迁移路径（回到 memory-only）。

退出码：0 = report status 为 ok（含空表）；1 = status 为 failed（行级失败）或迁移抛出
异常（异常不捕获、直接向上传播，由解释器以非零码退出并打印堆栈）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 项目根引导：直接执行 `python scripts\migrate_pg_kb_to_sqlite.py` 时，Python 只把
# 脚本所在目录（scripts/）放进 sys.path，`import app` 会 ModuleNotFoundError。
# 这里按脚本文件自身位置解析项目根（scripts/ 的父目录）并插入 sys.path 前部，
# 与当前工作目录和 PYTHONPATH 均无关。仅注入本仓库路径，不新增任何数据库访问面。
_PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="migrate_pg_kb_to_sqlite",
        description=(
            "一次性迁移：PostgreSQL kb_entries → 本地 SQLite 笔记本。"
            "仅迁移知识库经验，不迁移 traces / errors / sessions / specs。"
        ),
    )
    parser.add_argument(
        "--source-backend",
        default="postgresql",
        choices=["postgresql"],
        help="迁移源后端（迁移专用白名单，与运行时 STORAGE_BACKEND 无关）",
    )
    parser.add_argument(
        "--target-path",
        required=True,
        help="目标 SQLite 文件路径（KB「笔记本」数据文件）",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=1_000_000,
        help="最多迁移条数（默认全量导出）",
    )
    parser.add_argument(
        "--on-conflict",
        default="skip",
        choices=["skip", "upsert"],
        help="fingerprint 冲突策略：skip=保留目标既有经验（默认）；upsert=以源数据覆盖",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只统计将要发生的迁移，不写目标文件、不建备份",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="首条失败立即停止（默认逐行隔离错误并继续，全部计入 report）",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="放弃备份；目标文件已存在时迁移入口将拒绝执行（保护既有目标库）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # 局部导入：factory 是本仓库唯一数据库访问入口（Architecture Frozen 第 3 条）。
    from app.runtime.core.storage.factory import migrate_knowledge_entries

    report = migrate_knowledge_entries(
        source_backend=args.source_backend,
        target_path=args.target_path,
        limit=args.limit,
        on_conflict=args.on_conflict,
        dry_run=args.dry_run,
        fail_fast=args.fail_fast,
        backup=not args.no_backup,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("status") == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
