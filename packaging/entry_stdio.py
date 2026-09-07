"""PyInstaller 打包入口 —— stdio MCP Server。

打包后的二进制等价于 `python -m app.mcp_server`，
供 Claude Desktop / Cursor / Trae 等 MCP 客户端通过 stdio 启动。
"""
import asyncio
import multiprocessing

if __name__ == "__main__":
    # 必须在导入 app.mcp_server 之前分流 multiprocessing 的子进程入口。
    # Windows 冻结程序的 spawn 子进程会重新执行这个入口；过晚调用会先
    # 导入并初始化完整 MCP 服务，导致 heavy 子进程无法及时进入 target。
    multiprocessing.freeze_support()
    from app.mcp_server import main

    asyncio.run(main())
