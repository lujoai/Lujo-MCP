"""PyInstaller 打包入口 —— stdio MCP Server。

打包后的二进制等价于 `python -m app.mcp_server`，
供 Claude Desktop / Cursor / Trae 等 MCP 客户端通过 stdio 启动。
"""
import asyncio
import multiprocessing

from app.mcp_server import main

if __name__ == "__main__":
    # PyInstaller 冻结程序中的 heavy 工具使用 spawn 创建子进程。
    # 必须在进入 asyncio 主循环前分流 multiprocessing 的子进程入口，
    # 否则 Windows 产物可能把子进程再次当作 MCP 主进程启动。
    multiprocessing.freeze_support()
    asyncio.run(main())
