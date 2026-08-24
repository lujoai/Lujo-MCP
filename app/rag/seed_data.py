"""知识库种子知识 —— 30 条高频异常模式（v0.4.0 M2）。

覆盖六大类高频异常，为三级 fallback（L1 精确 / L1.5 归一化 / L2 类型级 Jaccard）
提供初始可匹配样本。每条以 DebugCase 为标准结构，经 to_kb_entry 导出为标准 KB entry。

指纹约定：fingerprint 使用稳定可读键（seed:<type>:<语义>），
便于在精确指纹阶段（L1）直接命中；同类型同模式不同变量值场景由
L1.5 归一化指纹 / L2 类型级 Jaccard 兜底。
"""

from __future__ import annotations

from app.rag.debug_case import DebugCase

# 构造辅助函数：message 中的数字/变量值会被归一化，保证同模式不同值可被 L1.5 命中
def _case(
    exception_type: str,
    message: str,
    fingerprint: str,
    root_cause: str,
    fix_suggestion: str,
    tags: list[str],
    case_confidence: float = 0.8,
) -> DebugCase:
    return DebugCase(
        exception_type=exception_type,
        message=message,
        fingerprint=fingerprint,
        root_cause=root_cause,
        fix_suggestion=fix_suggestion,
        tags=tags,
        case_confidence=case_confidence,
        verify_count=0,
    )


# ── ValueError（5 条）──────────────────────────────────────────────
_VALUE_ERROR_CASES = [
    _case(
        "ValueError",
        "invalid literal for int() with base 10: 'abc'",
        "seed:valueerror:int_literal",
        "字符串无法按 int() 解析，多为用户输入/API 参数未做类型校验",
        "对输入做 isdigit() 或 try/except 解析，失败时给出明确报错",
        ["valueerror", "type-conversion", "input-validation"],
    ),
    _case(
        "ValueError",
        "max() arg is an empty sequence",
        "seed:valueerror:max_empty_seq",
        "对空序列调用 max()/min()，调用方未处理空列表",
        "调用前判断序列非空，或给 max()/min() 提供 default 参数",
        ["valueerror", "empty-sequence", "builtin"],
    ),
    _case(
        "ValueError",
        "could not convert string to float: '1,234.56'",
        "seed:valueerror:float_parse",
        "字符串含千分位逗号/本地化格式，float() 无法直接解析",
        "先去除逗号等本地化分隔符，或用 locale 解析",
        ["valueerror", "type-conversion", "locale"],
    ),
    _case(
        "ValueError",
        "invalid mode 'x' for open(): expected 'r', 'w', 'a', 'x' or 'r+'",
        "seed:valueerror:open_mode",
        "open() 传入未支持的文件模式标志",
        "校验模式参数，仅传 r/w/a/x/r+ 及组合",
        ["valueerror", "file-io", "bad-argument"],
    ),
    _case(
        "ValueError",
        "malformed node or string: invalid literal",
        "seed:valueerror:ast_literal",
        "ast.literal_eval() 收到非安全字面量的字符串",
        "先校验输入格式，或改用安全的 JSON 解析",
        ["valueerror", "ast", "unsafe-eval"],
    ),
]

# ── TypeError（5 条）───────────────────────────────────────────────
_TYPE_ERROR_CASES = [
    _case(
        "TypeError",
        "unsupported operand type(s) for +: 'int' and 'str'",
        "seed:typeerror:add_int_str",
        "对 int 与 str 执行 + 运算，类型不匹配",
        "统一操作数类型，或先做类型转换",
        ["typeerror", "type-mismatch", "operand"],
    ),
    _case(
        "TypeError",
        "object of type 'NoneType' has no len()",
        "seed:typeerror:len_none",
        "对 None 调用 len()，函数返回了 None 而调用方未判空",
        "在调用 len() 前判空，或修复函数返回 None 的路径",
        ["typeerror", "none", "len"],
    ),
    _case(
        "TypeError",
        "'NoneType' object is not callable",
        "seed:typeerror:call_none",
        "把 None 当作函数调用，多为变量遮蔽/赋值覆盖了函数",
        "检查变量名是否遮蔽内置函数，或初始化时赋予可调用对象",
        ["typeerror", "none", "callable"],
    ),
    _case(
        "TypeError",
        "argument of type 'int' is not iterable",
        "seed:typeerror:iter_int",
        "对 int 执行迭代/解包，误传了标量",
        "校验参数为可迭代容器，或修正调用方传参",
        ["typeerror", "iterable", "unpacking"],
    ),
    _case(
        "TypeError",
        "takes 1 positional argument but 2 were given",
        "seed:typeerror:arg_count",
        "函数签名参数个数与调用方传参个数不匹配",
        "核对函数签名与调用点，补齐或精简参数",
        ["typeerror", "argument-count", "signature"],
    ),
]

# ── KeyError（5 条）────────────────────────────────────────────────
_KEY_ERROR_CASES = [
    _case(
        "KeyError",
        "'user_id'",
        "seed:keyerror:missing_user_id",
        "dict 中不存在 'user_id' 键，多为数据源结构变化或字段缺失",
        "改用 dict.get()，或先校验键存在",
        ["keyerror", "dict", "missing-key"],
    ),
    _case(
        "KeyError",
        "'name'",
        "seed:keyerror:missing_name",
        "对象/字典中缺少 'name' 字段，API 返回结构不完整",
        "使用 .get() 并提供默认值，或做结构校验",
        ["keyerror", "dict", "missing-key"],
    ),
    _case(
        "KeyError",
        "'config'",
        "seed:keyerror:missing_config",
        "配置字典中缺少 'config' 键，配置加载失败或默认值缺失",
        "为配置加载提供默认值，或校验配置完整性",
        ["keyerror", "config", "missing-key"],
    ),
    _case(
        "KeyError",
        "'id'",
        "seed:keyerror:missing_id",
        "记录中缺少 'id' 字段，数据库/API 返回缺少主键",
        "在写入前校验主键字段，或对缺失记录做跳过处理",
        ["keyerror", "record", "missing-key"],
    ),
    _case(
        "KeyError",
        "'status'",
        "seed:keyerror:missing_status",
        "响应对象缺少 'status' 字段，多为接口契约变更",
        "使用 .get('status', 默认值) 或校验响应字段",
        ["keyerror", "response", "missing-key"],
    ),
]

# ── AttributeError（5 条）──────────────────────────────────────────
_ATTRIBUTE_ERROR_CASES = [
    _case(
        "AttributeError",
        "'NoneType' object has no attribute 'name'",
        "seed:attributeerror:none_name",
        "对 None 访问 .name 属性，前置查询/调用返回了 None",
        "访问属性前判空，或修复返回 None 的调用链",
        ["attributeerror", "none", "attribute"],
    ),
    _case(
        "AttributeError",
        "'dict' object has no attribute 'append'",
        "seed:attributeerror:dict_append",
        "把 dict 当 list 用 append，数据容器类型混淆",
        "确认容器类型，dict 用 update/索引，list 用 append",
        ["attributeerror", "container", "type-confusion"],
    ),
    _case(
        "AttributeError",
        "module 'os' has no attribute 'pathx'",
        "seed:attributeerror:module_attr",
        "访问模块不存在的属性，多为拼写错误或版本差异",
        "核对模块 API，或检查导入的模块版本",
        ["attributeerror", "module", "typo"],
    ),
    _case(
        "AttributeError",
        "'str' object has no attribute 'decode'",
        "seed:attributeerror:str_decode",
        "对 str 调用 decode()（Python3 str 已解码），需 bytes 才可 decode",
        "改为对 bytes 调用 decode，或直接用 str 处理",
        ["attributeerror", "str", "py3-port"],
    ),
    _case(
        "AttributeError",
        "'Response' object has no attribute 'json'",
        "seed:attributeerror:response_json",
        "HTTP 响应对象无 .json 方法，可能为 requests 的 Response 与 http 库混淆",
        "确认使用 requests.Response，或改用 .text 后 json.loads",
        ["attributeerror", "http", "api"],
    ),
]

# ── ConnectionError（5 条）─────────────────────────────────────────
_CONNECTION_ERROR_CASES = [
    _case(
        "ConnectionError",
        "Connection refused",
        "seed:connectionerror:refused",
        "目标端口未监听/服务未启动，连接被拒绝",
        "确认服务已启动并监听正确端口，检查网络策略",
        ["connectionerror", "network", "refused"],
    ),
    _case(
        "ConnectionError",
        "Connection timed out",
        "seed:connectionerror:timeout",
        "连接超时，目标不可达或防火墙拦截",
        "检查网络可达性、防火墙/安全组，增加超时重试",
        ["connectionerror", "network", "timeout"],
    ),
    _case(
        "ConnectionError",
        "Connection reset by peer",
        "seed:connectionerror:reset",
        "连接被对端强拆，多因对端超时/异常关闭",
        "检查对端服务稳定性，增加重连与幂等处理",
        ["connectionerror", "network", "reset"],
    ),
    _case(
        "ConnectionError",
        "Name or service not known",
        "seed:connectionerror:dns",
        "DNS 解析失败，主机名不存在或 DNS 配置错误",
        "检查主机名拼写与 DNS 配置，改用 IP 兜底",
        ["connectionerror", "network", "dns"],
    ),
    _case(
        "ConnectionError",
        "getaddrinfo failed",
        "seed:connectionerror:getaddrinfo",
        "域名解析失败，常因代理/网络环境未配置",
        "检查代理配置与网络环境，确认域名可解析",
        ["connectionerror", "network", "dns"],
    ),
]

# ── HTTP & Web / API（5 条）────────────────────────────────────────
_HTTP_WEB_CASES = [
    _case(
        "httpx.HTTPStatusError",
        "Server error '502 Bad Gateway' for url 'https://api.example.com/v1/data'",
        "seed:http:502_bad_gateway",
        "反向代理（Nginx/Gateway）无法连接到后端上游服务，上游服务崩溃或未启动",
        "检查后端容器/服务运行状态与端口，配置健康检查并引入指数退避重试",
        ["http", "502", "gateway", "network"],
    ),
    _case(
        "httpx.HTTPStatusError",
        "Client error '401 Unauthorized' for url 'https://api.example.com/v1/auth'",
        "seed:http:401_unauthorized",
        "API 请求认证失败：API Key 缺失、过期、格式错误或鉴权 Header 名称不匹配",
        "检查 Authorization Bearer Token 或 X-API-Key 配置，确认密钥有效期并自动刷新",
        ["http", "401", "auth", "security"],
    ),
    _case(
        "httpx.HTTPStatusError",
        "Client error '429 Too Many Requests' for url 'https://api.example.com/v1/chat'",
        "seed:http:429_rate_limit",
        "请求超出下游服务速率配额限制或并发上限",
        "解析 Retry-After 响应头，增加客户端限流节流与异步队列缓冲",
        ["http", "429", "rate-limit", "throttle"],
    ),
    _case(
        "ssl.SSLCertVerificationError",
        "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate",
        "seed:ssl:cert_verify_failed",
        "客户端缺失根证书信任链或系统证书未正确安装，在企业代理环境下常见",
        "更新 certifi 证书包，或在受控内网环境中配置自定义 CA 证书路径",
        ["ssl", "tls", "certificate", "security"],
    ),
    _case(
        "CORSError",
        "Access to fetch at 'https://api.example.com' from origin 'https://app.example.com' has been blocked by CORS policy",
        "seed:web:cors_blocked",
        "浏览器同源策略拦截：服务端未返回 Access-Control-Allow-Origin 响应头或缺少指定方法",
        "在服务端配置 CORS 中间件（如 CORSMiddleware），允许对应的前端 Origin 及 Header",
        ["cors", "browser", "security", "frontend"],
    ),
]

# ── Database & Async（5 条）────────────────────────────────────────
_DB_ASYNC_CASES = [
    _case(
        "asyncio.TimeoutError",
        "Task timed out after 30 seconds awaiting coroutine",
        "seed:asyncio:task_timeout",
        "异步任务或协程执行超时，可能因数据库死锁、外部 RPC 挂起或慢查询阻塞",
        "使用 asyncio.wait_for 设置合理超时，并在慢查询/下游调用中设置连接及读写超时",
        ["asyncio", "timeout", "concurrency"],
    ),
    _case(
        "asyncpg.exceptions.UniqueViolationError",
        "duplicate key value violates unique constraint 'users_pkey'",
        "seed:db:unique_violation",
        "数据库主键或唯一索引冲突，在并发写入或重复重试时产生",
        "使用 ON CONFLICT DO UPDATE (Upsert) 或在应用层先做幂等存在性判断",
        ["database", "postgres", "unique-constraint", "idempotency"],
    ),
    _case(
        "asyncpg.exceptions.TooManyConnectionsError",
        "remaining connection slots are reserved for non-replication superuser connections",
        "seed:db:too_many_connections",
        "数据库连接池耗尽，连接泄露（未及时归还连接）或突发高并发超出 max_connections",
        "使用 async with 保证连接正确释放，合理配置连接池 pool_size 并启用 PgBouncer 连接池代理",
        ["database", "postgres", "connection-pool", "resource-leak"],
    ),
    _case(
        "redis.exceptions.ConnectionError",
        "Error 111 connecting to redis:6379. Connection refused.",
        "seed:redis:conn_refused",
        "Redis 实例未启动、端口不通、密码错误或容器间网络不互通",
        "检查 Redis 服务状态、网络组配置，并配置内存/降级缓存机制",
        ["redis", "cache", "connection", "network"],
    ),
    _case(
        "asyncio.CancelledError",
        "Task was cancelled",
        "seed:asyncio:task_cancelled",
        "协程任务在执行过程中被外部显式取消（task.cancel()）或请求连接被客户端提前断开",
        "在关键清理逻辑中使用 try...finally 或 asyncio.shield() 保护不可中断的状态提交操作",
        ["asyncio", "cancelled", "async"],
    ),
]

# ── Frontend & Browser（5 条）──────────────────────────────────────
_FRONTEND_BROWSER_CASES = [
    _case(
        "TypeError",
        "Cannot read properties of undefined (reading 'map')",
        "seed:frontend:cannot_read_map",
        "前端渲染列表时，后端接口返回空或尚未完成异步加载，变量为 undefined",
        "使用可选链 (items ?? []).map(...) 或在渲染组件前添加骨架屏/Loading 状态",
        ["frontend", "javascript", "null-safety", "react-vue"],
    ),
    _case(
        "TypeError",
        "Cannot read properties of null (reading 'addEventListener')",
        "seed:frontend:null_add_event_listener",
        "DOM 未完成挂载时尝试通过 document.getElementById 获取元素并绑定事件",
        "在 DOMContentLoaded / Vue onMounted / React useEffect 钩子中执行 DOM 绑定",
        ["frontend", "dom", "lifecycle", "javascript"],
    ),
    _case(
        "ReferenceError",
        "process is not defined",
        "seed:frontend:process_not_defined",
        "在前端 Vite / Webpack 浏览器打包产物中使用了 Node.js 专属的 process.env",
        "改用 import.meta.env（Vite）或在构建工具中配置 DefinePlugin 注入环境变量",
        ["frontend", "bundler", "vite", "node-vs-browser"],
    ),
    _case(
        "DOMException",
        "Failed to execute 'setItem' on 'Storage': Setting the value exceeded the quota.",
        "seed:frontend:localstorage_quota",
        "浏览器 LocalStorage 存储达到 5MB 上限，未能写入新的暂存日志",
        "实现基于 LRU 的自动淘汰清理机制，或对大体量日志启用 IndexedDB 存储",
        ["frontend", "localstorage", "quota", "storage"],
    ),
    _case(
        "TypeError",
        "Failed to fetch",
        "seed:frontend:failed_to_fetch",
        "浏览器端 fetch 请求失败，由于断网、DNS 失败、CORS 阻断或 Mixed Content (HTTPS 调 HTTP)",
        "捕获 fetch 异常，统一在 SDK 增加离线缓存与网络状态监测重试",
        ["frontend", "fetch", "network-error", "browser"],
    ),
]

# ── 其他（5 条）────────────────────────────────────────────────────
_OTHER_CASES = [
    _case(
        "IndexError",
        "list index out of range",
        "seed:indexerror:list_out_of_range",
        "访问列表越界索引，未校验列表长度",
        "访问前校验 len(seq)，或使用切片/安全访问",
        ["indexerror", "list", "bounds"],
    ),
    _case(
        "json.JSONDecodeError",
        "Expecting value: line 1 column 1 (char 0)",
        "seed:jsondecode:empty_body",
        "对空/非 JSON 字符串执行 json.loads 失败",
        "先校验响应非空，捕获解析异常并提示",
        ["jsondecode", "serialization", "empty-body"],
    ),
    _case(
        "ZeroDivisionError",
        "division by zero",
        "seed:zerodivision:div_by_zero",
        "除数为 0，未做零值校验",
        "除数前判零，或使用安全除法",
        ["zerodivision", "math", "bounds"],
    ),
    _case(
        "FileNotFoundError",
        "[Errno 2] No such file or directory: 'config.json'",
        "seed:filenotfound:config",
        "尝试读取不存在的配置文件",
        "读取前检查文件存在，或提供默认配置",
        ["filenotfound", "file-io", "missing-file"],
    ),
    _case(
        "RuntimeError",
        "Event loop is closed",
        "seed:runtimeerror:event_loop_closed",
        "在事件循环关闭后调用异步操作",
        "在循环生命周期内调度任务，避免关闭后调用",
        ["runtimeerror", "async", "event-loop"],
    ),
]

# 全部 30 条种子（按 KB entry 格式导出，供 load_knowledge_base_seeds 直接导入）
SEED_CASES: list[dict] = [
    case.to_kb_entry()
    for case in (
        _VALUE_ERROR_CASES
        + _TYPE_ERROR_CASES
        + _KEY_ERROR_CASES
        + _ATTRIBUTE_ERROR_CASES
        + _CONNECTION_ERROR_CASES
        + _HTTP_WEB_CASES
        + _DB_ASYNC_CASES
        + _FRONTEND_BROWSER_CASES
        + _OTHER_CASES
    )
]

# 各异常类型覆盖数量（供日志/评估使用）
SEED_COVERAGE: dict[str, int] = {
    "ValueError": len(_VALUE_ERROR_CASES),
    "TypeError": len(_TYPE_ERROR_CASES),
    "KeyError": len(_KEY_ERROR_CASES),
    "AttributeError": len(_ATTRIBUTE_ERROR_CASES),
    "ConnectionError": len(_CONNECTION_ERROR_CASES),
    "HTTP & Web": len(_HTTP_WEB_CASES),
    "Database & Async": len(_DB_ASYNC_CASES),
    "Frontend & Browser": len(_FRONTEND_BROWSER_CASES),
    "其他": len(_OTHER_CASES),
}


def load_seed_data() -> int:
    """加载种子知识到全局知识库，返回导入条数（幂等）。"""
    from app.rag.knowledge_base import load_knowledge_base_seeds

    return load_knowledge_base_seeds(SEED_CASES)
