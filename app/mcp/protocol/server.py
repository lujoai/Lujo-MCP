"""MCP 协议服务器 —— 遵循 MCP 2024-11-05 规范"""

import asyncio
import json
import logging
import time
import threading
from concurrent.futures import ThreadPoolExecutor

from app import __version__
from app.config import settings
from app.mcp.protocol.executor_lifecycle import SlotPool
from app.mcp.protocol.executor_lifecycle import lifecycle as _slot_lifecycle
from app.mcp.protocol.heavy_process import run_heavy_tool_blocking
from app.mcp.protocol.tool_errors import is_tool_failure_result
from app.observability import (
    record_mcp_tool_call,
    record_mcp_tool_busy,
    record_mcp_tool_wait,
)
from app.mcp.protocol.jsonrpc import (
    JSONRPCRequest,
    make_response,
    make_error,
    METHOD_NOT_FOUND,
    INTERNAL_ERROR,
    PARSE_ERROR,
    INVALID_REQUEST,
    INVALID_PARAMS,
    JSONParseError,
    InvalidRequestError,
    parse_request,
)

logger = logging.getLogger("lujo-mcp.protocol")

# MCP 协议版本
PROTOCOL_VERSION = "2024-11-05"

# 服务端支持的协议版本列表（用于版本协商，按推荐度降序）
SUPPORTED_PROTOCOL_VERSIONS = ["2024-11-05", "2024-08-27"]

# 服务端能力声明
CAPABILITIES = {
    "tools": {},  # 支持工具调用
}

SERVER_INFO = {
    "name": settings.service_name,
    "version": __version__,
}

# 工具注册表
_tool_registry: dict[str, dict] = {}

# FIX P3-12: 同步工具 handler 专用有界线程池（与 app/mcp_server.py 同方案）。
# 避免超时 handler 占用 asyncio 默认线程池并拖累其它 to_thread 任务；池有界不无限增长。
# FIX P3-12 / v0.6.2: 轻重型同步工具双池隔离执行架构
# 1. 通用/轻量同步工具专用池（如 stacktrace / get_debug_context / blame 等）
_LIGHT_TOOL_EXECUTOR = ThreadPoolExecutor(max_workers=settings.tool_executor_workers)

# 2. 重型工具（FIX: C2 —— 由线程池改为「每次调用独立子进程」）：
#    旧实现用 ThreadPoolExecutor，超时后 cancel() 无法中断已运行线程 → 僵尸线程
#    占满仅 2 个 worker 的 heavy 池、恒 TOOL_BUSY。现改为 heavy_process 子进程
#    执行 + 超时 terminate() 强杀（进程可杀），故不再需要重型线程池，仅保留
#    信号量做并发门控（限制同时运行的重活子进程数，避免无界拉起浏览器）。
#    （B20 / DESIGN C1 §5：原先模块级裸 `_light_tool_slots` / `_heavy_tool_slots`
#    Semaphore 已删除——「import 即永生」跨 loop 复用会绑定旧 loop、等待者悬挂、
#    许可泄漏或容量翻倍。槽位信号量改由下方代际属主持有。）

# 向后兼容旧版直接引用
_TOOL_EXECUTOR = _LIGHT_TOOL_EXECUTOR
_executor_lock = threading.Lock()

# DESIGN C1 §3.4/§5.1：容量代际属主 + token 状态机（**结算**与**许可归还**分离）。
# 信号量由代际属主持有并按代际替换；本模块不再持有任何裸 Semaphore
# （「槽位共享、池分立」不变，不统一 _TOOL_EXECUTOR 与 _LIGHT_TOOL_EXECUTOR）。
_light_pool = _slot_lifecycle.pool("light", settings.tool_executor_workers)
_heavy_pool = _slot_lifecycle.pool("heavy", settings.tool_heavy_executor_workers)


def _pool_for(pool_type: str) -> SlotPool:
    """按池类型取代际属主（light/heavy 分立）。"""
    return _heavy_pool if pool_type == "heavy" else _light_pool


def _get_tool_slots(pool_type: str) -> asyncio.Semaphore:
    """B20（DESIGN C1 §5）：返回 pool_type 池**当前代际**的信号量句柄。

    每次调用都从代际属主重读当前代，禁止缓存裸对象——代际重建
    （begin_close → retire → start_new_generation）后旧对象随旧代废弃，
    新调用自然拿到新代信号量（容量恢复、等待者不迁移）。
    """
    return _pool_for(pool_type).semaphore


def _get_light_tool_executor() -> ThreadPoolExecutor:
    """获取通用轻量工具专用线程池；已被 shutdown 时自愈重建同规格的有界池。

    保证在同一进程内某处触发 shutdown（如 stdio 关闭或测试回收）后，
    后续 HTTP/其它通道的工具调用仍可自愈恢复，避免被永久毒化。

    shutdown 检测只能读 ``ThreadPoolExecutor._shutdown``：关闭动作发生在本模块
    之外（``app/mcp_server.py`` 的退出清理、测试直接调 ``executor.shutdown()``），
    自持标记没有任何路径能置位，只会给出恒 False 的假信号。也不用
    ``getattr(..., False)`` 兜底——属性一旦消失会静默失去自愈能力、把池永久
    毒化且无告警，直接访问反而显式失败。该属性自 CPython 3.9 起稳定存在，
    本项目锁定 3.12（AGENTS.md §8），且单元测试已直接断言它。
    """
    global _LIGHT_TOOL_EXECUTOR, _TOOL_EXECUTOR
    with _executor_lock:
        if _LIGHT_TOOL_EXECUTOR._shutdown:
            _LIGHT_TOOL_EXECUTOR = ThreadPoolExecutor(
                max_workers=settings.tool_executor_workers
            )
            _TOOL_EXECUTOR = _LIGHT_TOOL_EXECUTOR
        return _LIGHT_TOOL_EXECUTOR


def is_heavy_tool(tool_name: str) -> bool:
    """判定工具是否属于重型长耗时工具（占用独立 Heavy 槽位池）。"""
    tool_meta = _tool_registry.get(tool_name, {})
    if tool_meta.get("heavy"):
        return True
    return tool_name in settings.heavy_tools


def _get_tool_executor_and_slots(tool_name: str) -> tuple[ThreadPoolExecutor | None, asyncio.Semaphore, str]:
    """根据工具类型选择执行池与并发信号量槽位。

    FIX: C2 —— 重型工具返回 executor=None（走独立子进程执行，见
    :func:`app.mcp.protocol.heavy_process.run_heavy_tool_blocking`），仅用信号量
    门控；轻量工具仍用专用线程池。
    """
    if is_heavy_tool(tool_name):
        return None, _get_tool_slots("heavy"), "heavy"
    return _get_light_tool_executor(), _get_tool_slots("light"), "light"


async def _acquire_slot_or_fastfail(
    slots: asyncio.Semaphore,
    busy_timeout: float,
    pool: SlotPool | None = None,
) -> bool:
    """竞态安全地获取执行槽位；超时/无槽位 fast-fail 返回 False。

    FIX: v0.6.6 超时背压竞态 —— 原 ``asyncio.wait_for(slots.acquire(), timeout)``
    在超时与获取完成同拍（典型：busy_timeout=0/极小且并发释放槽位）时，
    槽位可能已实际转移到本调用方，但调用方只看到 TimeoutError 并按
    TOOL_BUSY fast-fail 返回且永不 release → 槽位泄漏，重复 N 次后池永久
    占满、全部工具恒 TOOL_BUSY。此处超时后显式检查 acquire 任务完成态：
    已成功取得则立即归还槽位（防泄漏），未取得则由 wait_for 的取消语义
    保证 semaphore 状态机自行清理 waiter（不产生重复释放）。

    实现细节：
    - 快路径：有空位时 ``acquire`` 内部不挂起（事件循环单线程，locked()
      检查到获取之间无 await、无竞态），避免 ensure_future 包一层任务后
      timeout=0 的定时器把"有空位的快路径获取"误杀成 TOOL_BUSY；
    - ``busy_timeout <= 0``：无可用槽位时立即拒绝（Fast-Fail 文档语义）；
    - B20（DESIGN C1 §5 规则 2）：传入 ``pool`` 时，等待中的 acquire 任务
      登记进代际属主；代际关闭（``pool.cancel_waiters()``）统一取消等待者，
      按「传输关闭 / TOOL_BUSY」失败（返回 False，走既有 TOOL_BUSY 响应）。
      等待者**绝不**迁移到新代信号量（迁移 = 两代容量叠加 = 容量翻倍）。
    """
    if not slots.locked():
        # 有空位：acquire 走 Semaphore 快路径（不挂起、不进等待队列）
        await slots.acquire()
        return True

    if busy_timeout <= 0:
        # Fast-Fail：无可用槽位且不等待，立即拒绝
        return False

    task = asyncio.ensure_future(slots.acquire())
    waiter_id = pool.register_waiter(task) if pool is not None else None
    try:
        await asyncio.wait_for(task, timeout=busy_timeout)
        return True
    except asyncio.CancelledError:
        # FIX: R7-T1 —— 外层请求协程在等槽位期间被取消（如 HTTP 断连）时
        # wait_for 传播 CancelledError：若此刻内层 acquire 已完成，槽位已
        # 取得却无人归还 → 泄漏，重复发生使池容量永久缩减至恒 TOOL_BUSY
        # （与 v0.6.6 修的"超时同拍"同形）。按完成态检查后归还，并继续
        # 传播取消。
        if task.done() and not task.cancelled() and task.exception() is None:
            slots.release()
        # B20：取消来自代际关闭而非调用方自身（外层任务 cancelling() == 0
        # 说明不是 HTTP 断连等自身取消）→ 按 TOOL_BUSY 失败，不再向上传播。
        if (
            waiter_id is not None
            and pool is not None
            and asyncio.current_task().cancelling() == 0
            and pool.waiter_cancel_requested(waiter_id)
        ):
            return False
        raise
    except asyncio.TimeoutError:
        if task.done() and not task.cancelled() and task.exception() is None:
            # 完成与超时同拍：槽位已实际取得，归还防泄漏
            slots.release()
        return False
    finally:
        if waiter_id is not None:
            pool.unregister_waiter(waiter_id)


def register_tool(
    name: str,
    description: str,
    handler: callable,
    **kwargs,
):
    """注册一个 MCP 工具。inputSchema 等额外字段通过 kwargs 透传。

    v0.5: 支持 category 和 experimental 元数据（可选，向后兼容）。
    v0.6.2: 支持 heavy 元数据标记重型工具（默认通过 settings.heavy_tools 判定）。
    FIX C2: 支持可选 ``prepare_args`` 钩子——重型工具改在子进程执行后，父进程
    内存态（如 spec_store）对子进程不可见，需在派发前于父进程把入参预处理
    （如 verify_ui 把 spec_id 解析为 spec）。
    v0.7.3: 支持 ``agent_visible=False``——SDK 上报类工具（ingest_*）仍注册
    可被 tools/call 直接调用（REST/SDK 链路不受影响），但默认不出现在
    tools/list 中，减少宿主 AI 的选择噪音（HTTP/stdio 经 get_agent_visible_tools
    保持一致过滤）。
    FIX(R8): 支持可选 ``is_failure`` 谓词——全局失败契约「dict 含非空 error 键
    ⇒ 失败」对结论型工具不成立（verify_ui 的载荷本身就是验证结论，error 只是
    解释字段）。声明谓词的工具按自身语义判定，未声明的工具行为完全不变。
    """
    _tool_registry[name] = {
        "name": name,
        "description": description,
        "inputSchema": kwargs.get("inputSchema", {}),
        "handler": handler,
        "category": kwargs.get("category"),
        "experimental": kwargs.get("experimental", False),
        "heavy": kwargs.get("heavy", False),
        "prepare_args": kwargs.get("prepare_args"),
        "availability": kwargs.get("availability"),
        "agent_visible": kwargs.get("agent_visible", True),
        "is_failure": kwargs.get("is_failure"),
    }


def tool_failure_predicate(tool: dict):
    """取该工具的失败判定谓词；未声明则回落到全局默认契约。

    HTTP 与 stdio 两条传输都必须经此函数取谓词，避免各自内联默认值造成口径漂移
    （R8 修的 stdio 门控缺失就是这类漂移的结果）。
    """
    return tool.get("is_failure") or is_tool_failure_result


def _is_tool_available(tool: dict) -> bool:
    """检查可选依赖，避免 tools/list 宣布当前运行时无法执行的工具。"""
    checker = tool.get("availability")
    if checker is None:
        return True
    try:
        return bool(checker())
    except Exception:
        logger.warning("工具 %s 可用性检查失败，按不可用处理", tool.get("name"), exc_info=True)
        return False


def get_agent_visible_tools() -> list[dict]:
    """返回 tools/list 应暴露的 Agent-facing 工具（按注册顺序）。

    agent_visible=False 的 SDK 上报类工具不在其中，但仍保留在 _tool_registry
    里，tools/call 按名调用照常执行——过滤只影响「清单展示」，不影响「调用」。
    """
    return [
        t for t in _tool_registry.values()
        if t.get("agent_visible", True) and _is_tool_available(t)
    ]


def _handle_initialize(req: JSONRPCRequest) -> dict:
    """处理 initialize 握手 —— 协商协议版本

    读取客户端 params.protocolVersion：
    - 若在 SUPPORTED_PROTOCOL_VERSIONS 中则回显该版本
    - 未知/缺失版本回退到 PROTOCOL_VERSION 并记录 warning
    """
    params = req.params
    # FIX(v0.7.1-b4-1): params 非 dict（数组/字符串/null）时防御——
    # 此前 `req.params or {}` 对 list 不生效，params.get 抛 AttributeError
    # 被 dispatch 兜底成 500/-32603（初始化握手应给 -32600 语义化拒绝）。
    if not isinstance(params, dict):
        return make_error(req.id, INVALID_REQUEST, "initialize params 必须为对象")
    client_version = params.get("protocolVersion")

    if client_version and client_version in SUPPORTED_PROTOCOL_VERSIONS:
        # 客户端请求的版本在支持列表内，回显该版本
        negotiated = client_version
    else:
        # 未知/缺失版本，回退到服务端最新版本并记录 warning
        if client_version:
            logger.warning(
                "客户端请求的协议版本 %s 不在支持列表 %s 中，回退到 %s",
                client_version, SUPPORTED_PROTOCOL_VERSIONS, PROTOCOL_VERSION,
            )
        else:
            logger.warning("客户端未提供 protocolVersion，回退到 %s", PROTOCOL_VERSION)
        negotiated = PROTOCOL_VERSION

    return make_response(req.id, {
        "protocolVersion": negotiated,
        "capabilities": CAPABILITIES,
        "serverInfo": SERVER_INFO,
    })


def _handle_tools_list(req: JSONRPCRequest) -> dict:
    """处理 tools/list

    v0.5: 响应中包含 category 和 experimental 元数据。
    旧 MCP 客户端可忽略这些额外字段（JSON 语义安全）。
    v0.7.3: 默认只暴露 Agent-facing 工具（agent_visible=False 的 SDK 上报类
    工具不进清单，但 tools/call 按名调用仍可执行）。
    """
    tools = [
        {
            "name": t["name"],
            "description": t["description"],
            "inputSchema": t["inputSchema"],
            "category": t.get("category"),
            "experimental": t.get("experimental", False),
        }
        for t in get_agent_visible_tools()
    ]
    return make_response(req.id, {"tools": tools})


def _validate_tool_arguments(tool: dict, arguments) -> "str | None":
    """FIX: P1-C5 —— 按注册的 inputSchema 做轻量校验，返回错误描述或 None。

    此前 inputSchema 仅用于 tools/list 展示、从不校验：缺 required 参数或参数
    类型错误时，直接索引型 handler（network/git/spec/auto_test 等）抛
    KeyError/TypeError 被兜底捕获成 TOOL_INTERNAL，而 LLM 客户端依赖
    -32602 INVALID_PARAMS 语义做参数自纠错重试。

    只做两层轻量校验（不实现完整 JSON Schema，嵌套结构不递归）：
    1. arguments 必须为 dict（含显式 null）；
    2. required 字段存在性；
    3. 顶层字段类型与声明的 JSON 类型一致（string/number/integer/boolean/
       array/object；未声明的额外参数不拒绝——保持旧兼容）。
    """
    if not isinstance(arguments, dict):
        return "arguments 必须为对象"

    schema = tool.get("inputSchema") or {}
    for field_name in schema.get("required") or []:
        if field_name not in arguments:
            return f"缺少必填参数: {field_name}"

    properties = schema.get("properties") or {}
    type_map = {
        "string": str,
        "number": (int, float),
        "integer": int,
        "boolean": bool,
        "array": list,
        "object": dict,
    }
    for field_name, value in arguments.items():
        declared = properties.get(field_name)
        if not isinstance(declared, dict):
            continue
        expected = declared.get("type")
        py_types = type_map.get(expected)
        if py_types is None:
            continue
        # 显式 null：本项目所有声明类型的字段均不可为 null；放行会在
        # ingest_network/get_blame_for_frame 等索引/realpath 路径崩溃成
        # TOOL_INTERNAL，此处按类型错误归入 -32602
        if value is None:
            return f"参数 {field_name} 类型应为 {expected}，不接受 null"
        # bool 是 int 的子类：integer/number 不接受布尔值
        if isinstance(value, bool) and expected in ("integer", "number"):
            return f"参数 {field_name} 类型应为 {expected}"
        # integer 容忍整值 float（JSON 反序列化 20.0 为 float，handler 的
        # int()/min() 本可正常处理，不应收紧拒绝）
        if expected == "integer" and isinstance(value, float) and value.is_integer():
            continue
        if not isinstance(value, py_types):
            return f"参数 {field_name} 类型应为 {expected}"
    return None


def _validate_tool_name(params: dict) -> tuple[str | None, str | None]:
    """B25: 纯输入分类 —— 区分 name 键缺失 / 非字符串 / 合法字符串。

    只负责分类，不构造任何错误载体（JSONResponse / make_error / ToolExecutionError）。
    返回 (name, error_message)：
    - name 键缺失 → ("", None)：保持既有"未知工具 -32601"语义
    - name 键存在但非 str → (None, "工具名必须为字符串")：malformed params -32602
    - name 是 str → (name, None)
    """
    if "name" not in params:
        return ("", None)
    name = params["name"]
    if not isinstance(name, str):
        return (None, "工具名必须为字符串")
    return (name, None)


async def _handle_tools_call(req: JSONRPCRequest) -> dict:
    """处理 tools/call"""
    # FIX: P1-9i params 非 dict（list/str/null）时返回 -32602，避免 AttributeError → 500
    if not isinstance(req.params, dict):
        return make_error(req.id, INVALID_PARAMS, "Invalid params")
    params = req.params
    # FIX(B25): 非字符串 name 在 registry 查找前拒绝，避免 TypeError → -32603
    # 或可哈希非字符串（null/number/boolean）误判为未知工具 -32601。
    tool_name, name_error = _validate_tool_name(params)
    if name_error:
        return make_error(req.id, INVALID_PARAMS, name_error)
    arguments = params.get("arguments", {})

    tool = _tool_registry.get(tool_name)
    if not tool:
        return make_error(req.id, METHOD_NOT_FOUND, f"未知工具: {tool_name}")

    # FIX: P1-C5 —— inputSchema 轻量校验：参数错误 → -32602（LLM 自纠错依据），
    # 而非进入 handler 抛 KeyError/TypeError 被吞成 TOOL_INTERNAL
    validation_error = _validate_tool_arguments(tool, arguments)
    if validation_error:
        record_mcp_tool_call(tool_name, "invalid_params", 0.0)
        return make_error(req.id, INVALID_PARAMS, validation_error)

    timeout = settings.tool_timeout_seconds
    _tool_start = time.monotonic()
    handler = tool["handler"]

    if asyncio.iscoroutinefunction(handler) and not is_heavy_tool(tool_name):
        # async **轻量**：留进程内（C2 §6.1 B08——heavy 判定先于 async 判定，
        # async heavy 落入下方 heavy 分支进子进程；repair_async 等异步轻量
        # 必须留在进程内）。FIX: v0.6.6 async 工具绕过双池 —— 按同一
        # heavy 判定获取对应池槽位（async 无需线程池，仅信号量门控），
        # 超时同样走 TOOL_BUSY fast-fail，保持双池隔离语义。
        _, slots, pool_type = _get_tool_executor_and_slots(tool_name)
        busy_timeout = settings.tool_busy_queue_timeout
        wait_start = time.perf_counter()
        if not await _acquire_slot_or_fastfail(slots, busy_timeout, pool=_pool_for(pool_type)):
            wait_sec = time.perf_counter() - wait_start
            record_mcp_tool_busy(tool_name, pool_type, wait_sec)
            record_mcp_tool_call(tool_name, "busy", wait_sec)
            if busy_timeout <= 0:
                logger.warning("工具 %s (%s池) 执行队列已满（不等待，立即拒绝），已拒绝执行", tool_name, pool_type)
            else:
                logger.warning("工具 %s (%s池) 执行队列已满（等待 %.3fs 超时），已拒绝执行", tool_name, pool_type, wait_sec)
            return make_response(req.id, {
                "content": [
                    {
                        "type": "text",
                        "text": "工具执行队列已满，请稍后重试。",
                    }
                ],
                "isError": True,
                "error_code": "TOOL_BUSY",
                "_busy": True,
            })
        # FIX(v0.7.1-b1-5): async 工具取得槽位后补记 record_mcp_tool_wait，
        # 与同步分支口径一致（此前 async 排队耗时是指标盲区）。
        record_mcp_tool_wait(tool_name, pool_type, time.perf_counter() - wait_start)
        # DESIGN C1 §3.2 / §3.4：async 轻量的真实任务即 asyncio Task。
        # 在 **task** 上挂结算，槽位跟随「真实任务终结」归还；超时与取消都
        # **不再**由 awaiter 直接释放许可（旧实现 finally 无条件 release 是 B07 根因）。
        _async_pool = _pool_for(pool_type)
        _token = _async_pool.acquire_token(loop=asyncio.get_running_loop(), semaphore=slots)
        _task = asyncio.ensure_future(handler(arguments))
        _task.add_done_callback(
            lambda _t, _tok=_token, _p=_async_pool: _p.settle(_tok)
        )
        try:
            result = await asyncio.wait_for(_task, timeout=timeout)
            record_mcp_tool_call(tool_name, "ok", time.monotonic() - _tool_start)
        except asyncio.TimeoutError:
            record_mcp_tool_call(tool_name, "timeout", timeout)
            logger.warning("工具 %s 执行超时(>%ss)，已终止", tool_name, timeout)
            return make_response(req.id, {
                "content": [
                    {
                        "type": "text",
                        "text": f"工具执行超时(>{timeout}s)，已中止",
                    }
                ],
                "isError": True,
                "error_code": "TOOL_TIMEOUT",
                "_timed_out": True,
            })
        except Exception:
            record_mcp_tool_call(tool_name, "error", time.monotonic() - _tool_start)
            logger.exception("工具 %s 执行失败", tool_name)
            return make_response(req.id, {
                "content": [
                    {
                        "type": "text",
                        "text": "工具执行失败，详情见服务端日志",
                    }
                ],
                "isError": True,
                "error_code": "TOOL_INTERNAL",
            })
    else:
        # 同步工具 / **heavy 工具（sync 与 async 都进子进程，C2 §6.1 B08）**：
        # 获取执行槽位，带等待超时，防止线程池排队堆积与饥饿
        executor, slots, pool_type = _get_tool_executor_and_slots(tool_name)
        busy_timeout = settings.tool_busy_queue_timeout
        wait_start = time.perf_counter()
        if not await _acquire_slot_or_fastfail(slots, busy_timeout, pool=_pool_for(pool_type)):
            wait_sec = time.perf_counter() - wait_start
            record_mcp_tool_busy(tool_name, pool_type, wait_sec)
            record_mcp_tool_call(tool_name, "busy", wait_sec)
            if busy_timeout <= 0:
                logger.warning("工具 %s (%s池) 执行队列已满（不等待，立即拒绝），已拒绝执行", tool_name, pool_type)
            else:
                # FIX(v0.7.1-b1-4): 打印实际等待时长（与 async 分支口径一致），
                # 此前误打配置超时 busy_timeout，误导排障。
                logger.warning("工具 %s (%s池) 执行队列已满（等待 %.3fs 超时），已拒绝执行", tool_name, pool_type, wait_sec)
            return make_response(req.id, {
                "content": [
                    {
                        "type": "text",
                        "text": "工具执行队列已满，请稍后重试。",
                    }
                ],
                "isError": True,
                "error_code": "TOOL_BUSY",
                "_busy": True,
            })

        wait_sec = time.perf_counter() - wait_start
        record_mcp_tool_wait(tool_name, pool_type, wait_sec)

        # FIX: C2 —— 重型工具在子进程执行，父进程内存态（如 spec_store）对子进程
        # 不可见，派发前先经 prepare_args 在父进程预处理入参（如 spec_id→spec）。
        if pool_type == "heavy":
            prepare = tool.get("prepare_args")
            if prepare is not None:
                try:
                    arguments = prepare(arguments)
                except Exception:
                    logger.exception("工具 %s prepare_args 预处理失败，沿用原入参", tool_name)

        # DESIGN C1 §3.1 / §3.4：结算挂到**真实任务**上，槽位跟随真实执行终结归还。
        _sync_pool = _pool_for(pool_type)
        _sync_loop = asyncio.get_running_loop()
        _sync_token = _sync_pool.acquire_token(loop=_sync_loop, semaphore=slots)
        sync_future: asyncio.Future | None = None
        real_future = None
        try:
            if pool_type == "heavy":
                # FIX: C2 —— 重活进程隔离：子进程执行 + 超时 terminate() 强杀。
                # 等待动作放在默认线程池的一个线程里（内部按 timeout 自限并强杀子进程），
                # 事件循环只 await 该线程结果，不阻塞；超时后无僵尸、不打满任何池。
                # W1 阶段：结算挂在「收割线程返回」上；W2 将改为由条目 REAPED 驱动。
                sync_future = _sync_loop.run_in_executor(
                    None,
                    run_heavy_tool_blocking,
                    handler.__module__,
                    handler.__name__,
                    arguments,
                    float(timeout),
                )
                sync_future.add_done_callback(
                    lambda _f, _tok=_sync_token, _p=_sync_pool: _p.settle(_tok)
                )
                result = await sync_future
            else:
                # 关键：用 executor.submit 拿到**真实 concurrent.futures.Future**，
                # 回调挂它上面（不是 asyncio 包装 future）。这样「超时响应早已返回、
                # 线程后来才终于跑完」时才会结算——旧实现在 finally 里无条件
                # slots.release() 正是 B07「超发」的根因。
                real_future = executor.submit(handler, arguments)
                _sync_pool.attach(real_future, _sync_token)
                result = await asyncio.wait_for(
                    asyncio.wrap_future(real_future), timeout=timeout
                )
            record_mcp_tool_call(tool_name, "ok", time.monotonic() - _tool_start)
        except asyncio.TimeoutError:
            record_mcp_tool_call(tool_name, "timeout", timeout)
            # 「停止等待」的善意尝试，**不是记账事件**：对运行中线程 cancel()
            # 返回 False，槽位继续由真实 future 的回调在任务真正终结时归还。
            if real_future is not None:
                real_future.cancel()
            elif sync_future is not None:
                sync_future.cancel()
            if pool_type == "heavy":
                logger.warning(
                    "工具 %s (heavy/子进程) 执行超时(>%ss)，子进程已强杀回收，无僵尸残留",
                    tool_name, timeout,
                )
            else:
                logger.warning("工具 %s 执行超时(>%ss)，已终止", tool_name, timeout)
                logger.warning(
                    "工具 %s 同步线程超时后 cancel() 无法中断已运行线程（%s池），"
                    "线程将继续占用线程池资源直至完成；若持续出现请排查工具耗时。",
                    tool_name,
                    pool_type,
                )
            return make_response(req.id, {
                "content": [
                    {
                        "type": "text",
                        "text": f"工具执行超时(>{timeout}s)，已中止",
                    }
                ],
                "isError": True,
                "error_code": "TOOL_TIMEOUT",
                "_timed_out": True,
            })
        except asyncio.CancelledError:
            # DESIGN C1 §1.4a/b：仅「从未运行」的任务可补偿结算；已运行/已完成
            # 的交给真实 future 回调。两条路径由同一 token 闸门互斥，不会双结算。
            if real_future is not None and real_future.cancel():
                _sync_pool.settle(_sync_token)
            raise
        except Exception:
            record_mcp_tool_call(tool_name, "error", time.monotonic() - _tool_start)
            logger.exception("工具 %s 执行失败", tool_name)
            # submit 抛错（executor 已关闭）等「任务从未入队」路径需补偿结算；
            # handler 自身抛错时 real_future 已完成、回调已结算，此处幂等空转。
            _sync_pool.settle(_sync_token)
            return make_response(req.id, {
                "content": [
                    {
                        "type": "text",
                        "text": "工具执行失败，详情见服务端日志",
                    }
                ],
                "isError": True,
                "error_code": "TOOL_INTERNAL",
            })

    _elapsed = time.monotonic() - _tool_start
    try:
        _size = len(json.dumps(result, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        _size = 0
    # Phase 3 D5：记录 Tool 响应耗时/大小（仅日志，不修改协议响应、不打印敏感负载）
    logger.info("MCP HTTP tool=%s response_ms=%.1f response_size=%d", tool_name, _elapsed * 1000, _size)
    # FIX: R7 —— handler 正常返回但代表执行失败的 dict（含非空 error 键）必须
    # 标记 isError=true，与 stdio 的 ToolExecutionError 契约一致；正常无数据
    # （found=false 等，无 error 键）仍是成功结果。
    # FIX(R8): 判定改走工具自己的谓词（未声明则仍用全局契约），
    # 结论型工具的载荷不再被误标为失败。
    if tool_failure_predicate(tool)(result):
        return make_response(req.id, {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(result, ensure_ascii=False, default=str),
                }
            ],
            "isError": True,
        })
    return make_response(req.id, {
        "content": [
            {
                "type": "text",
                "text": json.dumps(result, ensure_ascii=False, default=str),
            }
        ],
        "isError": False,
    })


def _handle_ping(req: JSONRPCRequest) -> dict:
    """处理 ping"""
    return make_response(req.id, {})


# 方法路由表
_METHOD_MAP = {
    "initialize": _handle_initialize,
    "tools/list": _handle_tools_list,
    "tools/call": _handle_tools_call,
    "ping": _handle_ping,
}


async def dispatch(jsonrpc_request: JSONRPCRequest) -> dict:
    """
    路由 JSON-RPC 请求到对应处理方法

    输入: JSONRPCRequest
    输出: JSON-RPC Response dict（可直接序列化返回）
    """
    method = jsonrpc_request.method
    handler = _METHOD_MAP.get(method)

    if handler is None:
        return make_error(
            jsonrpc_request.id,
            METHOD_NOT_FOUND,
            f"未支持的方法: {method}",
        )

    try:
        result = handler(jsonrpc_request)
        if asyncio.iscoroutine(result):
            result = await result
        return result
    except Exception:
        logger.exception(f"处理 {method} 时出错")
        return make_error(
            jsonrpc_request.id,
            INTERNAL_ERROR,
            "内部错误，详情见服务端日志",
        )


async def dispatch_raw(raw: str | bytes) -> dict:
    """解析原始请求文本并分发"""
    try:
        req = parse_request(raw)
    except JSONParseError as e:
        return make_error(None, PARSE_ERROR, str(e))
    except InvalidRequestError as e:
        return make_error(None, INVALID_REQUEST, str(e))

    return await dispatch(req)
