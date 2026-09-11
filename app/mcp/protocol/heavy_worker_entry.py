"""heavy worker `python -m` 引导入口（DESIGN_C2 §1.1/§2.1，B08 前置 W2-2）。

源码模式的 worker 子进程入口；冻结模式经 ``packaging.entry_stdio`` 分流到
同一协议（C2 §4：源码/冻结一份 bootstrap，防第二次漂移）。命令行与冻结
worker 同构：``python -m app.mcp.protocol.heavy_worker_entry
--lujo-heavy-worker <handler_module> <handler_name>``。

子进程时序（§2.1，逐条对齐）：

1. **接管结果写端**（:func:`heavy_spawn.takeover_result_write_end`）——必须
   发生在任何 handler 导入或孙进程创建之前；接管后立即清除可继承属性；
2. 读请求**原始字节**帧——go 前不做 ``pickle.loads``、不导入 handler
   （反序列化恢复对象可能提前触发导入或副作用）；
3. 发单字节 ``R``（ready，走专用结果通道）；
4. **阻塞等**单字节 ``G``（go，走 stdin）；未收到 go 一律退出、不执行业务；
5. go 之后才 ``pickle.loads`` 参数、``importlib`` 导入 handler、执行；
6. 写结果帧（8 字节小端长度 + pickle 体）到结果通道并**关闭写端**
   （本入口是写端的唯一最终关闭者）。

失败兜底：go 之后的一切异常（参数反序列化、handler 导入、handler 执行、
结果不可 pickle）都转成 ``("error", "<说明>")`` 结构化载荷回传，不挂死、
不静默——与既有源码/冻结入口的兜底口径一致（C2 §4 第 4 条）。

轻量入口原则：本模块只 import stdlib 与 ``heavy_spawn``（stdlib-only 启动
支撑件）及 ``heavy_process`` 的 flag 常量（该模块同样只依赖 stdlib、无
模块级副作用）；**禁止 import app 服务器任何模块**（server / mcp_server /
tools 等）。
"""

from __future__ import annotations

import importlib
import pickle
import sys

from app.mcp.protocol import heavy_spawn
from app.mcp.protocol.heavy_process import _FROZEN_WORKER_FLAG as WORKER_FLAG

_PICKLE_PROTOCOL = pickle.HIGHEST_PROTOCOL


def _execute(handler_module: str, handler_name: str, arguments):
    """go 后执行：导入 handler 并同步执行（W2-3 将抽为共享 resolve_and_run，
    届时源码入口 / 冻结入口 / 本入口三处共用同一份 sync/async 判定与
    R11 协程单次执行规则；当前世界 heavy handler 仅同步）。"""
    module = importlib.import_module(handler_module)
    handler = getattr(module, handler_name)
    return handler(arguments)


def main(argv: list[str] | None = None) -> int:
    """worker 入口：返回进程退出码（0 成功；2 用法/握手错误；1 执行期兜底失败）。"""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 3 or args[0] != WORKER_FLAG:
        return 2
    _flag, handler_module, handler_name = args

    # 1) 接管结果写端并清除可继承属性（早于任何 handler 导入或孙进程创建）
    try:
        wfd = heavy_spawn.takeover_result_write_end()
    except OSError:
        return 2

    write_end_closed = False

    def _close_write_end_once() -> None:
        nonlocal write_end_closed
        if not write_end_closed:
            heavy_spawn.close_result_write_end(wfd)
            write_end_closed = True

    try:
        # 2) 读请求原始字节帧——go 前不反序列化、不导入 handler
        request_bytes = heavy_spawn.read_request_frame(sys.stdin.buffer)

        # 3) 发 ready（专用结果通道）
        heavy_spawn.write_frame(wfd, heavy_spawn.READY_BYTE, header_len=0)

        # 4) 阻塞等 go（stdin 单字节）
        go = sys.stdin.buffer.read(1)
        if go != heavy_spawn.GO_BYTE:
            # 父侧未提交（终止先于 GO_COMMITTED 等）：不执行业务
            _close_write_end_once()
            return 2

        # 5) go 之后才反序列化参数、导入 handler、执行
        try:
            arguments = pickle.loads(request_bytes)
        except Exception as exc:  # noqa: BLE001 —— 结构化回传，不静默
            payload = pickle.dumps(
                ("error", f"{type(exc).__name__}: {exc}"), _PICKLE_PROTOCOL
            )
            heavy_spawn.write_frame(wfd, payload, header_len=heavy_spawn.RESULT_HEADER_LEN)
            _close_write_end_once()
            return 0

        try:
            result = _execute(handler_module, handler_name, arguments)
            try:
                payload = pickle.dumps(("ok", result), _PICKLE_PROTOCOL)
            except Exception:
                payload = pickle.dumps(
                    ("error", "heavy tool result not serializable"), _PICKLE_PROTOCOL
                )
        except Exception as exc:  # noqa: BLE001 —— handler 异常结构化回传
            payload = pickle.dumps(
                ("error", f"{type(exc).__name__}: {exc}"), _PICKLE_PROTOCOL
            )

        # 6) 写结果帧并关闭写端（唯一最终关闭者）
        heavy_spawn.write_frame(wfd, payload, header_len=heavy_spawn.RESULT_HEADER_LEN)
        _close_write_end_once()
        return 0
    except (OSError, EOFError, ValueError):
        # 管道断裂 / 截断帧 / stdin EOF：无法回传结构化错误，按退出码表达
        _close_write_end_once()
        return 1
    finally:
        _close_write_end_once()


if __name__ == "__main__":
    sys.exit(main())
