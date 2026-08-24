"""PG 连接异常脱敏 —— N4-FU-3。

把 psycopg2 / asyncpg 的原始连接异常归类为「脱敏摘要」，摘要文本
**绝不含** host / port / user / dbname / password 等 DSN 细节。

设计要点：
- 始终开启，不依赖 `redaction_enabled` 开关（安全关键路径不可被关闭）。
- 只基于异常的分类返回固定安全文本，**不拼接**原始 `str(exc)`。
- 如需完整细节用于排障，请在调用方用 logger 单独记录原始异常。
"""


def sanitize_pg_error(exc: Exception) -> str:
    """把 PG 连接异常归类为脱敏摘要。

    返回固定语气的安全消息；原始异常细节（含 DSN 参数）不进入返回值。
    """
    text = str(exc)
    low = text.lower()

    if "password" in low or "authentication" in low or "auth failed" in low:
        return "PostgreSQL 连接失败：凭据错误（invalid credentials）"
    if "does not exist" in low:
        return "PostgreSQL 连接失败：目标数据库不存在"
    if "timeout" in low or "timed out" in low:
        return "PostgreSQL 连接失败：连接超时"
    if (
        "refused" in low
        or "could not connect" in low
        or "unreachable" in low
        or "failed to connect" in low
        or "failed to make a connection" in low
        or "could not translate host name" in low
        or "could not resolve" in low
    ):
        return "PostgreSQL 连接失败：无法连接数据库（网络/端口不可达）"
    return "PostgreSQL 连接失败：未知错误（详见服务器日志）"
