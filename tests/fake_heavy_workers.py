"""假 heavy worker 载体（W2-1 故障注入矩阵专用）。

由测试以 ``python -m tests.fake_heavy_workers <mode>`` 启动，每个模式对应
C2 §3/§3.1 的一个握手边界行为。worker 只 import stdlib 与
``app.mcp.protocol.heavy_spawn``（stdlib-only 启动支撑件，无 server 依赖），
遵守轻量入口原则。
"""

from __future__ import annotations

import os
import pickle
import sys
import time

from app.mcp.protocol.heavy_spawn import (
    GO_BYTE,
    READY_BYTE,
    RESULT_HEADER_LEN,
    close_result_write_end,
    read_request_frame,
    takeover_result_write_end,
    write_frame,
)


def _echo_payload(request_bytes: bytes) -> bytes:
    """把请求参数原样 pickle 回传（校验父→子→父往返）。"""
    arguments = pickle.loads(request_bytes)
    return pickle.dumps(("ok", arguments), protocol=pickle.HIGHEST_PROTOCOL)


def main() -> int:
    mode = sys.argv[1]
    wfd = takeover_result_write_end()  # 接管结果写端并清除可继承属性

    if mode == "crash_before_ready":
        # ① go 前崩溃：ready 永不到达
        os._exit(3)

    if mode == "hang_no_ready":
        # ③ 握手超时：不发 ready、不退出
        time.sleep(120)
        return 0

    if mode == "bad_length":
        # 协议错误：ready 正常但帧长度无效（超出父侧上限）
        write_frame(wfd, READY_BYTE, header_len=0)
        os.write(wfd, __import__("struct").pack("<Q", 2**63))
        time.sleep(120)
        return 0

    if mode == "garbage_ready":
        # 协议错误：ready 字节错
        write_frame(wfd, b'X', header_len=0)  # 'X'
        time.sleep(120)
        return 0

    if mode == "half_result":
        # 结果半帧停住：R 之后只写半个帧头（4 字节）然后挂住
        import struct as _struct
        request_bytes = read_request_frame(sys.stdin.buffer)
        write_frame(wfd, READY_BYTE, header_len=0)
        go = sys.stdin.buffer.read(1)
        if go != GO_BYTE:
            os._exit(9)
        os.write(wfd, _struct.pack("<Q", 64)[:4])
        time.sleep(120)
        return 0

    if mode == "grandchild_then_exit":
        # 孙进程收容边界：拉起孙进程后发结果帧并退出；孙进程**不得**持有
        # 结果写端（takeover 已清继承属性）——父侧读取器必须能立即结束，
        # 不得依赖杀孙进程才读到 EOF。回传孙进程 pid 供测试清理。
        import pickle as _pickle
        import subprocess as _subprocess
        gc = _subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdin=_subprocess.DEVNULL, stdout=_subprocess.DEVNULL,
            stderr=_subprocess.DEVNULL,
        )
        request_bytes = read_request_frame(sys.stdin.buffer)
        write_frame(wfd, READY_BYTE, header_len=0)
        go = sys.stdin.buffer.read(1)
        if go != GO_BYTE:
            gc.kill()
            os._exit(9)
        payload = _pickle.dumps(
            ("ok", {"gc_pid": gc.pid}), protocol=_pickle.HIGHEST_PROTOCOL
        )
        write_frame(wfd, payload, header_len=RESULT_HEADER_LEN)
        close_result_write_end(wfd)
        return 0

    # 以下模式都需要读请求帧
    request_bytes = read_request_frame(sys.stdin.buffer)
    write_frame(wfd, READY_BYTE, header_len=0)

    if mode == "echo":
        go = sys.stdin.buffer.read(1)
        if go != GO_BYTE:
            os._exit(9)
        noise = os.environ.get("FAKE_FD1_NOISE")
        if noise:
            os.write(1, noise.encode())  # R09：fd1 噪声不得污染结果通道
        write_frame(wfd, _echo_payload(request_bytes), header_len=RESULT_HEADER_LEN)
        close_result_write_end(wfd)
        return 0

    if mode == "echo_big":
        go = sys.stdin.buffer.read(1)
        if go != GO_BYTE:
            os._exit(9)
        arguments = pickle.loads(request_bytes)
        payload = pickle.dumps(
            ("ok", b"x" * int(arguments["size"])), protocol=pickle.HIGHEST_PROTOCOL
        )
        write_frame(wfd, payload, header_len=RESULT_HEADER_LEN)
        close_result_write_end(wfd)
        return 0

    if mode == "ready_then_exit_after_go":
        # go 后、结果帧前崩溃：归入 exited without result
        go = sys.stdin.buffer.read(1)
        if go != GO_BYTE:
            os._exit(9)
        os._exit(1)

    if mode == "refuse_go_marker":
        # 提交被拒场景：若收到 G 则退出码 7（父侧必须保证永不写 G）
        go = sys.stdin.buffer.read(1)
        os._exit(7 if go == GO_BYTE else 6)

    raise RuntimeError(f"unknown fake worker mode: {mode}")


if __name__ == "__main__":
    sys.exit(main())
