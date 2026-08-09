# -*- mode: python ; coding: utf-8 -*-
"""Lujo-MCP stdio MCP Server — PyInstaller 打包配置。

用法（在项目根目录）：
    pip install pyinstaller
    pyinstaller --clean -y packaging/lujo-mcp-server.spec

产物：dist/lujo-mcp-server(.exe) 单个二进制，供 npm 平台包分发。
"""

import os

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

a = Analysis(
    ["packaging/entry_stdio.py"],
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
        "pydantic_settings",
        "mcp.server",
        "mcp.server.stdio",
        "mcp.types",
        "mcp.shared",
        "httpx",
        "psutil",
        "asyncpg",
        "psycopg2",
        "psycopg2.extensions",
        "qdrant_client",
        "opentelemetry",
        "opentelemetry.api",
        "opentelemetry.sdk",
        "opentelemetry.exporter.otlp.proto.grpc",
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
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,   # stdio MCP Server 需要控制台/标准流
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
