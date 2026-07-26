# pg_store.py 技术债评估与重构方案

> **任务**：T8 — pg_store.py 技术债评估与方案准备（纯分析，不修改代码）
> **日期**：2026-07-25
> **状态**：待审批
> **范围**：`app/mcp/core/storage/pg_store.py` 及其所有引用方
> **遵守**：AI_RULES.md §三.6「PGStore 修改规则」——先输出问题分析/影响范围/测试方案，等待确认后再修改

---

## 0. 概述

本文档对 `app/mcp/core/storage/pg_store.py` 的技术债进行系统性评估，给出三个递进式重构方案（A/B/C）、N4-FU-3 安全泄露修复方案、重试一致性修复方案、测试方案、工作量估算与风险分析，供项目 owner 做审批决策。

**关键事实校准**（与任务描述的差异）：
- 任务描述称 pg_store.py "约 598 行"，**实际为 850 行**，技术债规模高于预估。
- 任务描述称 N4-FU-3 位于"第 59 行"，**实际位于第 90 行**（`raise RuntimeError(f"无法连接 PostgreSQL: {e}")`），行号因代码增补而偏移。
- `async_pg_store.py` 第 169 行存在**相同的 N4-FU-3 泄露**（`raise RuntimeError(f"无法连接 PostgreSQL (asyncpg): {e}")`），修复需同步覆盖。

---

## 1. 问题分析

### 1.1 文件规模与职责混杂

**现状**：`pg_store.py` 共 850 行，承担 5 类职责：

| 职责 | 行范围 | 说明 |
|------|--------|------|
| 全局连接池 + 熔断器 | 1–105 | `_get_pool` / `close_pool` / `_get_pg_circuit_breaker` |
| 建表 DDL + 分区/归档 | 107–341 | `DDL_*` 常量、`_ensure_init`、`_create_partition_for_month`、`_archive_old_traces` |
| SQL 执行基础设施 | 344–438 | `_execute_with_retry`（写）、`_query_with_retry`（读） |
| Trace 存储（类） | 441–565 | `PGTraceStore(TraceStorage)` |
| Session 存储（类） | 568–670 | `PGSessionStore(SessionStorage)` |
| errors 表 CRUD（裸函数） | 673–732 | `upsert_error`（模块级函数，非类） |
| specs 表 CRUD（裸函数） | 735–850 | `save_spec` / `get_spec` / `list_specs_pg` / `delete_spec`（模块级函数） |

**问题**：
- 单文件混合了「基础设施（连接池/熔断器）」「DDL/分区/归档」「4 种业务存储」，违反单一职责原则。
- Trace/Session 用类实现（符合 ABC），errors/specs 用模块级裸函数实现（无 ABC，风格不一致）。
- 850 行已超过单文件可维护性阈值（通常建议 < 500 行），修改任一职责都需在长文件中定位，PR review 成本高。

### 1.2 ABC 缺失（设计债）

**现状**：`app/mcp/core/storage/base.py` 仅定义两个 ABC：

```
class TraceStorage(ABC):     # save_entry / get_entries / delete / cleanup_expired / list_request_ids
class SessionStorage(ABC):   # save / get / delete / list_active / cleanup_expired
```

**缺失**：
- **无 `ErrorStorage` ABC**：`upsert_error` 是裸函数，没有抽象接口。`errors.py` 直接 `from pg_store import upsert_error`，无法被 mock/替换/换后端。
- **无 `SpecStorage` ABC**：`save_spec` / `get_spec` / `list_specs_pg` / `delete_spec` 是裸函数。`spec_store.py` 直接 import 这 4 个函数。
- `factory.py` 只有 `get_trace_store()` / `get_session_store()`，**没有** `get_error_store()` / `get_spec_store()`。

**后果**：
- errors/specs 的 PG 实现与调用方硬耦合，无法像 Trace/Session 那样通过 factory 切换后端或降级到 memory。
- 测试中无法用 ABC 替换 PG 实现，只能 mock 模块级函数（脆弱）。
- 与 Trace/Session 的架构一致性断裂——同样的存储模式，一半用 ABC + factory，一半用裸函数 + 直接 import。

### 1.3 N4-FU-3：RuntimeError 泄露 PG 连接参数

**位置**：`pg_store.py:90`（任务描述中的"第 59 行"为历史行号，已偏移）

```python
# pg_store.py:88-90
except psycopg2.OperationalError as e:
    logger.critical(f"PostgreSQL 连接失败: {e}")
    raise RuntimeError(f"无法连接 PostgreSQL: {e}")   # ← N4-FU-3：{e} 含 PG 连接参数细节
```

**泄露内容**：`psycopg2.OperationalError` 的字符串表示包含连接上下文，可能包括 host、port、dbname、user，甚至密码（取决于 psycopg2 版本和错误类型）。该 RuntimeError 在启动期向上抛出，可能进入：
- FastAPI 启动日志（stdout/stderr）
- 容器编排系统的日志收集管道
- 前端错误展示（若启动失败被上层捕获并返回客户端）

**同类泄露点**：`async_pg_store.py:169` 存在完全相同的问题：
```python
raise RuntimeError(f"无法连接 PostgreSQL (asyncpg): {e}")
```

**历史**：根据 `AI_HANDOFF.md`，N4-FU-1/2 已于 2026-07-20 修复，N4-FU-3 因 pg_store.py 修改需审批而保留。本评估即对该 follow-up 的正式响应。

### 1.4 重试路径不一致

**现状**：pg_store.py 有两个执行 helper，读写路径的重连重试行为不一致。

| Helper | 路径 | OperationalError 处理 | 重试? | 重连? |
|--------|------|----------------------|-------|-------|
| `_execute_with_retry` (L345) | 写/DDL | rollback → `putconn(close=True)` → `getconn()` 新连接 → sleep 0.1s → 重试 | ✅ max_retries=2 | ✅ |
| `_query_with_retry` (L408) | 读/SELECT | `putconn(close=True)` → **直接 raise** | ❌ 无 | 部分（关坏连接但不在新连接上重试） |

**问题**：
- 读路径遇到连接断开（如 PG 重启、网络瞬断、连接被服务端关闭）时，**不会在新连接上重试**，直接抛给上层。
- 而写路径会在新连接上重试 2 次。
- 这意味着一次 PG 主备切换期间，所有 SELECT 调用（`get_entries` / `get` / `list_active` / `list_request_ids` / `get_spec` / `list_specs_pg`）都会失败，而 INSERT/UPDATE/DELETE 可能成功。
- 不一致行为让上层调用方难以编写统一的事务/错误处理逻辑。

**额外差异**：
- `_execute_with_retry` 有 `time.sleep(0.1)` 退避；`_query_with_retry` 无退避。
- 两者都受熔断器保护（一致）。

### 1.5 其他发现

1. **errors.py 直接访问连接池**：`app/mcp/core/errors.py:368` 直接 `from pg_store import _get_pool, _ensure_init, _parse_data`，然后在 `query_pg_errors` 中手写 SQL 并直接操作 `pool.getconn()/putconn()`，**绕过了 `_query_with_retry`**，既无重试也无熔断器保护。

2. **async_pg_store.py 的 N4-FU-3**：第 169 行同款泄露，任何重构/修复需同步覆盖。

3. **DDL 重复**：`pg_store.py` 与 `async_pg_store.py` 各自维护一份完全相同的 `DDL_TRACES` / `DDL_SESSIONS` / `DDL_ERRORS` / `DDL_SPECS` / `DDL_TRACES_ARCHIVE` 常量和 `_month_partition_name` / `_month_range_epoch` 函数，存在双源同步风险（已由 `test_storage.py` 的 `test_month_*_async_consistent` 测试守护，但 DDL 字符串本身无一致性测试）。

4. **errors/specs 无内存后端**：Trace/Session 在 memory backend 下有 `MemoryTraceStore` / `MemorySessionStore`，但 errors/specs 只有 PG 实现，非 PG 后端时 spec_store 回退到扫描 trace_store（N+1 路径），errors 则完全无持久化。

---

## 2. 影响范围

### 2.1 生产代码引用方（5 个文件）

| 文件 | 引用符号 | 引用方式 | 改动影响 |
|------|----------|----------|----------|
| `app/mcp/core/storage/factory.py` | `PGTraceStore`, `PGSessionStore` | 延迟 import | 类路径若变需改 import；若保留 `pg_store.py` 作为 re-export facade 则无需改 |
| `app/mcp/core/errors.py` | `upsert_error`, `_get_pool`, `_ensure_init`, `_parse_data` | 延迟 import（函数内） | 拆分后需改 import 路径；`_get_pool` 等基础设施若留在 `pg_store.py` 或提取到 `pg_pool.py` 则路径变 |
| `app/mcp/verifier/spec_store.py` | `list_specs_pg`, `save_spec`, `get_spec`, `delete_spec` | 延迟 import（函数内，5 处） | 拆分后需改 import 路径；若改用 `SpecStorage` ABC + factory 则需重构调用方式 |
| `app/main.py` | `close_pool` (L81), `_get_pool` (L222) | 延迟 import | `close_pool` 是关闭钩子；`_get_pool` 用于健康检查。基础设施提取后路径变 |
| `app/mcp_server.py` | `close_pool` (L81) | 延迟 import | 同上 |

### 2.2 测试/脚本引用方（6 个文件）

| 文件 | 引用符号 |
|------|----------|
| `tests/unit/test_storage.py` | `PGTraceStore`, `PGSessionStore`, `_month_partition_name`, `_month_range_epoch`（7 处 import） |
| `tests/unit/test_circuit_breaker.py` | `_execute_with_retry`, `_query_with_retry`, `_get_pg_circuit_breaker`，模块级 `pg_store`（4 处） |
| `tests/integration/test_pg_integration.py` | `_get_pool`, `_parse_data` |
| `tests/integration/test_process_boundary.py` | `_get_pool`，模块级 `pg_store`（3 处） |
| `tests/integration/test_runtime_enablement.py` | `_get_pg_circuit_breaker` |
| `test_full_flow.py` | `_get_pool`, `close_pool` |

### 2.3 引用符号分类（决定拆分后是否需要 re-export facade）

| 分类 | 符号 | 引用方数 |
|------|------|----------|
| **基础设施**（连接池/熔断/DDL/分区/归档） | `_get_pool`, `close_pool`, `_ensure_init`, `_parse_data`, `_get_pg_circuit_breaker`, `_execute_with_retry`, `_query_with_retry`, `DDL_*`, `_month_*`, `_ensure_partitions`, `_archive_old_traces` | 6+ |
| **Trace 存储** | `PGTraceStore` | 2 |
| **Session 存储** | `PGSessionStore` | 2 |
| **errors 存储** | `upsert_error` | 1（errors.py） |
| **specs 存储** | `save_spec`, `get_spec`, `list_specs_pg`, `delete_spec` | 1（spec_store.py） |

> **关键结论**：`_parse_data` 被 errors.py 和 test_pg_integration.py 直接 import，是跨模块共享的工具函数，拆分时应放入公共位置（如 `pg_schema.py` 或 `pg_utils.py`）而非 trace/error 任一子模块。

---

## 3. 方案对比

### 方案 A：仅提 DDL 到 pg_schema.py（零风险试水）

**内容**：
- 新建 `app/mcp/core/storage/pg_schema.py`，将 `DDL_TRACES` / `DDL_SESSIONS` / `DDL_ERRORS` / `DDL_SPECS` / `DDL_TRACES_ARCHIVE` 常量、`_month_partition_name` / `_month_range_epoch` 函数移入。
- `pg_store.py` 与 `async_pg_store.py` 改为 `from app.mcp.core.storage.pg_schema import *`，**保留原符号 re-export** 以保证向后兼容。
- 不改任何调用方、不改 ABC、不拆类。

**改动文件**：
- 新增：`app/mcp/core/storage/pg_schema.py`
- 修改：`pg_store.py`（删 DDL 段，加 import）、`async_pg_store.py`（同）
- 调用方：**0 改动**（re-export 保证符号不变）

**收益**：
- 消除 DDL 双源同步风险（pg_store 与 async_pg_store 共用一份）。
- pg_store.py 减少约 80 行。
- 为后续拆分铺路（DDL 是最易提取、零风险的部分）。

### 方案 B：按职责拆分为 4 个子模块

**内容**：
- `pg_pool.py`：`_get_pool` / `close_pool` / `_ensure_init` / `_execute_with_retry` / `_query_with_retry` / `_get_pg_circuit_breaker` / `_parse_data` / DDL（或 import 自 pg_schema）。
- `pg_trace_store.py`：`PGTraceStore` 类。
- `pg_session_store.py`：`PGSessionStore` 类。
- `pg_error_store.py`：`upsert_error` 函数（暂不补 ABC）。
- `pg_spec_store.py`：`save_spec` / `get_spec` / `list_specs_pg` / `delete_spec`。
- `pg_store.py`：**保留为 re-export facade**，`from .pg_trace_store import PGTraceStore` 等，保证现有 import 不破。

**改动文件**：
- 新增：5 个子模块文件
- 修改：`pg_store.py`（改为 facade，约 30 行 re-export）、`async_pg_store.py`（可选同步拆分，或不拆）
- 调用方：**0 改动**（facade re-export 保证 `from pg_store import X` 仍可用）

**收益**：
- 每个子模块 100–200 行，可维护性大幅提升。
- 职责边界清晰，PR review 范围缩小。
- 为方案 C 的 ABC 补全打下结构基础。

### 方案 C：完整拆分 + 补 ErrorStorage/SpecStorage ABC + pg_store/async_pg_store 同步

**内容**：在方案 B 基础上：
1. **补 ABC**（`base.py` 新增）：
   ```python
   class ErrorStorage(ABC):
       @abstractmethod
       def upsert_error(self, record_data: dict) -> None: ...
       @abstractmethod
       def query_errors(self, fingerprint=None, session_id=None, since_minutes=1440, limit=100) -> list[dict]: ...

   class SpecStorage(ABC):
       @abstractmethod
       def save_spec(self, spec: dict) -> None: ...
       @abstractmethod
       def get_spec(self, spec_id: str) -> Optional[dict]: ...
       @abstractmethod
       def list_specs(self, kind=None, target=None) -> list[dict]: ...
       @abstractmethod
       def delete_spec(self, spec_id: str) -> bool: ...
   ```
2. **类化 errors/specs**：`PGErrorStore(ErrorStorage)`、`PGSpecStore(SpecStorage)`，将裸函数改为方法。
3. **扩展 factory**：新增 `get_error_store()` / `get_spec_store()`，支持 memory 降级。
4. **同步 async_pg_store**：拆分为对应的 async 子模块，补 async 版 ABC（或同步/异步共用 ABC 但方法签名不同——需设计决策）。
5. **重构调用方**：`errors.py` / `spec_store.py` 改为通过 factory 获取 store，不再直接 import pg_store 函数。
6. **errors.py 直连池修复**：`query_pg_errors` 改为调用 `error_store.query_errors()`，消除直接 `_get_pool` 访问。

**改动文件**：
- 新增：5 个 PG 子模块 + 可能的 memory 版 ErrorStore/SpecStore
- 修改：`base.py`（加 2 个 ABC）、`factory.py`（加 2 个 getter）、`pg_store.py`（facade）、`async_pg_store.py`（拆分）、`errors.py`（改用 factory）、`spec_store.py`（改用 factory）
- 调用方：errors.py / spec_store.py 需重构调用方式

**收益**：
- 架构一致性：4 种存储全部 ABC + factory + 可换后端。
- errors/specs 获得 memory 降级能力（非 PG 部署也能用）。
- errors.py 的直连池泄露点被消除。
- 测试可通过 ABC mock，降低对真实 PG 的依赖。

### 方案对比矩阵

| 维度 | 方案 A | 方案 B | 方案 C |
|------|--------|--------|--------|
| 改动文件数 | 1 新 + 2 改 | 5 新 + 2 改 | 7+ 新 + 6 改 |
| 调用方改动 | 0 | 0（facade） | errors.py + spec_store.py |
| 消除职责混杂 | 部分（仅 DDL） | 是 | 是 |
| 补 ABC | 否 | 否 | 是 |
| 消除 errors.py 直连池 | 否 | 否 | 是 |
| 修复 N4-FU-3 | 不涉及（独立修复） | 不涉及 | 不涉及 |
| 修复重试不一致 | 不涉及 | 不涉及 | 不涉及 |
| 工作量 | 0.5 人日 | 2 人日 | 5–7 人日 |
| 风险 | 极低 | 低 | 中 |
| 是否可独立交付 | 是 | 是 | 是（但建议先 A→B→C） |

> **注**：N4-FU-3 修复与重试一致性修复是**独立工作项**，不绑定于 A/B/C 任一方案，可单独审批执行（见 §4、§5）。方案 A/B/C 是结构重构，N4-FU-3 和重试是行为修复，两者正交。

---

## 4. N4-FU-3 修复方案

### 4.1 问题定位

| 文件 | 行号 | 当前代码 |
|------|------|----------|
| `pg_store.py` | 90 | `raise RuntimeError(f"无法连接 PostgreSQL: {e}")` |
| `async_pg_store.py` | 169 | `raise RuntimeError(f"无法连接 PostgreSQL (asyncpg): {e}")` |

`{e}` 是 `psycopg2.OperationalError` / `asyncpg.PostgresError` 的字符串形式，可能包含 host/port/dbname/user 等连接参数细节。上一行 `logger.critical(f"PostgreSQL 连接失败: {e}")` 已在**服务端日志**记录完整错误（合规），`raise` 的目的是向上层抛出启动失败信号，无需携带参数细节。

### 4.2 修复方案

**对齐 N4-FU-1/2 的修复模式**（见 `AI_HANDOFF.md` L259-262）：raise 的消息去除 `{e}`，仅保留通用提示；完整错误由 logger 记录。

```python
# pg_store.py:88-90 修复后
except psycopg2.OperationalError as e:
    logger.critical(f"PostgreSQL 连接失败: {e}")   # 服务端日志保留完整错误（合规）
    raise RuntimeError("无法连接 PostgreSQL，详情见服务端日志")  # 不含 {e}
```

```python
# async_pg_store.py:167-169 修复后
except (OSError, asyncpg.PostgresError) as e:
    logger.critical("asyncpg 连接失败: %s", e)   # 服务端日志保留完整错误
    raise RuntimeError("无法连接 PostgreSQL (asyncpg)，详情见服务端日志")
```

### 4.3 为什么不直接 `raise RuntimeError("无法连接 PostgreSQL") from e`

两种选择：
- **选项 1（推荐）**：`raise RuntimeError("无法连接 PostgreSQL，详情见服务端日志")` —— 完全不附带 e，与 N4-FU-1/2 的处理方式一致（spec.py / stdio.py 都是去掉 `{e}`）。
- **选项 2**：`raise RuntimeError("无法连接 PostgreSQL") from e` —— Python 的异常链 `from e` 会在 traceback 中显示 `The above exception was the direct cause of...` 并打印 `e` 的完整 repr，**仍然泄露**到 traceback 输出。因此 `from e` 不能用于修复此问题。

**推荐选项 1**，与既有 N4-FU 修复保持一致。

### 4.4 验证方式

新增测试 `tests/unit/test_pg_store_security.py`：
```python
def test_pg_connection_error_does_not_leak_params(monkeypatch):
    """N4-FU-3：连接失败时 RuntimeError 不含 PG 连接参数细节"""
    # mock psycopg2.pool.ThreadedConnectionPool 抛 OperationalError 含敏感信息
    # 断言 raise 的 RuntimeError 消息不含 host/port/user/password
```

---

## 5. 重试一致性修复方案

### 5.1 问题定位

`_query_with_retry`（L408-438）的 `_do_query` 内部：
```python
except psycopg2.OperationalError:
    try:
        pool.putconn(conn, close=True)   # 关闭坏连接
    except Exception:
        pass
    raise                               # ← 直接抛出，无重试，无在新连接上重试
```

对比 `_execute_with_retry` 的 `_do_execute`：会 `putconn(close=True)` → `getconn()` 新连接 → `sleep(0.1)` → 重试 `max_retries` 次。

### 5.2 修复方案

为 `_query_with_retry` 增加重连重试，行为对齐 `_execute_with_retry`：

```python
def _query_with_retry(conn, sql: str, params: tuple = (), fetch_all: bool = True, max_retries: int = 2):
    """执行查询 SQL（SELECT），受熔断器保护。
    OperationalError 时获取新连接并重试（与 _execute_with_retry 行为一致）。
    """

    def _do_query():
        nonlocal conn
        last_error = None
        pool = _get_pool()
        for attempt in range(max_retries + 1):
            try:
                cur = conn.cursor()
                cur.execute(sql, params)
                if fetch_all:
                    return cur.fetchall()
                return cur.fetchone()
            except psycopg2.OperationalError as e:
                last_error = e
                if attempt < max_retries:
                    try:
                        pool.putconn(conn, close=True)
                    except Exception:
                        pass
                    conn = pool.getconn()
                    logger.warning(f"PG 查询重试 ({attempt + 1}/{max_retries}): {e}")
                    time.sleep(0.1)
                else:
                    try:
                        pool.putconn(conn, close=True)
                    except Exception:
                        pass
                    raise last_error

    cb = _get_pg_circuit_breaker()
    if cb:
        try:
            return cb.call(_do_query)
        except pybreaker.CircuitBreakerError:
            logger.warning("PG 熔断器已触发（查询）")
            raise
    return _do_query()
```

### 5.3 注意事项

1. **连接所有权问题**：`_query_with_retry` 的调用方（如 `PGTraceStore.get_entries`）在 `finally` 中调用 `self._put(conn)` 归还连接。若 `_query_with_retry` 内部换了连接（重连后），返回的 `conn` 引用已变，但调用方仍持有旧 `conn` 引用去 `putconn`——会 putconn 一个已 close 的连接。
   - **当前 `_execute_with_retry` 已有此问题**：它返回 `(conn, rowcount)`，调用方需用返回的 conn 替换。但 `_query_with_retry` 当前返回的是查询结果，不返回 conn。
   - **修复方案**：`_query_with_retry` 也应返回 `(result, conn)`，调用方在 finally 中用最新 conn 归还。这需要同步修改所有调用方（`get_entries` / `get` / `list_active` / `list_request_ids` / `get_spec` / `list_specs_pg`）。
   - **替代方案**：在 `_query_with_retry` 内部完成重试后，把重连后的 conn 重新赋值给外层的 conn 变量（Python 闭包无法直接改外层局部变量，除非用 nonlocal 或 mutable 容器）。更简洁的做法是让调用方传入 conn 的 holder（如 `lambda: conn[0]`）——但这会增加复杂度。
   - **推荐做法**：参考 `_execute_with_retry` 的 `(conn, rowcount)` 返回模式，让 `_query_with_retry` 返回 `(conn, result)`，调用方在 finally 用返回的 conn。这是与现有写路径一致的语义。

2. **errors.py 的 `query_pg_errors` 直连池**：该函数绕过 `_query_with_retry`，直接用 `cur.execute`，既无重试也无熔断。修复重试一致性时，建议同时将其改为调用 `error_store.query_errors()`（依赖方案 C 的 ABC）或至少改用 `_query_with_retry`。

### 5.4 验证方式

扩展 `tests/unit/test_circuit_breaker.py`：
```python
def test_query_with_retry_reconnects_on_operational_error(monkeypatch):
    """_query_with_retry 在 OperationalError 时应在新连接上重试"""
    # mock pool：第一次 getconn 返回坏连接（execute 抛 OperationalError）
    # 第二次 getconn 返回好连接（execute 返回结果）
    # 断言：调用了 2 次 getconn，最终返回好连接的结果
```

---

## 6. 测试方案

### 6.1 现有测试覆盖

| 测试文件 | 覆盖范围 | 类型 |
|----------|----------|------|
| `tests/unit/test_storage.py` | PGTraceStore / PGSessionStore CRUD（需真实 PG，默认 skip）；factory 白名单；asyncpg mock；分区工具函数（纯函数）；归档 mock | 单元+集成 |
| `tests/unit/test_circuit_breaker.py` | `_execute_with_retry` / `_query_with_retry` 熔断行为；pybreaker 缺失降级 | 单元（mock） |
| `tests/integration/test_pg_integration.py` | `_get_pool` / `_parse_data`；trace 往返；Dashboard；MCP Tools；trace_repo 持久化 | 集成（需 PG） |
| `tests/integration/test_process_boundary.py` | `close_pool` 进程退出清理 | 集成（需 PG） |
| `tests/integration/test_runtime_enablement.py` | `_get_pg_circuit_breaker` 可用性 | 集成 |

**覆盖空白**：
- `_query_with_retry` 的重连重试行为**无测试**（仅测了熔断触发，未测 OperationalError 重连）。
- `upsert_error` / `save_spec` / `get_spec` / `list_specs_pg` / `delete_spec` 的 PG 实现**无单元测试**（仅 asyncpg mock 版有测试），PG 路径仅靠集成测试覆盖（且集成测试未直接测这些函数）。
- N4-FU-3 泄露点**无测试**。
- DDL 字符串一致性**无测试**（pg_store 与 async_pg_store 的 DDL 常量是否相同，无断言）。

### 6.2 拆分后需新增的测试

| 测试 | 目的 | 依赖方案 |
|------|------|----------|
| `test_pg_store_security.py::test_pg_connection_error_no_leak` | N4-FU-3：RuntimeError 不含连接参数 | 独立（N4-FU-3 修复） |
| `test_circuit_breaker.py::test_query_with_retry_reconnects` | 重试一致性：读路径重连重试 | 独立（重试修复） |
| `test_circuit_breaker.py::test_query_with_retry_returns_new_conn` | 重试后返回的 conn 是新连接 | 独立 |
| `test_pg_error_store.py`（新增） | `upsert_error` PG 路径（mock pool） | 方案 B/C |
| `test_pg_spec_store.py`（新增） | `save_spec`/`get_spec`/`list_specs_pg`/`delete_spec` PG 路径（mock pool） | 方案 B/C |
| `test_storage_factory.py::test_get_error_store` / `test_get_spec_store` | factory 返回正确后端 | 方案 C |
| `test_pg_schema_consistency.py` | pg_store 与 async_pg_store 的 DDL 常量字符串一致 | 方案 A |

### 6.3 回归验证策略

1. **facade 兼容性测试**（方案 A/B）：保留 `pg_store.py` 作为 re-export facade 后，运行现有全部测试（`pytest tests/unit/ tests/integration/ -q`）应 **0 失败 0 改动**——这证明 facade 的 re-export 行为与原单文件等价。
2. **import 路径扫描**：拆分后用 `grep -r "from app.mcp.core.storage.pg_store import"` 确认所有 import 仍可用（facade re-export 兜底）。
3. **真实 PG 集成测试**：在 `STORAGE_BACKEND=postgresql` 环境下运行 `tests/integration/test_pg_integration.py` 全部通过。
4. **ruff lint**：`ruff check app/mcp/core/storage/` 0 违规。

---

## 7. 工作量估算

| 工作项 | 估算（人日） | 说明 |
|--------|-------------|------|
| **方案 A**（DDL 提取） | 0.5 | 新建 pg_schema.py + 2 文件改 import + facade re-export + 1 一致性测试 |
| **方案 B**（职责拆分） | 2.0 | 5 新子模块 + pg_store.py 改 facade + 现有测试验证 + import 扫描 |
| **方案 C**（完整重构） | 5.0–7.0 | B 的基础上 + 2 ABC + factory 扩展 + errors.py/spec_store.py 重构 + memory 后端 + async 同步拆分 + 新增测试 |
| **N4-FU-3 修复** | 0.25 | 2 行改动（pg_store + async_pg_store）+ 1 测试用例 |
| **重试一致性修复** | 0.5–1.0 | _query_with_retry 改造 + 所有调用方改返回值处理 + 2 测试用例（连接所有权问题增加复杂度） |
| **errors.py 直连池修复** | 0.5（含方案 C） / 独立 1.0 | 若走方案 C 的 ErrorStorage ABC，自然消除；独立修需改 query_pg_errors 用 _query_with_retry |

**推荐执行顺序**（若全量推进）：
1. N4-FU-3 修复（0.25 人日，独立可交付，风险极低）
2. 方案 A（0.5 人日，零风险试水，消除 DDL 双源）
3. 重试一致性修复（0.5–1.0 人日，独立行为修复）
4. 方案 B（2.0 人日，结构拆分，facade 兜底）
5. 方案 C（3.0–5.0 人日，在 B 基础上增量，补 ABC + factory + 调用方重构）

**若只做最小修复**：仅做 1 + 3 = 0.75–1.25 人日，不触碰结构。

---

## 8. 风险分析

### 8.1 方案 A 风险

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| re-export import 失败导致 ImportError | 低 | 启动失败 | facade 用 `from .pg_schema import DDL_TRACES, DDL_SESSIONS, ...` 显式列出；现有测试自动覆盖 |
| DDL 字符串意外修改 | 低 | 表结构不一致 | 新增 DDL 一致性测试 |

### 8.2 方案 B 风险

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| 循环 import（子模块互相引用基础设施） | 中 | ImportError | 基础设施放 `pg_pool.py`，子模块单向 import pool 模块 |
| facade re-export 遗漏符号 | 低 | 调用方 ImportError | 用 `grep` 扫描所有 import 符号，逐个在 facade 中 re-export |
| `_parse_data` 归属争议（跨 trace/error 使用） | 低 | 放错模块导致循环 import | 放入 `pg_pool.py` 或独立 `pg_utils.py` |
| 测试 mock 路径失效（test_storage.py 用 `monkeypatch.setattr(pg_mod, "PGTraceStore", ...)`） | 中 | 测试失败 | facade re-export 保证 `pg_store.PGTraceStore` 仍指向同一类对象；mock 路径不变 |

### 8.3 方案 C 风险

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| async 与 sync ABC 契约冲突（async 方法 vs sync 方法签名） | 高 | 设计僵局 | 不强求共用 ABC；sync ABC 定义 sync 方法，async store 单独定义 async 方法，factory 按 `pg_async_enabled` 返回不同类型（现状即如此） |
| errors.py / spec_store.py 重构引入行为变化 | 中 | errors/specs 写入/查询行为变化 | 重构后跑全部集成测试；保持 PG SQL 不变，仅改调用方式 |
| memory 后端 ErrorStore/SpecStore 需新设计 | 中 | 设计成本 | 可只做 PG 实现，factory 在非 PG 时返回 None 或抛 NotImplementedError，不强制实现 memory 版 |
| errors.py `query_pg_errors` 行为变化（从直连池改为 ABC 调用） | 中 | 查询结果格式变化 | 保持 `query_errors` 返回值结构与 `query_pg_errors` 完全一致 |
| 改动面大，回归风险高 | 高 | 线上故障 | 分阶段合并：先 ABC（不改调用方）→ 再 factory → 再调用方迁移，每阶段独立测试 |

### 8.4 N4-FU-3 修复风险

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| 错误信息过于笼统，排障困难 | 低 | 运维成本 | `logger.critical` 已记录完整 `{e}`，运维查服务端日志即可 |
| 遗漏 async_pg_store.py 的同款泄露 | 中 | 安全漏洞残留 | 修复时同步改两处；测试覆盖两个文件 |

### 8.5 重试一致性修复风险

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| 连接所有权变更导致 putconn 重复/遗漏 | 高 | 连接池连接泄漏或报错 | 让 `_query_with_retry` 返回 `(conn, result)`，调用方在 finally 用返回的 conn；增加连接归还计数测试 |
| 重试放大慢查询风险（SELECT 重试 2 次） | 低 | 查询延迟翻倍 | 仅对 OperationalError（连接级）重试，不对其他错误重试；保持 max_retries=2 与写路径一致 |
| 现有 mock 测试（test_circuit_breaker.py）假设 `_query_with_retry` 不重连 | 中 | 测试失败 | 更新 mock 测试，断言新行为 |

---

## 9. 推荐路径与审批决策建议

### 9.1 推荐执行路径

**阶段 1（立即可做，低风险）**：
- ✅ N4-FU-3 修复（0.25 人日）——安全债，独立可交付，无结构改动
- ✅ 方案 A：DDL 提取（0.5 人日）——零风险试水，消除 DDL 双源

**阶段 2（行为修复，中等风险）**：
- ✅ 重试一致性修复（0.5–1.0 人日）——需谨慎处理连接所有权

**阶段 3（结构重构，可选）**：
- 方案 B（2.0 人日）——若团队认为 850 行可接受，可推迟
- 方案 C（3.0–5.0 人日）——若需 errors/specs 支持多后端/可测试性，再做

### 9.2 审批决策点

请项目 owner 对以下各项独立审批：

1. **N4-FU-3 修复**：是否批准修改 `pg_store.py:90` + `async_pg_store.py:169`（各 1 行）？
2. **重试一致性修复**：是否批准改造 `_query_with_retry` 及其调用方？
3. **方案 A**：是否批准提取 DDL 到 `pg_schema.py`？
4. **方案 B**：是否批准按职责拆分为 4 子模块 + facade？
5. **方案 C**：是否批准补 ErrorStorage/SpecStorage ABC + factory 扩展 + 调用方重构？

每项可独立批准/拒绝。建议至少批准 1（安全债）和 3（DDL 双源风险）。

---

## 附录 A：pg_store.py 完整结构索引

```
L1-16     模块 docstring + imports
L18-43    熔断器（_pg_circuit_breaker, _get_pg_circuit_breaker）
L46-57    _parse_data 工具函数
L60-104   全局连接池（_get_pool, close_pool）           ← N4-FU-3 泄露点 L90
L107-179  DDL 常量（DDL_TRACES/SESSIONS/ERRORS/SPECS/TRACES_ARCHIVE）
L182-249  分区工具（_month_partition_name, _month_range_epoch, _create_partition_for_month, _ensure_partitions）
L252-282  归档工具（_archive_old_traces）
L285-341  _ensure_init（建表+分区+归档初始化）
L345-405  _execute_with_retry（写路径，重连重试）       ← 重试一致性问题：读路径无此行为
L408-438  _query_with_retry（读路径，无重连重试）       ← 重试不一致点
L444-565  PGTraceStore(TraceStorage)
L571-670  PGSessionStore(SessionStorage)
L677-732  upsert_error（裸函数）                         ← 无 ErrorStorage ABC
L738-850  save_spec/get_spec/list_specs_pg/delete_spec（裸函数） ← 无 SpecStorage ABC
```

## 附录 B：引用方完整清单（31 处 import）

见 §2.1（生产 5 文件）与 §2.2（测试 6 文件）。
