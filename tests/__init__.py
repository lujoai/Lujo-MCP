"""``tests`` 包标记。

⚠️ 本文件**曾经**含一份 ``TestContextBuilder``（三个用例），与
``tests/unit/test_context_builder.py`` 逐字重复；pytest 不收集 ``__init__.py``，
所以那三个用例**从未执行过**（W15 / P3-TEST-1 已删除，覆盖由
``tests/unit/test_context_builder.py`` 承担，用例名与断言一一对应）。

下面这行 import 是**有意保留的副作用**，不是残留：它在 pytest 导入 ``tests``
包时就触达 ``app.config`` 并创建 ``settings`` 单例，早于 ``tests/conftest.py``
的函数体执行。``tests/conftest.py`` 顶部那段「FIX: e2e uvicorn 启动被 SEC-03
误杀（host 哨兵时序失效）」与 M13 的 API_KEY 哨兵说明，正是围绕这个时序写的，
其兜底（直接重置单例）也以此为前提。删掉它会改变测试进程的 bootstrap 时序——
那属于独立变更，须单独评估并重跑全量，不要在清理重复测试类时顺手做。
"""
from app.runtime.context.builder import build_context  # noqa: F401 —— 副作用导入，见上方说明
