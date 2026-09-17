"""AD-1（方案 B）：app/rag 不得依赖 app.runtime —— 架构边界与注入契约测试。

Architecture Frozen 第 2 条：`app/rag/` 不得依赖 agent / runtime / llm / mcp。
修复方式为 Composition Root 注入：上层启动入口（HTTP lifespan / stdio / 统一
模式）从 Runtime Storage Factory 取得 Knowledge 持久化 Store，注入 RAG
KnowledgeBase。本文件锁定：

1. rag 源码与真实 import 图中不再出现 app.runtime（旧实现的
   ``knowledge_base.py`` 直接 import factory，必须先红）；
2. 注入的 Store 被 upsert / verification / clear / load 四路共同使用；
3. 未注入时保持纯内存行为（不构造任何真实存储）；
4. 生产装配点真实传入 Factory 返回的 Store（源码契约，防止装配遗漏）；
5. bootstrap 注入进入进程 singleton、幂等，且不跨测试残留。
"""
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import app.rag.knowledge_base as kb_module
from app.rag.knowledge_base import KnowledgeBaseStore
from tests.unit.test_kb_persistence import FakeKnowledgeBaseStore

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RAG_DIR = _REPO_ROOT / "app" / "rag"


def _minimal_row(fp: str) -> dict:
    return {
        "fingerprint": fp,
        "analysis": {"exception_type": "E", "message": "m"},
        "fix_suggestion": "f",
        "source": "llm",
        "created_at": 1.0,
        "updated_at": 2.0,
        "normalized_fingerprint": "",
        "type_fingerprint": "",
        "verify_count": 0,
        "case_confidence": 0.0,
    }


def _upsert(store: KnowledgeBaseStore, fp: str, source: str = "llm") -> dict:
    return store.upsert(
        fingerprint=fp,
        analysis={"exception_type": "E", "message": f"m {fp}"},
        fix_suggestion=f"fix {fp}",
        source=source,
    )


@pytest.fixture
def _clean_global_kb():
    """清空全局 KB singleton 与 bootstrap 状态，用例结束后恢复干净。"""
    kb_module.clear_knowledge_base()
    kb_module._reset_bootstrap_state()
    yield
    kb_module.clear_knowledge_base()
    kb_module._reset_bootstrap_state()


# ---------------------------------------------------------------------------
# 1. rag 层边界：源码与 import 图零 runtime
# ---------------------------------------------------------------------------


class TestRagLayerBoundary:
    def test_rag_source_has_no_runtime_import(self):
        """app/rag 全部源码不得出现 app.runtime 的 import 语句。"""
        offenders: list[str] = []
        for py in sorted(_RAG_DIR.glob("*.py")):
            for lineno, line in enumerate(
                py.read_text(encoding="utf-8").splitlines(), 1
            ):
                stripped = line.strip()
                if stripped.startswith(("#", '"')):
                    continue
                if "app.runtime" in line and "import" in line:
                    offenders.append(f"{py.name}:{lineno}: {stripped}")
        assert offenders == [], f"app/rag 仍存在 runtime import（AD-1 未修复）: {offenders}"

    def test_importing_rag_knowledge_base_does_not_load_runtime(self):
        """干净子进程仅 import app.rag.knowledge_base，sys.modules 不得出现 app.runtime。"""
        code = (
            "import sys; import app.rag.knowledge_base; "
            "mods = [m for m in sys.modules if m.startswith('app.runtime')]; "
            "print('RUNTIME_MODULES=' + ','.join(mods))"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            cwd=str(_REPO_ROOT),
            timeout=60,
        )
        assert proc.returncode == 0, f"子进程 import 失败: {proc.stderr[-500:]}"
        marker = [ln for ln in proc.stdout.splitlines() if ln.startswith("RUNTIME_MODULES=")]
        assert marker, f"未捕获子进程输出: stdout={proc.stdout[-200:]}"
        assert marker[0] == "RUNTIME_MODULES=", (
            f"import app.rag.knowledge_base 传导加载了 runtime 模块: {marker[0]}"
        )


# ---------------------------------------------------------------------------
# 2. 注入 Store：四路持久化路径使用同一注入对象
# ---------------------------------------------------------------------------


class TestInjectedStore:
    def test_all_persist_paths_use_same_injected_store(self):
        fake = FakeKnowledgeBaseStore()
        store = KnowledgeBaseStore(max_entries=10, persist_store=fake)

        _upsert(store, "fp-a")
        assert [e["fingerprint"] for e in fake.upsert_calls] == ["fp-a"]

        store.record_verification("fp-a", 0.9)
        assert len(fake.verification_calls) == 1

        assert store.clear() is True
        assert fake.delete_all_calls == 1

        fake.rows["fp-b"] = _minimal_row("fp-b")
        assert store.load_from_persistent() == 1
        assert fake.list_calls == 1
        assert store.get("fp-b") is not None

    def test_without_injection_stays_memory_only(self):
        """未注入持久层：纯内存行为，upsert/clear/load 不触碰任何真实存储。"""
        store = KnowledgeBaseStore(max_entries=5)

        entry = _upsert(store, "fp-mem")
        assert entry["fingerprint"] == "fp-mem"
        assert store.get("fp-mem") is not None

        assert store.clear() is True
        assert store.size() == 0

        assert store.load_from_persistent() == 0


# ---------------------------------------------------------------------------
# 3. 生产装配点：Composition Root 必须 Factory 取 Store 并注入
# ---------------------------------------------------------------------------


class TestCompositionRootWiring:
    def test_http_lifespan_passes_factory_store(self):
        """app/main.py lifespan 的 bootstrap 调用必须传入 factory 返回的 Store。"""
        src = (_REPO_ROOT / "app" / "main.py").read_text(encoding="utf-8")
        assert "bootstrap_knowledge_base(persist_store=get_knowledge_store())" in src, (
            "HTTP lifespan 装配未注入 Factory Store（AD-1 方案 B 装配缺失）"
        )

    def test_stdio_and_unified_pass_factory_store(self):
        """app/mcp_server.py 的 stdio 与统一模式两处 bootstrap 均必须注入。"""
        src = (_REPO_ROOT / "app" / "mcp_server.py").read_text(encoding="utf-8")
        assert src.count("bootstrap_knowledge_base(persist_store=") >= 2, (
            "stdio/统一模式装配点数量不足（每一处 bootstrap 入口都必须注入）"
        )

    def test_stdio_factory_fetch_stays_outside_bootstrap_swallow(self):
        """stdio eager gate 语义保持：main() 中 Factory 取 Store 必须位于裸
        except 容错块之外（gate 与 bootstrap 之间），后端拒绝仍 fail-closed。
        """
        import app.mcp_server as mcp_server

        lines = __import__("inspect").getsource(mcp_server.main).splitlines()

        def _idx(exact: str) -> int:
            for i, ln in enumerate(lines):
                if ln.strip() == exact:
                    return i
            pytest.fail(f"mcp_server.main 结构缺失：{exact}")

        gate_idx = _idx("_validate_backend()")
        fetch_idx = _idx("kb_persist_store = get_knowledge_store()")
        bootstrap_idx = next(
            i for i, ln in enumerate(lines)
            if ln.strip().startswith("bootstrap_knowledge_base(")
        )
        assert gate_idx < fetch_idx < bootstrap_idx
        # factory 取 Store 与 gate 之间不得出现容错块（拒绝不可被吞）
        wrappers = [
            ln.strip()
            for ln in lines[gate_idx + 1:fetch_idx]
            if ln.strip().startswith(("try:", "except", "finally:"))
        ]
        assert wrappers == [], f"Factory 取 Store 被容错块包住，拒绝会被吞掉: {wrappers}"


# ---------------------------------------------------------------------------
# 4. bootstrap 注入语义：singleton + 幂等 + 测试隔离
# ---------------------------------------------------------------------------


class TestBootstrapInjection:
    def test_bootstrap_injects_into_singleton_and_is_idempotent(self, _clean_global_kb):
        fake = FakeKnowledgeBaseStore()
        fake.rows["fp-boot"] = _minimal_row("fp-boot")

        r1 = kb_module.bootstrap_knowledge_base(persist_store=fake)
        assert r1["persisted"] == 1
        assert kb_module.get_knowledge_entry("fp-boot") is not None

        # singleton 的写穿走注入对象
        kb_module.upsert_knowledge_entry(
            fingerprint="fp-x",
            analysis={"exception_type": "E", "message": "m"},
            fix_suggestion="f",
            source="llm",
        )
        assert any(e["fingerprint"] == "fp-x" for e in fake.upsert_calls)

        # 幂等：第二次调用（即使传了不同 Store）必须整体跳过，不重复 load/seed
        fake.list_calls = 0
        size_before = kb_module.get_knowledge_base().size()
        r2 = kb_module.bootstrap_knowledge_base(persist_store=FakeKnowledgeBaseStore())
        assert r2 == {"persisted": 0, "seed": 0}
        assert fake.list_calls == 0
        assert kb_module.get_knowledge_base().size() == size_before

    def test_reset_bootstrap_state_clears_injected_store(self, _clean_global_kb):
        """_reset_bootstrap_state 必须同时卸载注入的 Store，避免跨测试残留。"""
        fake = FakeKnowledgeBaseStore()
        kb_module.bootstrap_knowledge_base(persist_store=fake)
        assert kb_module.get_knowledge_base()._persist_store is fake

        kb_module._reset_bootstrap_state()

        assert kb_module.get_knowledge_base()._persist_store is None
        kb_module.upsert_knowledge_entry(
            fingerprint="fp-after-reset",
            analysis={"exception_type": "E", "message": "m"},
            fix_suggestion="f",
            source="llm",
        )
        assert all(
            e["fingerprint"] != "fp-after-reset" for e in fake.upsert_calls
        ), "reset 后不得继续写入上一次注入的 Store"

    def test_bootstrap_without_store_keeps_memory_only(self, _clean_global_kb):
        """无注入调用 bootstrap：不连接持久层，仅种子加载，行为与旧 NoOp 一致。"""
        r = kb_module.bootstrap_knowledge_base()
        assert r["persisted"] == 0
        assert kb_module.get_knowledge_base()._persist_store is None


# ---------------------------------------------------------------------------
# 5. 并发安全：注入不破坏既有 generation 栅栏语义（冒烟）
# ---------------------------------------------------------------------------


def test_injected_store_survives_concurrent_upserts():
    """注入 Store 下并发 upsert 不抛异常且全部落库（generation 栅栏仍有效）。"""
    fake = FakeKnowledgeBaseStore()
    store = KnowledgeBaseStore(max_entries=100, persist_store=fake)
    errors: list[Exception] = []

    def worker(i: int) -> None:
        try:
            _upsert(store, f"fp-c{i}")
        except Exception as exc:  # pragma: no cover - 仅收集失败
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == []
    assert len(fake.upsert_calls) == 8
    assert store.size() == 8
