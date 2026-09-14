"""WP3（Step 3）STORAGE_BACKEND 闸门收口验收 —— postgresql 由「启动告警」转为「启动拒绝」。

规格来源：``docs/internal/DESIGN_STEP3_PG_REMOVAL_20260913.md``

- §5.1 ``StorageBackendRemovedError`` 定义
- §5.2 为什么不继续用 ``ValueError``（R7-A2 先例；``ingest.py`` / ``debug.py`` 的
  ``except ValueError`` 会把服务端配置错误上报成 422 "Invalid request payload"）
- §5.3 匹配纪律（精确 ``postgresql`` → 新异常；大小写变体与其它非法值 → 通用 ``ValueError``）
- §5.4 四条启动路径的拒绝表现
- §5.5 stdio eager gate 必须早于 ``bootstrap_knowledge_base()``（其裸 ``except Exception``
  会把拒绝吞成一条 warning → 静默回退 memory）
- §5.6 必测的挂起风险（超时内真实退出 / 非零退出码 / stdout 零字节 / 错误只进 stderr）
- §6.4 WP3 必测清单 1–8

本文件同时承接原 ``tests/unit/test_config_pg_warning.py``（E 批 PG-1 启动告警）：
告警在 WP3 退役后被测行为不复存在，其 8 例已逐条迁移为拒绝测试（对照表见 WP3 报告）。

全部用例不连接真实 PostgreSQL：闸门在任何连接建立之前即拒绝（WP6 起 PG 配置
键本身已不被读取）。
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.config import Settings, settings
from app.runtime.core.storage import factory as factory_mod

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 红灯期（实现尚未落地）取不到该异常。用 getattr + _removed_error() 让每个用例各自
# 以可读断言失败，而不是整文件 ImportError——否则无法逐条取证「先红」。
_REMOVED_ERROR = getattr(factory_mod, "StorageBackendRemovedError", None)

_STORE_ATTRS = (
    "_trace_store",
    "_session_store",
    "_error_store",
    "_spec_store",
    "_knowledge_store",
)

_ALL_GETTERS = (
    "get_trace_store",
    "get_session_store",
    "get_error_store",
    "get_spec_store",
    "get_knowledge_store",
)

# 拒绝消息必须同时给出「已移除」定性与「往哪走」的迁移指引（§5.1 / 决策 4）。
_GUIDANCE_MARKERS = ("移除", "SQLite", "migrate_pg_kb_to_sqlite", "memory")

_SUBPROCESS_TIMEOUT = 60.0


def _removed_error():
    """返回 WP3 新增异常类型；未落地时以可读信息失败（而非 TypeError/ImportError）。"""
    if _REMOVED_ERROR is None:
        pytest.fail(
            "WP3 未落地：app.runtime.core.storage.factory.StorageBackendRemovedError 不存在"
        )
    return _REMOVED_ERROR


@pytest.fixture(autouse=True)
def _reset_factory_singletons():
    """factory 是模块级单例缓存：命中缓存时 getter 直接返回、不再校验后端。

    不重置会让「配 postgresql 却拿到上一个用例缓存的 memory store」变成假绿。
    """
    for attr in _STORE_ATTRS:
        setattr(factory_mod, attr, None)
    yield
    for attr in _STORE_ATTRS:
        setattr(factory_mod, attr, None)


# ── 1. 白名单收窄（§2 WP3：_VALID_BACKENDS → {"memory"}） ──────────────────


class TestBackendWhitelistNarrowed:
    def test_valid_backends_contains_only_memory(self):
        assert factory_mod._VALID_BACKENDS == {"memory"}


# ── 2. StorageBackendRemovedError 的类型契约（§5.1 / §5.2） ─────────────────


class TestRemovedErrorTypeContract:
    def test_subclasses_runtime_error(self):
        assert issubclass(_removed_error(), RuntimeError)

    def test_is_not_value_error(self):
        """决策 4 的核心：必须独立于 ValueError（R7-A2 先例）。

        ValueError 子类会被 ``ingest.py`` / ``debug.py`` 的 ``except ValueError``
        抢先截走，把服务端 misconfiguration 上报成调用方的 422。
        """
        assert not issubclass(_removed_error(), ValueError)

    def test_lives_in_factory_module(self):
        """放置位置遵循既有 factory 异常职责（与 _AsyncMixError 同模块同层）。"""
        assert _removed_error().__module__ == factory_mod.__name__


# ── 3. 精确匹配 postgresql → 拒绝（§5.3） ───────────────────────────────────


class TestExactPostgresqlRejected:
    def test_validate_backend_raises_removed_error(self, monkeypatch):
        monkeypatch.setattr(settings, "storage_backend", "postgresql")
        with pytest.raises(_removed_error()):
            factory_mod._validate_backend()

    def test_raised_instance_is_not_value_error(self, monkeypatch):
        """实例级复核：即使将来有人让它多继承 ValueError，本用例也会红。"""
        monkeypatch.setattr(settings, "storage_backend", "postgresql")
        with pytest.raises(RuntimeError) as exc_info:
            factory_mod._validate_backend()
        assert not isinstance(exc_info.value, ValueError)

    @pytest.mark.parametrize("getter", _ALL_GETTERS)
    def test_every_factory_entry_point_rejects(self, monkeypatch, getter):
        """所有经 factory 的入口都拒绝，不留旁路（架构冻结第 3 条）。"""
        monkeypatch.setattr(settings, "storage_backend", "postgresql")
        with pytest.raises(_removed_error()):
            getattr(factory_mod, getter)()

    def test_rejection_does_not_silently_build_memory_store(self, monkeypatch):
        """拒绝即拒绝：不得顺手 new 一个 memory store 顶上（DEV_PLAN S3-2 禁令）。"""
        import app.runtime.core.storage.memory_store as mem_mod
        import app.runtime.core.storage.noop_store as noop_mod

        monkeypatch.setattr(settings, "storage_backend", "postgresql")

        built: list[str] = []

        def _spy(name, original):
            def _ctor(*args, **kwargs):
                built.append(name)
                return original(*args, **kwargs)

            return _ctor

        for name in ("MemoryTraceStore", "MemorySessionStore"):
            monkeypatch.setattr(mem_mod, name, _spy(name, getattr(mem_mod, name)))
        for name in ("NoOpErrorStore", "NoOpSpecStore", "NoOpKnowledgeBaseStore"):
            monkeypatch.setattr(noop_mod, name, _spy(name, getattr(noop_mod, name)))

        for getter in ("get_trace_store", "get_session_store", "get_error_store",
                       "get_spec_store", "get_knowledge_store"):
            with pytest.raises(_removed_error()):
                getattr(factory_mod, getter)()

        assert built == [], f"拒绝路径仍构造了降级 store: {built}"

    def test_singletons_stay_none_after_rejection(self, monkeypatch):
        """单例不得被拒绝路径污染（下一次合法调用仍能干净初始化）。"""
        monkeypatch.setattr(settings, "storage_backend", "postgresql")
        for getter in ("get_trace_store", "get_session_store", "get_error_store",
                       "get_spec_store", "get_knowledge_store"):
            with pytest.raises(_removed_error()):
                getattr(factory_mod, getter)()
        for attr in _STORE_ATTRS:
            assert getattr(factory_mod, attr) is None, f"{attr} 被拒绝路径污染"

    def test_message_contains_removal_and_migration_guidance(self, monkeypatch):
        monkeypatch.setattr(settings, "storage_backend", "postgresql")
        with pytest.raises(_removed_error()) as exc_info:
            factory_mod._validate_backend()
        message = str(exc_info.value)
        for marker in _GUIDANCE_MARKERS:
            assert marker in message, f"拒绝消息缺少迁移指引片段 {marker!r}: {message}"

    def test_message_contains_no_credentials(self, monkeypatch):
        """对齐原 test_config_pg_warning.py:215 的既有手法：文案不得回显凭据。"""
        monkeypatch.setattr(settings, "storage_backend", "postgresql")
        monkeypatch.setattr(settings, "api_key", "sk-test-secret")
        monkeypatch.setattr(settings, "redis_url", "redis://:redis-pass@localhost:6379/0")
        with pytest.raises(_removed_error()) as exc_info:
            factory_mod._validate_backend()
        message = str(exc_info.value)
        assert "sk-test-secret" not in message
        assert "redis-pass" not in message

    def test_legacy_pg_settings_do_not_mask_guidance(self, monkeypatch, tmp_path):
        """WP6：PG 配置族已删除。.env 遗留的旧 PG_* 键经 extra="ignore" 被忽略
        （构造不崩、不产生同名属性），移除指引仍完整可见、不被遮蔽/截断。"""
        from app.config import Settings

        legacy_env = tmp_path / ".env"
        legacy_env.write_text(
            "STORAGE_BACKEND=postgresql\n"
            "PG_HOST=db.internal\n"
            "PG_PORT=5432\n"
            "PG_DATABASE=lujo\n"
            "PG_USER=lujo\n"
            "PG_PASSWORD=legacy-pw\n"
            "PG_ASYNC_ENABLED=true\n"
            "STORAGE_FALLBACK_TO_MEMORY=false\n"
            "POSTGRES_PASSWORD=legacy-pw\n"
            "DATABASE_URL=postgresql://legacy\n",
            encoding="utf-8",
        )
        obj = Settings(_env_file=str(legacy_env))
        for removed in ("pg_host", "pg_port", "pg_database", "pg_user", "pg_password",
                        "pg_async_enabled", "storage_fallback_to_memory"):
            assert not hasattr(obj, removed), f"{removed} 应在 WP6 被删除"

        monkeypatch.setattr(settings, "storage_backend", "postgresql")
        with pytest.raises(_removed_error()) as exc_info:
            factory_mod._validate_backend()
        message = str(exc_info.value)
        for marker in _GUIDANCE_MARKERS:
            assert marker in message, f"旧 PG_* 配置遮蔽了迁移指引 {marker!r}: {message}"
        assert "lujo" not in message and "db.internal" not in message


# ── 4. 其余非法值仍走通用 ValueError（§5.3 / A7–A10） ───────────────────────


class TestGenericInvalidBackendSemantics:
    def test_empty_backend_keeps_valueerror(self, monkeypatch):
        """空串按原有非法配置语义处理：仍是 ValueError，不是新异常。"""
        monkeypatch.setattr(settings, "storage_backend", "")
        with pytest.raises(ValueError) as exc_info:
            factory_mod._validate_backend()
        assert not isinstance(exc_info.value, _removed_error())
        message = str(exc_info.value)
        assert "Invalid STORAGE_BACKEND" in message
        assert "''" in message
        assert "memory" in message

    @pytest.mark.parametrize("variant", ["PostgreSQL", "POSTGRESQL", "Postgresql", "postgreSql"])
    def test_case_variants_keep_valueerror_with_removal_hint(self, monkeypatch, variant):
        """大小写变体不是精确匹配 → 通用 ValueError + case-sensitive 提示 + 移除提示。

        ``test_storage.py::test_case_sensitive_raises_valueerror`` 的
        ``"case-sensitive" in msg`` 断言因此逐字存活（§5.3 / A9）。
        """
        monkeypatch.setattr(settings, "storage_backend", variant)
        with pytest.raises(ValueError) as exc_info:
            factory_mod._validate_backend()
        assert not isinstance(exc_info.value, _removed_error())
        message = str(exc_info.value)
        assert "Invalid STORAGE_BACKEND" in message
        assert variant in message
        assert "case-sensitive" in message
        assert "移除" in message

    @pytest.mark.parametrize("bad", ["postgrsql", "postgres", "sqlite", "MEMORY"])
    def test_misspellings_keep_invalid_backend_message(self, monkeypatch, bad):
        """拼写错误仍 fail-fast，消息结构（非法值 !r / 有效值列表 / 提示）保持既有形状。"""
        monkeypatch.setattr(settings, "storage_backend", bad)
        with pytest.raises(ValueError) as exc_info:
            factory_mod._validate_backend()
        assert not isinstance(exc_info.value, _removed_error())
        message = str(exc_info.value)
        assert "Invalid STORAGE_BACKEND" in message
        assert bad in message
        assert "memory" in message
        assert "case-sensitive" in message or "spelling" in message

    def test_valid_value_list_no_longer_advertises_postgresql(self, monkeypatch):
        """A1：收窄后有效值列表只剩 memory，不得再把 postgresql 当合法选项广告出去。"""
        monkeypatch.setattr(settings, "storage_backend", "postgrsql")
        with pytest.raises(ValueError) as exc_info:
            factory_mod._validate_backend()
        message = str(exc_info.value)
        assert "['memory']" in message
        assert "Valid values" in message


# ── 5. memory 行为逐位不变（§6.4 必测 7） ───────────────────────────────────


class TestMemoryBackendUnchanged:
    def test_memory_dispatch_unchanged(self, monkeypatch):
        """默认后端分发一行不变：trace/session → Memory*，error/spec/kb → NoOp*。"""
        from app.runtime.core.storage.memory_store import (
            MemorySessionStore,
            MemoryTraceStore,
        )
        from app.runtime.core.storage.noop_store import (
            NoOpErrorStore,
            NoOpKnowledgeBaseStore,
            NoOpSpecStore,
        )

        monkeypatch.setattr(settings, "storage_backend", "memory")
        monkeypatch.setattr(settings, "kb_persist_enabled", False)

        assert isinstance(factory_mod.get_trace_store(), MemoryTraceStore)
        assert isinstance(factory_mod.get_session_store(), MemorySessionStore)
        assert isinstance(factory_mod.get_error_store(), NoOpErrorStore)
        assert isinstance(factory_mod.get_spec_store(), NoOpSpecStore)
        assert isinstance(factory_mod.get_knowledge_store(), NoOpKnowledgeBaseStore)

    def test_memory_validate_backend_is_silent(self, monkeypatch, caplog):
        """memory 静默路径保留：不产生任何 PG 相关告警/拒绝。"""
        monkeypatch.setattr(settings, "storage_backend", "memory")
        with caplog.at_level(logging.WARNING):
            factory_mod._validate_backend()
        assert caplog.records == []
        assert [r for r in caplog.records if "postgresql" in r.getMessage().lower()] == []

    def test_memory_kb_sqlite_fallback_semantics_preserved(self, monkeypatch, tmp_path):
        """get_knowledge_store() 自身的 SQLite 降级语义不受闸门收口影响。

        SQLite 分支是无条件 try/except、不依赖任何后端开关（原
        ``storage_fallback_to_memory`` 已随 WP6 删除）；本用例锁住这一点。
        """
        from app.runtime.core.storage.noop_store import NoOpKnowledgeBaseStore

        monkeypatch.setattr(settings, "storage_backend", "memory")
        monkeypatch.setattr(settings, "kb_persist_enabled", True)
        monkeypatch.setattr(settings, "kb_persist_path", str(tmp_path / "kb.sqlite3"))

        def _boom(*args, **kwargs):
            raise RuntimeError("sqlite unavailable")

        monkeypatch.setattr(
            "app.runtime.core.storage.sqlite_kb_store.SQLiteKnowledgeBaseStore", _boom
        )
        assert isinstance(factory_mod.get_knowledge_store(), NoOpKnowledgeBaseStore)


# ── 6. Settings 构造阶段不崩、不再告警（§6.4 A6 硬约束） ────────────────────


class TestSettingsConstruction:
    def test_postgresql_construction_does_not_raise(self):
        """拒绝点必须留在 factory 使用点：Settings 构造阶段抛出会让拒绝测试无法构造被测对象。"""
        obj = Settings(storage_backend="postgresql")
        assert obj.storage_backend == "postgresql"

    def test_construction_does_not_downgrade_to_memory(self):
        """原 test_warning_does_not_downgrade_backend 的存活部分：只观察不改写的契约不变。"""
        assert Settings(storage_backend="postgresql").storage_backend == "postgresql"
        assert Settings(storage_backend="memory").storage_backend == "memory"

    def test_construction_emits_no_pg_warning(self, monkeypatch, caplog):
        """PG-1 启动告警已在 WP3 退役：构造阶段不得再出现「实验性后端」告警。

        沿用原 test_config_pg_warning.py 的 hasattr 守卫重置一次性标志：标志可能
        已被导入链上游的 Settings 构造置位，不重置会让本用例在红灯期假绿。
        """
        import app.config as app_config

        if hasattr(app_config, "_pg_backend_warning_emitted"):
            monkeypatch.setattr(app_config, "_pg_backend_warning_emitted", False)
        with caplog.at_level(logging.WARNING, logger="app.config"):
            Settings(storage_backend="postgresql")
        assert [r for r in caplog.records if "实验性后端" in r.getMessage()] == []

    def test_pg_warning_guard_globals_retired(self):
        """告警的一次性守卫（标志 + 锁）随告警一并退役，不留死状态。"""
        import app.config as app_config

        assert not hasattr(app_config, "_pg_backend_warning_emitted")
        assert not hasattr(app_config, "_pg_backend_warning_lock")

    def test_extra_ignore_behavior_preserved(self):
        """配置兼容性：extra="ignore" 不变，.env 中未声明的旧 PG_* 键不得让构造崩。"""
        obj = Settings(
            storage_backend="postgresql",
            POSTGRES_PASSWORD="from-legacy-env",
            DATABASE_URL="postgresql://legacy",
        )
        assert obj.storage_backend == "postgresql"
        assert Settings.model_config.get("extra") == "ignore"

    def test_env_postgresql_does_not_break_settings_import(self, monkeypatch):
        """.env 里写着 STORAGE_BACKEND=postgresql 的既有部署，导入期不得直接崩。

        崩在导入期会让 pytest --collect-only 全红、也会让宿主拿到不可读的启动失败。
        """
        monkeypatch.setenv("STORAGE_BACKEND", "postgresql")
        obj = Settings()
        assert obj.storage_backend == "postgresql"


# ── 7. HTTP 启动路径 fail-fast（§5.4 / 必测 4） ─────────────────────────────


class TestHttpStartupRejection:
    def test_lifespan_fails_fast(self, monkeypatch):
        """main.py lifespan 内既有的 get_trace_store() fail-fast 现在抛新异常。"""
        from fastapi import FastAPI

        from app.main import lifespan

        monkeypatch.setattr(settings, "storage_backend", "postgresql")
        monkeypatch.setenv("STORAGE_BACKEND", "postgresql")
        monkeypatch.setenv("API_KEY", "")

        async def _scenario():
            async with lifespan(FastAPI()):
                pytest.fail("lifespan 不应在 STORAGE_BACKEND=postgresql 下启动成功")

        loop = asyncio.new_event_loop()
        try:
            with pytest.raises(_removed_error()):
                loop.run_until_complete(_scenario())
        finally:
            loop.close()

    def test_server_config_error_is_not_reported_as_invalid_payload(self, monkeypatch):
        """§5.2：服务端 misconfiguration 不得被 ``except ValueError`` 转成 422。

        ``ingest.py`` 的 ``except ValueError → 422 "Invalid request payload"`` 会把
        责任推给调用方；RuntimeError 子类落到 ``except Exception`` 分支。
        """
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.api.ingest import router

        monkeypatch.setattr(settings, "storage_backend", "postgresql")
        monkeypatch.setattr(settings, "rbac_enabled", False)

        app = FastAPI()
        app.include_router(router, prefix="/api")
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.post("/api/ingest/network", json={"record": {"url": "https://example.com"}})

        assert resp.status_code != 422, resp.text
        assert resp.json().get("detail") != "Invalid request payload"

    def test_dashboard_entry_fails_fast_instead_of_degrading(self, monkeypatch):
        """dashboard 的 errors/history 不得把拒绝吞成「degraded + 空列表」。"""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.api.dashboard import router

        monkeypatch.setattr(settings, "storage_backend", "postgresql")

        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)
        # raise_server_exceptions 默认 True：拒绝必须穿透 handler 冒到调用方，
        # 而不是被吞成 200 + degraded=True + 空列表（那正是 factory 注释里点名
        # 要防的「查询降级、恒返回空列表」）。
        with pytest.raises(_removed_error()):
            client.get("/api/dashboard/errors/history?limit=5")


# ── 8. stdio eager gate（§5.5 / §5.6 / 必测 5、6） ──────────────────────────


class TestStdioEagerGate:
    def test_gate_is_first_statement_of_outer_try(self):
        """结构断言：gate 必须是 main() 外层 try 的第一条语句，且不被任何 try/except 包住。

        ``bootstrap_knowledge_base()`` 外有裸 ``except Exception``（mcp_server.py:615），
        gate 若落在其作用域内，拒绝会被吞成 warning → 进程照常启动并静默回退 memory。
        """
        import app.mcp_server as mcp_server

        lines = inspect.getsource(mcp_server.main).splitlines()

        def _find(pred, what):
            for i, ln in enumerate(lines):
                if pred(ln):
                    return i
            pytest.fail(f"WP3 stdio eager gate 结构缺失：{what}")

        try_idx = _find(lambda ln: ln.strip() == "try:", "main() 外层 try")
        gate_idx = _find(lambda ln: ln.strip() == "_validate_backend()", "gate 调用")
        # 整行精确匹配：gate 的说明注释里也出现了 bootstrap_knowledge_base() 字样，
        # 用子串匹配会把注释行误判成调用点。
        bootstrap_idx = _find(
            lambda ln: ln.strip() == "bootstrap_knowledge_base()",
            "bootstrap_knowledge_base() 调用",
        )

        assert try_idx < gate_idx < bootstrap_idx, (
            f"gate 必须位于外层 try 之后、bootstrap 之前："
            f"try={try_idx} gate={gate_idx} bootstrap={bootstrap_idx}"
        )
        wrappers = [
            ln.strip()
            for ln in lines[try_idx + 1:gate_idx]
            if ln.strip().startswith(("try:", "except", "finally:"))
        ]
        assert wrappers == [], f"gate 被容错块包住，拒绝会被吞掉: {wrappers}"
        assert any(
            ln.strip() == "cleanup_resources()" for ln in lines[bootstrap_idx:]
        ), "finally cleanup_resources() 必须保留"

    def test_gate_does_not_bypass_exit_supervisor(self):
        """gate 落位不得改变 ensure_exit_supervisor() 的既有生命周期语义（在 gate 之前）。"""
        import app.mcp_server as mcp_server

        lines = inspect.getsource(mcp_server.main).splitlines()
        # 行尾匹配：gate 的说明注释里同样提到 ensure_exit_supervisor()。
        supervisor_idx = next(
            (i for i, ln in enumerate(lines) if ln.strip().endswith("ensure_exit_supervisor()")),
            None,
        )
        gate_idx = next(
            (i for i, ln in enumerate(lines) if ln.strip() == "_validate_backend()"), None
        )
        assert supervisor_idx is not None and gate_idx is not None
        assert supervisor_idx < gate_idx, "gate 不得早于 ensure_exit_supervisor()"


def _subprocess_env(backend: str) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "STORAGE_BACKEND": backend,
            "KB_PERSIST_ENABLED": "false",
            "API_KEY": "",
            "HOST": "127.0.0.1",
            "OTEL_SDK_DISABLED": "true",
            "PYTHONUNBUFFERED": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            # WP6：PG 配置族已删除；遗留 PG_* env 键经 Settings extra="ignore"
            # 被忽略，闸门在任何连接之前拒绝，不存在触达真实库的路径。
        }
    )
    return env


def _run_mcp_server(args: list[str], backend: str) -> tuple[int, float, bytes, bytes]:
    """真实子进程启动 app.mcp_server；挂住即 pytest.fail（§5.6 第 1 条）。"""
    started = time.monotonic()
    proc = subprocess.Popen(
        [sys.executable, "-m", "app.mcp_server", *args],
        cwd=str(_PROJECT_ROOT),
        env=_subprocess_env(backend),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        stdout, stderr = proc.communicate(timeout=_SUBPROCESS_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        _, stderr = proc.communicate(timeout=15.0)
        tail = stderr.decode("utf-8", errors="replace")[-800:]
        pytest.fail(
            f"子进程在 {_SUBPROCESS_TIMEOUT}s 内未退出（挂起）：args={args} "
            f"backend={backend}\nstderr tail:\n{tail}"
        )
    return proc.returncode, time.monotonic() - started, stdout, stderr


@pytest.mark.parametrize(
    "args,label",
    [
        (["--no-http"], "pure_stdio"),
        (["--http", "--http-host", "127.0.0.1", "--http-port", "58931"], "unified"),
    ],
)
def test_stdio_subprocess_fails_fast_on_removed_backend(args, label):
    """§5.6 四条断言：超时内退出 / 非零退出码 / stdout 零字节 / 错误只进 stderr。"""
    code, elapsed, stdout, stderr = _run_mcp_server(args, "postgresql")
    text = stderr.decode("utf-8", errors="replace")

    assert code != 0, f"{label}: 期望非零退出码，实际 {code}（{elapsed:.1f}s）\nstderr:\n{text[-800:]}"
    assert stdout == b"", (
        f"{label}: stdout 必须零字节（AGENTS.md §4：stdout 保持纯 MCP 协议），"
        f"实际 {len(stdout)} 字节: {stdout[:200]!r}"
    )
    assert "StorageBackendRemovedError" in text, f"{label}: stderr 缺少异常类型\n{text[-800:]}"
    for marker in _GUIDANCE_MARKERS:
        assert marker in text, f"{label}: stderr 缺少迁移指引片段 {marker!r}"


def test_stdio_subprocess_memory_backend_still_starts():
    """对照组：memory 后端不得被 gate 误伤（stdin 立即 EOF → 干净退出 0）。"""
    code, elapsed, stdout, stderr = _run_mcp_server(["--no-http"], "memory")
    text = stderr.decode("utf-8", errors="replace")

    assert code == 0, f"memory 启动被 gate 误伤：exit={code}（{elapsed:.1f}s）\nstderr:\n{text[-800:]}"
    assert "StorageBackendRemovedError" not in text
    assert "Invalid STORAGE_BACKEND" not in text


# ── 9. 各工作包删除边界（WP4 接口 / WP5 模块与迁移入口 / WP6 依赖） ──────────


class TestStep3WorkpackageBoundaries:
    def test_wp4_get_error_store_async_removed(self):
        """WP4 已删除 get_error_store_async()：接口不再存在（I2 顺序）。"""
        assert not hasattr(factory_mod, "get_error_store_async"), \
            "get_error_store_async 应在 WP4 被删除"

    @pytest.mark.parametrize(
        "module",
        [
            "pg_executor", "async_pg_store", "pg_trace_store", "pg_session_store",
            "pg_error_store", "pg_spec_store", "pg_kb_store", "pg_partitions",
            "ddl", "_pg_errors",
        ],
    )
    def test_wp5_pg_implementation_modules_removed(self, module):
        """WP5（Step 3 Breaking #3）：§6.2 的 10 个 PG 模块已与迁移入口同批删除。"""
        path = _PROJECT_ROOT / "app" / "runtime" / "core" / "storage" / f"{module}.py"
        assert not path.exists(), f"{module}.py 应在 WP5 被删除"
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(f"app.runtime.core.storage.{module}")

    def test_wp5_migration_entry_and_cli_removed(self):
        """WP5：WP2 一次性迁移入口与 CLI 到期删除（设计文档 §4.3-G1 硬到期）。"""
        assert not hasattr(factory_mod, "migrate_knowledge_entries")
        assert not hasattr(factory_mod, "_MIGRATION_SOURCE_BACKENDS")
        assert not (_PROJECT_ROOT / "scripts" / "migrate_pg_kb_to_sqlite.py").exists()

    @pytest.mark.parametrize("filename", ["requirements.txt", "requirements-locked.txt"])
    def test_wp6_pg_dependencies_removed(self, filename):
        """WP6（Step 3 Breaking #4）：PG 驱动已从依赖声明删除；
        pybreaker 仍被 LLM 熔断使用，不得随 PG 误删（设计文档 §6.1 / R7）。"""
        path = _PROJECT_ROOT / filename
        text = path.read_text(encoding="utf-8")
        assert "psycopg2" not in text, f"{filename} 的 PG 驱动应在 WP6 被删除"
        assert "asyncpg" not in text, f"{filename} 的 asyncpg 应在 WP6 被删除"
        assert "pybreaker" in text, "pybreaker 仍被 LLM 熔断使用，不得随 PG 删除"
