"""W2-5 冻结子进程 smoke：源码父 + 冻结子的结果通道往返（R09 通道级）。

覆盖 DESIGN_C2 §8.1「Windows 原生 HANDLE 接管与关闭」的**冻结侧**实测：
冻结 exe 作为 heavy worker 子进程（entry_stdio 分流 → heavy_worker_entry
同一 bootstrap），经 heavy_spawn 的 HANDLE→CRT fd 接管完成握手与结果回传。

环境理由（计数纪律）：冻结产物不入库（docs/internal/ 为 git 忽略目录）；产物缺失时
skip 并注明构建命令。构建：
    pyinstaller --clean -y packaging/lujo-mcp-server.spec \
        --distpath docs/internal/c_batch/frozen_smoke/dist --workpath docs/internal/c_batch/frozen_smoke/build
"""

from __future__ import annotations

import os
import pickle
import time

import pytest

import app.mcp.protocol.heavy_spawn as hs

_FROZEN_EXE = os.path.join(
    os.path.dirname(__file__), "..", "..",
    "docs", "internal", "c_batch", "frozen_smoke", "dist", "lujo-mcp-server.exe",
)

pytestmark = [
    pytest.mark.skipif(
        not os.path.exists(_FROZEN_EXE),
        reason="冻结产物未构建（环境理由：构建命令见模块 docstring；产物不入库）",
    ),
]


def test_frozen_child_result_channel_roundtrip():
    """源码父 + 冻结子：HANDLE 接管、ready/go 握手、结果帧回传、退出码 0。

    handler 用真实捆绑工具（app.mcp.tools.context_api.handler，无浏览器依赖、
    无副作用）；_heavy_selftest 未入冻结包（生产 spec 不携带测试辅助件），
    故 R09 的 fd1 噪声四断言以源码子进程为准（通道级机制与形态无关）。
    """
    attempt = hs.spawn_attempt(
        1,
        [_FROZEN_EXE, "--lujo-heavy-worker", "app.mcp.tools.context_api", "handler"],
    )
    try:
        request = pickle.dumps({"request_id": "frozen-smoke"}, protocol=pickle.HIGHEST_PROTOCOL)
        payload = hs.handshake(
            attempt, request, deadline=time.monotonic() + 60.0,
            allow_commit=lambda: True,
        )
        status, value = pickle.loads(payload)
        assert status == "ok", f"冻结子进程返回结构化错误: {value}"
        assert isinstance(value, dict)
    finally:
        exitcode = hs.terminate_and_reap(attempt, grace=10.0)
    assert exitcode == 0
