"""MCP 工具：ingest_specs —— OpenAPI 一键生成断言规范并入库。

规范自动生成的最后一环（PRD §14：静默失败不应依赖手写规范）：此前
parse_openapi_to_specs 仅为内部函数；本工具把它暴露给宿主 AI——
拿到用户项目的 openapi.json/swagger 文档即可一键生成全套 API 断言规范
并存入 spec_store，此后 verify 对这些接口自动做「返回 200 但数据不对」
的静默失败校验，无需用户手写任何 expect JSON。
"""
import logging

from app.runtime.verifier import spec_store
from app.runtime.verifier.spec_generator import parse_openapi_to_specs

logger = logging.getLogger("lujo-mcp.tools.spec_ingest")

INGEST_SPECS_DEF = {
    "name": "ingest_specs",
    "description": (
        "把 OpenAPI 3.0 / Swagger 2.0 文档一键转成 API 断言规范并写入规范存储，"
        "激活静默失败自动校验——之后这些接口的响应会被自动比对「返回 200 但"
        "数据不对」类问题（配合 verify 工具，无需手写任何 expect 规则）。"
        "当用户提供 openapi.json / swagger 文档内容，或要求「给这些接口加自动"
        "校验/断言」时调用本工具；不需要 request_id。"
        "传 openapi（解析后的 JSON 对象）；store=false 时仅生成草稿不入库。"
        "同一 target 重复入库自动去重。"
        "纯代码问题或与接口规范无关的任务不要调用。"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "openapi": {
                "type": "object",
                "description": "OpenAPI 3.0 / Swagger 2.0 解析后的 JSON 对象",
            },
            "store": {
                "type": "boolean",
                "description": "是否写入规范存储（默认 true）；false 仅返回草稿预览",
                "default": True,
            },
        },
        "required": ["openapi"],
    },
}


def ingest_specs_handler(arguments: dict) -> dict:
    """ingest_specs 工具处理函数。"""
    arguments = arguments or {}
    openapi_data = arguments.get("openapi")
    if not isinstance(openapi_data, dict):
        return {
            "error": "openapi 必须为 OpenAPI/Swagger JSON 对象",
            "count": 0,
            "stored": 0,
            "skipped": 0,
            "spec_ids": [],
        }

    store = arguments.get("store", True)
    specs = parse_openapi_to_specs(openapi_data)

    spec_ids: list[str] = []
    skipped = 0
    if store:
        # 同 kind+target 去重：AI 重复调用不产生重复规范（防刷屏）
        existing = {
            (s.get("kind"), s.get("target")) for s in spec_store.list_specs()
        }
        for spec in specs:
            key = (spec.get("kind"), spec.get("target"))
            if key in existing:
                skipped += 1
                continue
            spec_ids.append(spec_store.create(spec))
            existing.add(key)

    return {
        "count": len(specs),
        "stored": len(spec_ids),
        "skipped": skipped,
        "spec_ids": spec_ids,
    }


def invoke(body) -> dict:
    return ingest_specs_handler(getattr(body, "arguments", {}) or {})
