# -*- mode: python ; coding: utf-8 -*-
"""Lujo-MCP stdio MCP Server — PyInstaller 打包配置。

用法（在项目根目录）：
    pip install pyinstaller
    pyinstaller --clean -y packaging/lujo-mcp-server.spec

产物：dist/lujo-mcp-server(.exe) 单个二进制，供 npm 平台包分发。
"""

import os
import sys

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
        # 内置 Web 演示页 / SDK / 迁移 SQL（供 HTTP / Dashboard 路由读取）
        (os.path.join(ROOT, "app", "web"), os.path.join("app", "web")),
        (os.path.join(ROOT, "browser-sdk"), "browser-sdk"),
        (os.path.join(ROOT, "migrations"), "migrations"),
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
        "asyncpg",
        "psycopg2",
        "psycopg2.extensions",
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
        "playwright",
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
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,   # stdio MCP Server 需要控制台/标准流
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
