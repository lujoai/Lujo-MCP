"""单元测试：E 批 PG-1 —— STORAGE_BACKEND=postgresql 启动告警（V8-5）

规格来源：DEV_PLAN.md 附录 B §3（原 PLAN_kb-sqlite-persist.md）S2-1：
- 覆盖 HTTP / 纯 stdio / 统一模式：三者共享同一启动点（app.config Settings
  构造；app/main.py 与 app/mcp_server.py 均经 ``from app.config import
  settings`` 在导入期触发），传输差异只在日志路由。
- 每进程最多一次；stdio 仅 stderr；memory 静默；非法 backend 仍 fail-fast。
- 告警不含凭据；不把 PG 自动降级 memory。
全部用例不连接真实 PostgreSQL。
"""

import logging
import threading
from pathlib import Path

import pytest

import app.config as app_config
from app.config import Settings

_PG_MARKER = "STORAGE_BACKEND=postgresql"
_WARNING_MARKER = "实验性后端"
# 告警文案钉死（与 DEV_PLAN ROADMAP §6.1 定稿逐字一致），防止并发修复时被顺手改动
_EXPECTED_WARNING = (
    "检测到 STORAGE_BACKEND=postgresql：PG 为实验性后端，存在已知未修问题，"
    "不承诺支持，推荐使用默认 SQLite 笔记本。SQLite 仅持久化 KB，"
    "运行现场的 STORAGE_BACKEND 默认仍为 memory；本提示不会自动迁移 PG 数据。"
    "已知问题：PG KB 初始化失败可能无法降级、错误计数可能偏低、"
    "调度失败后的节流记账可能抑制有效写入（报告 #5/#6/#7）。"
    "stdio asyncpg pool 关闭风险尚未验证（报告 §4.3 #8）。"
)


def _reset_pg_warning_flag(monkeypatch) -> None:
    """重置进程级一次性告警标志；monkeypatch 自动还原。

    hasattr 守卫让本文件在「告警未实现」的旧代码上同样可运行——
    红灯反映的是缺失的告警行为，而不是 monkeypatch 目标不存在。
    """
    if hasattr(app_config, "_pg_backend_warning_emitted"):
        monkeypatch.setattr(app_config, "_pg_backend_warning_emitted", False)


def _fresh_pg_settings(monkeypatch, **overrides) -> Settings:
    _reset_pg_warning_flag(monkeypatch)
    return Settings(storage_backend="postgresql", **overrides)


def _pg_records(caplog) -> list:
    return [r for r in caplog.records if _WARNING_MARKER in r.getMessage()]


def _config_marker_lines() -> tuple[int, int]:
    """从源码定位 model_post_init 内 PG 告警的「检查行」与「置位行」行号。"""
    source = Path(app_config.__file__).read_text(encoding="utf-8").splitlines()
    check = next(
        i for i, ln in enumerate(source, 1)
        if ln.strip().startswith("if ") and "_pg_backend_warning_emitted" in ln
    )
    assign = next(
        i for i, ln in enumerate(source, 1)
        if ln.strip() == "_pg_backend_warning_emitted = True"
    )
    return check, assign


class _CheckArrivalGate:
    """确定性交错协调器：置位行执行前必须等全部线程完成检查行求值。

    LINE 事件先于该行代码执行触发，因此把等待挂在置位行上，能保证
    「任一线程写标志」发生在「所有线程读标志」之后——旧实现（裸布尔、
    检查与置位非原子）下两线程必然都通过检查并各自告警（=2 条，红灯）；
    锁内 check+set 的新实现下，后到线程的检查被告警锁挡住、事件等不到，
    超时兜底放行（绿灯）。红灯由对方的真实检查事件驱动，不依赖 sleep。
    """

    def __init__(self, check_line: int, set_line: int, n_threads: int = 2, timeout: float = 2.0):
        self.check_line = check_line
        self.set_line = set_line
        self._n = n_threads
        self._timeout = timeout
        self._lock = threading.Lock()
        self._checked = 0
        self._all_checked = threading.Event()

    def note_checked(self) -> None:
        with self._lock:
            self._checked += 1
            if self._checked >= self._n:
                self._all_checked.set()

    def wait_all_checked(self) -> None:
        self._all_checked.wait(self._timeout)


def _line_gate_tracer(gate: _CheckArrivalGate, config_file: str):
    """只对 app/config.py 的 model_post_init 挂 LINE 钩子的 trace 函数。"""

    def tracer(frame, event, arg):
        if event == "call":
            return tracer if frame.f_code.co_filename == config_file else None
        if event == "line" and frame.f_code.co_name == "model_post_init":
            if frame.f_lineno == gate.check_line:
                gate.note_checked()
            elif frame.f_lineno == gate.set_line:
                gate.wait_all_checked()
        return tracer

    return tracer


class TestPgBackendStartupWarning:

    def test_http_startup_path_warns_for_postgresql(self, monkeypatch, caplog):
        """HTTP 入口共享启动点（app/main.py 导入链触发 Settings 构造）产生 warning。"""
        with caplog.at_level(logging.WARNING, logger="app.config"):
            _fresh_pg_settings(monkeypatch)
        records = _pg_records(caplog)
        assert len(records) == 1
        assert records[0].levelno == logging.WARNING
        assert records[0].name == "app.config"
        assert _PG_MARKER in records[0].getMessage()

    def test_stdio_startup_path_warning_lands_on_stderr_not_stdout(self, monkeypatch, capsys):
        """stdio 入口：生产 stdio 日志基建（_configure_stdio_logging）就位时，
        告警只能出现在 stderr，stdout 协议流保持纯净。"""
        from app.mcp.transports.stdio import _configure_stdio_logging

        _configure_stdio_logging()
        _fresh_pg_settings(monkeypatch)
        captured = capsys.readouterr()
        assert _WARNING_MARKER in captured.err
        assert _WARNING_MARKER not in captured.out

    def test_warning_emitted_only_once_per_process(self, monkeypatch, caplog):
        """同一进程重复触发启动逻辑（重复构造 Settings）只产生一次 warning。"""
        with caplog.at_level(logging.WARNING, logger="app.config"):
            _fresh_pg_settings(monkeypatch)
            Settings(storage_backend="postgresql")
            Settings(storage_backend="postgresql")
        assert len(_pg_records(caplog)) == 1

    def test_concurrent_construction_warns_exactly_once(self, monkeypatch, caplog):
        """P2 竞态回归：两线程并发构造 postgresql Settings 只允许一次告警。

        确定性交错（Barrier + settrace LINE 钩子，无 sleep、非压力测试）：
        置位行执行前等两线程都完成检查行求值（_CheckArrivalGate）。
        断言：两线程均完成、无异常、PG warning 恰好 1 条、文案与 WARNING
        级别不变。
        """
        _reset_pg_warning_flag(monkeypatch)
        check_line, set_line = _config_marker_lines()
        gate = _CheckArrivalGate(check_line, set_line)
        results: list[str] = []
        errors: list[BaseException] = []
        ready = threading.Barrier(2)

        def worker() -> None:
            try:
                ready.wait()
                Settings(storage_backend="postgresql")
                results.append("ok")
            except BaseException as exc:  # pragma: no cover - 防御线程内异常逃逸
                errors.append(exc)

        config_file = app_config.__file__
        threading.settrace(_line_gate_tracer(gate, config_file))
        try:
            with caplog.at_level(logging.WARNING, logger="app.config"):
                threads = [
                    threading.Thread(target=worker, name=f"pg1-race-{i}")
                    for i in range(2)
                ]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(timeout=15)
        finally:
            threading.settrace(None)

        # 1) 两个线程均完成；2) 无异常
        assert not any(t.is_alive() for t in threads), "worker 线程未在超时内完成"
        assert errors == []
        assert len(results) == 2
        # 3) PG warning 总数恰好 1（旧实现下 gate 强制交错产生 2 条 → 红灯）
        records = _pg_records(caplog)
        assert len(records) == 1, (
            f"expected exactly 1 PG warning, got {len(records)}: "
            f"{[r.getMessage()[:40] for r in records]}"
        )
        # 4) 文案与级别保持不变
        assert records[0].levelno == logging.WARNING
        assert records[0].getMessage() == _EXPECTED_WARNING

    def test_memory_backend_emits_no_pg_warning(self, monkeypatch, caplog):
        """默认 memory 后端静默：不产生任何 PG 告警。"""
        _reset_pg_warning_flag(monkeypatch)
        with caplog.at_level(logging.WARNING, logger="app.config"):
            Settings(storage_backend="memory")
        assert _pg_records(caplog) == []

    def test_invalid_backend_silent_and_still_fail_fast(self, monkeypatch, caplog):
        """非法 backend 不触发 PG 告警，且仍由 factory 校验 fail-fast（不被告警吞掉）。"""
        _reset_pg_warning_flag(monkeypatch)
        with caplog.at_level(logging.WARNING, logger="app.config"):
            Settings(storage_backend="postgrsql")  # 拼写错误
        assert _pg_records(caplog) == []

        monkeypatch.setattr(app_config.settings, "storage_backend", "postgrsql")
        from app.runtime.core.storage import factory

        with pytest.raises(ValueError, match="Invalid STORAGE_BACKEND"):
            factory._validate_backend()

    def test_warning_contains_no_credentials(self, monkeypatch, caplog):
        """告警文案不包含密码 / API key / 连接串凭据。"""
        with caplog.at_level(logging.WARNING, logger="app.config"):
            _fresh_pg_settings(
                monkeypatch,
                pg_password="super-secret-pw",
                api_key="sk-test-secret",
                redis_url="redis://:redis-pass@localhost:6379/0",
            )
        records = _pg_records(caplog)
        assert len(records) == 1
        message = records[0].getMessage()
        assert "super-secret-pw" not in message
        assert "sk-test-secret" not in message
        assert "redis-pass" not in message

    def test_warning_does_not_downgrade_backend(self, monkeypatch):
        """告警只观察不改写：storage_backend 保持 postgresql，不自动降级 memory。"""
        settings_obj = _fresh_pg_settings(monkeypatch)
        assert settings_obj.storage_backend == "postgresql"
