"""PyInstaller 打包入口 —— stdio MCP Server。

打包后的二进制等价于 `python -m app.mcp_server`，
供 Claude Desktop / Cursor / Trae 等 MCP 客户端通过 stdio 启动。
"""
import asyncio

from app.mcp_server import main

if __name__ == "__main__":
    asyncio.run(main())
