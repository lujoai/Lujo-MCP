"""Lujo-MCP AI Runtime Debug Intelligence Layer.

承载 MCP 协议之外的运行时能力：上下文装配、采集、验证、存储、状态。
MCP / API 层作为适配器依赖本包；**本包不依赖任何协议层**（api / mcp），
也不依赖 agent / llm / rag。该不变量由
``tests/unit/test_architecture_boundaries.py::TestRuntimeLayerBoundary`` 以
AST 级扫描 + 干净子进程真实写入两种方式锁定（W14 / P1-ARC-1 之前，
``core/logs.py`` 与 ``core/errors.py`` 有三处惰性 ``from app.api.dashboard
import invalidate_cache``，是本声明的唯一反例；现改为 ``core/invalidation.py``
的订阅/广播，由 api 层注册监听器）。

公开面（W14 / P2-ARC-1）
------------------------
上层**应当**按下列入口使用本包；这些是稳定契约，其余模块视为内部实现，
签名可能随重构变化：

- 现场写入：``core.logs``（``create_request_id`` / ``add_log`` /
  ``add_logs_batch`` / ``get_logs``）、``core.trace_repo``（``save_trace`` /
  ``save_network_record`` / ``save_ui_event``）、``core.errors``（``record`` /
  ``list_recent``）
- 现场读取：``context.builder``（``build_context`` / ``build_debug_context``）
- 采集：``collectors.runtime``（``collect_runtime_snapshot``）、
  ``collectors.code_locator``（``get_snippets_for_frames``）
- 存储：``core.storage.factory``（``get_trace_store`` / ``get_error_store`` /
  ``get_spec_store`` / ``get_session_store`` / ``get_knowledge_store``）
- 安全边界：``core.redaction``（``redact`` / ``redact_nested``）、
  ``core.git``（受 ``git_path_whitelist`` 约束）、``verifier.ui_runner``
  （``is_safe_url``，受 SSRF 白名单约束）
- 生命周期钩子：``hooks.exception_hook``、``core.invalidation``

刻意**不**在这里做 re-export 门面：实测上层有 75 处深引用、分布在 30 个文件，
把它们全部改为 ``from app.runtime import X`` 是零行为收益的机械 churn，且会
与存储/协议各包争抢同一批文件。真正需要被强制的是**依赖方向**，那已由上面
的守卫测试承担；本 docstring 承担「哪些入口算公开契约」。
"""
