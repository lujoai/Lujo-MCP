"""
网络请求记录采集器 —— 解析外部上报的原始 dict，截断 body，规范化字段。

职责：解析 / 截断 / 校验。脱敏在 trace_repo 存储边界统一执行，本模块不重复脱敏。
按 proj1 架构重写（非复制 proj2）：dict in / dict out，与 trace_repo 保持一致。
"""
import time
import logging

logger = logging.getLogger("lujo-mcp.collectors.network")

# W15 / P3-SDK-3 更正：此前这两条注释自称"与浏览器 SDK 截断阈值一致"，实测为假 ——
# 浏览器 SDK 只在 fetch/XHR 钩子里把 response_body 截到 2000，request_body 与 url
# 当时完全不设限（_serializeRequestBody 对字符串原样返回）。这里的值是服务端**兜底**
# 上限，不是任何 SDK 阈值的镜像。现口径：两个 SDK 都在客户端把 body 截到 10176、
# url 截到 2000，并追加"（客户端已截断）"标记；SDK 上限**严格小于**这里的值，好让
# 本模块的截断只在遇到其他上报方（curl / 自建集成 / 旧版 SDK）时才生效，且不会把
# SDK 留下的标记二次截掉。
_MAX_BODY_CHARS = 10 * 1024  # 10KB 兜底
# FIX(v0.7.1-b3-7): url 截断上限（2048 字符）——超长 url 此前原样入库/
# 进 context/发 LLM，纯浪费。同为兜底值，SDK 侧上限是 2000。
_MAX_URL_CHARS = 2048


def _truncate_body(text):
    """超长 body 截断，None/空原样返回。

    已知边界（W15 / P3-SDK-3）：只按**字符串**长度收敛。对象形态的 body（手动
    `reportNetworkError({request_body: {...}})` 不会先 JSON 化）在这里走
    `len(dict)` = 键数，因此不受上限保护；真要按字节收敛得先统一成字符串，那会
    改变入库与 MCP 工具返回的形状（契约变更），不在本次 SDK 清理批范围内。
    """
    if not text:
        return text
    if len(text) > _MAX_BODY_CHARS:
        return text[:_MAX_BODY_CHARS] + "\n...（已截断）"
    return text


def _truncate_url(url):
    """超长 url 截断，None/非字符串原样返回。"""
    if not isinstance(url, str):
        return url
    if len(url) > _MAX_URL_CHARS:
        return url[:_MAX_URL_CHARS] + "...（已截断）"
    return url


def parse_network_record(raw: dict) -> dict:
    """把原始 dict 规范化为 network 记录。非法输入抛 ValueError。"""
    if not isinstance(raw, dict):
        raise ValueError("network record 必须是对象")

    status_code = raw.get("status_code")
    duration_ms = raw.get("duration_ms")
    # FIX(v0.7.1-b3-7): timestamp 类型归一——非数值（字符串/None/bool）此前
    # 原样透传，下游排序/区间筛选/JSON 契约全部置信它；现仅 int/float 生效，
    # 其余回退当前时间（与旧 `or time.time()` 对 falsy 的兜底语义一致，但
    # 不再让字符串时间戳混入数值字段）。
    raw_ts = raw.get("timestamp")
    ts = raw_ts if isinstance(raw_ts, (int, float)) and not isinstance(raw_ts, bool) else time.time()

    return {
        "record_id": raw.get("record_id"),
        "timestamp": ts,
        "direction": raw.get("direction") or "outbound",
        "method": (raw.get("method") or "GET").upper(),
        "url": _truncate_url(raw.get("url")),
        "status_code": int(status_code) if status_code is not None else None,
        "request_body": _truncate_body(raw.get("request_body")),
        "response_body": _truncate_body(raw.get("response_body")),
        "duration_ms": float(duration_ms) if duration_ms is not None else None,
    }


def parse_network_records(raw_list) -> list[dict]:
    """批量解析，单条异常跳过不阻断整体。"""
    records: list[dict] = []
    for raw in raw_list or []:
        try:
            records.append(parse_network_record(raw))
        except Exception:
            logger.warning("跳过格式异常的 network record: %r", raw)
            continue
    return records
