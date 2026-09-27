# -*- mode: python ; coding: utf-8 -*-
"""Lujo-MCP stdio MCP Server — PyInstaller 打包配置。

用法（在项目根目录）：
    pip install pyinstaller
    pyinstaller --clean -y packaging/lujo-mcp-server.spec

产物：dist/lujo-mcp-server(.exe) 单个二进制，供 npm 平台包分发。
"""

import os
import sys

from PyInstaller.utils.hooks import collect_data_files

# PyInstaller 通过 exec() 加载 spec，命名空间不含 __file__；
# SPECPATH 是 PyInstaller 专门注入的变量——当前 spec 文件的所在目录。
_spec_dir = os.path.abspath(SPECPATH)
ROOT = os.path.abspath(os.path.join(_spec_dir, ".."))
# 兜底：若解析后找不到 app 目录（例如 CI 中 cwd 就是项目根）就用当前工作目录。
if not os.path.isdir(os.path.join(ROOT, "app")):
    ROOT = os.getcwd()

a = Analysis(
    [os.path.join(ROOT, "packaging", "entry_stdio.py")],
    pathex=[ROOT],
    binaries=[],
    datas=[
        # 内置 Web 演示页 / SDK（供 HTTP / Dashboard 路由读取；
        # PostgreSQL migrations 已随 Step 3 WP6 归档，不再打包）
        (os.path.join(ROOT, "app", "web"), os.path.join("app", "web")),
        (os.path.join(ROOT, "browser-sdk"), "browser-sdk"),
        # v0.9.8 浏览器能力进冻结包：playwright 的 node driver（node.exe +
        # package/ 整棵树，含 browsers.json）必须完整收集——playwright 运行时
        # 经 inspect.getfile(playwright) 相对定位 driver（冻结后解析到
        # _MEIPASS/playwright/driver），缺任一文件浏览器通道即失效。
        # 注意：只收集库与 driver，不收集 chromium 浏览器二进制（150MB+ 且
        # 冻结内无法 playwright install）——浏览器由 browser_launcher 回退链
        # （chromium → 系统 Chrome → 系统 Edge）在运行时解析，无需设
        # PLAYWRIGHT_BROWSERS_PATH / PLAYWRIGHT_NODEJS_PATH。
        *collect_data_files("playwright"),
    ],
    hiddenimports=[
        # 动态/间接导入的库，PyInstaller 静态分析可能遗漏
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.http.h11_impl",
        "uvicorn.protocols.http.httptools_impl",
        "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
        "uvicorn.lifespan.off",
        "pydantic",
        "pydantic.deprecated",
        "pydantic_settings",
        "fastapi",
        "fastapi.responses",
        "starlette",
        "starlette.middleware",
        "starlette.middleware.base",
        "starlette.requests",
        "starlette.responses",
        "mcp",
        "mcp.server",
        "mcp.server.stdio",
        "mcp.server.sse",
        "mcp.server.streamable_http",
        "mcp.types",
        "mcp.shared",
        "mcp.shared.abc",
        "httpx",
        "httpx._client",
        "openai",
        "openai.resources",
        "dotenv",
        "psutil",
        "redis",
        "redis.asyncio",
        "pybreaker",
        "qdrant_client",
        "opentelemetry",
        "opentelemetry.api",
        "opentelemetry.sdk",
        "opentelemetry.sdk.trace",
        "opentelemetry.sdk.resources",
        "opentelemetry.exporter.otlp.proto.grpc",
        "opentelemetry.exporter.otlp.proto.grpc.trace_exporter",
        "opentelemetry.proto",
        # v0.9.8 浏览器能力：playwright 双 API + greenlet（C 扩展依赖）。
        # sync_api/async_api 经延迟导入加载，静态分析容易整包遗漏。
        "playwright",
        "playwright.sync_api",
        "playwright.async_api",
        "playwright._impl",
        "greenlet",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
        "pandas",
        "numpy",
        "PIL",
        "pytest",
        "ruff",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="lujo-mcp-server",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=sys.platform == "win32",
    # 已知坑：UPX 压缩 playwright node driver 的 node.exe 会产生损坏二进制
    # （启动即崩），必须排除。
    upx_exclude=["node.exe"],
    runtime_tmpdir=None,
    console=True,   # stdio MCP Server 需要控制台/标准流
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
