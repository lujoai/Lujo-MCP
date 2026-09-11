"""B05: 统一 KB bootstrap 测试。

验证：
1. 纯 stdio 模式（--no-http）启动时，必须执行 KB 回灌与种子加载；
2. 统一模式（HTTP lifespan）启动时，必须执行 KB 回灌与种子加载；
3. 单进程内重复调用 bootstrap 是幂等的，只初始化一次。
"""
from pathlib import Path
import pytest

from app.config import settings
import app.rag.knowledge_base as kb_module
from app.rag.knowledge_base import get_knowledge_entry, clear_knowledge_base
from app.runtime.core.storage.sqlite_kb_store import SQLiteKnowledgeBaseStore


@pytest.fixture
def sqlite_with_preset(tmp_path, monkeypatch):
    """预置包含历史经验的 SQLite 数据库。"""
    db_path = str(tmp_path / "bootstrap_test.sqlite3")
    store = SQLiteKnowledgeBaseStore(db_path=db_path)
    monkeypatch.setattr(settings, "kb_persist_enabled", True)
    monkeypatch.setattr(kb_module, "get_knowledge_store", lambda: store)

    # 先清理全局内存与持久层，并重置 bootstrap 标志
    clear_knowledge_base()
    kb_module._reset_bootstrap_state()

    # 清理之后再写入预置经验
    store.upsert_kb_entry({
        "fingerprint": "fp-preset-b05",
        "analysis": {"exception_type": "ValueError", "message": "preset error"},
        "fix_suggestion": "preset fix",
        "source": "llm",
        "verify_count": 5,
        "case_confidence": 0.95,
        "created_at": 1000.0,
        "updated_at": 1000.0,
    })

    return store


@pytest.mark.asyncio
async def test_stdio_mode_bootstraps_knowledge_base(sqlite_with_preset, monkeypatch):
    """B05 红测试：纯 stdio 模式启动前必须执行 KB 回灌，使既有经验可被查询。"""
    import app.mcp_server as mcp_server

    # 替换底层阻塞的 stdio transport 循环为立即返回
    called = False

    async def fake_run_stdio():
        nonlocal called
        called = True
        # 此时 stdio transport 已就绪，检查知识库中是否已有预置经验
        entry = get_knowledge_entry("fp-preset-b05")
        assert entry is not None
        assert entry["verify_count"] == 5

    monkeypatch.setattr(mcp_server, "_run_stdio_transport", fake_run_stdio)
    monkeypatch.setattr(mcp_server, "_register_signal_handlers", lambda: None)
    monkeypatch.setattr(mcp_server, "cleanup_resources", lambda: None)

    # 启动 stdio-only
    await mcp_server.main(["--no-http"])

    assert called is True
    assert get_knowledge_entry("fp-preset-b05") is not None


def test_unified_mode_bootstraps_knowledge_base(sqlite_with_preset):
    """B05: 统一模式（FastAPI lifespan）启动入口调用的 bootstrap 执行后能命中既有经验。"""
    from app.rag.knowledge_base import bootstrap_knowledge_base

    res = bootstrap_knowledge_base()
    assert res["persisted"] >= 1

    entry = get_knowledge_entry("fp-preset-b05")
    assert entry is not None
    assert entry["verify_count"] == 5


def test_bootstrap_idempotency(sqlite_with_preset):
    """B05: bootstrap_knowledge_base 在同一进程内只初始化一次。"""
    from app.rag.knowledge_base import bootstrap_knowledge_base

    r1 = bootstrap_knowledge_base()
    assert r1["persisted"] >= 1

    # 第二次调用必须跳过
    r2 = bootstrap_knowledge_base()
    assert r2 == {"persisted": 0, "seed": 0}
