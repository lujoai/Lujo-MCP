# ai-debug-mcp 长期技术路线

> 定位：技术负责人 Roadmap，描述未来架构演进、Phase 规划、技术债和演进方向。
> 注意：本文件不记录当前开发任务，当前任务请见 [DEV_PLAN.md](./DEV_PLAN.md)。
> 来源：2026-07-10 架构师评审 + 产品迭代规划
> 最终定位：**AI 时代的软件可观测 + 自动调试基础设施**
> 核心原则：**先成为"优秀的数据采集系统"，再成为"聪明的 AI Debug 系统"**
> 没有高质量数据，AI 只是聊天机器人。
> 功能完成度与默认交付状态请以 [DELIVERY_MATRIX.md](./DELIVERY_MATRIX.md) 为准。

---

## 最终目标架构

```
                 AI IDE
             Cursor/Trae/Codex
                   |
                   |
                 MCP
                   |
        ---------------------
        AI Debug Platform
        ---------------------
                   |
        ---------------------
        Trace System
        Error Engine
        Knowledge Base
        Agent System
        ---------------------
                   |
             Application
```

---

## 总路线

```
Phase 0  项目基础整理（1 周）
    ↓
Phase 1  生产级数据采集系统（2-3 周）
    ↓
Phase 2  分布式链路追踪平台（3 周）
    ↓
Phase 3  智能错误分析引擎（1 个月）
    ↓
Phase 4  RAG 知识库系统（1 个月，当前已完成指纹知识库基础版）
    ↓
Phase 5  AI Debug Agent（核心）
    ↓
Phase 6  自动修复平台
```

---

## Phase 0：项目基础整理（1 周）

> 目标：让别人 clone 下来能运行的开源项目

### Module 1：项目标准化

```
ai-debug-mcp/
├── app/
├── tests/
├── docs/
├── docker/
├── examples/
├── migrations/           # 数据库 Schema 迁移 SQL 文件（新增）
├── scripts/              # 一键式脚本（新增详细规划）
│   ├── run_tests.sh
│   ├── lint.sh
│   └── init_db.sh
├── requirements.txt
├── docker-compose.yml    # 第一公民启动方式
├── README.md
└── LICENSE
```

**docker-compose 优先策略**：

> **战略决策**：README 的“快速开始”部分，第一步是 `git clone`，第二步就是 `docker compose up -d`。确保这个命令能一键拉起 PostgreSQL、Redis 和 App。

当前 docker-compose.yaml 已完整定义 PostgreSQL、Redis 和 App 三个服务。Redis 服务配置如下（redis:7-alpine + healthcheck + maxmemory 256mb allkeys-lru）：

```yaml
# docker-compose.yaml 中的 Redis 服务（已实现）
services:
  redis:
    image: redis:7-alpine
    ports: ["6379:6379"]
    command: redis-server --maxmemory 256mb --maxmemory-policy allkeys-lru
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 10
```

**scripts/ 目录详细规划**：

| 脚本 | 内容 | 用途 |
|------|------|------|
| `scripts/run_tests.sh` | 封装 `python -m pytest tests/`，输出测试数量 | 一键跑测试 |
| `scripts/lint.sh` | 封装 `ruff` 检查工具 | 代码质量检查 |
| `scripts/init_db.sh` | 按顺序执行 migrations/ 目录下的所有 SQL 文件 | 数据库初始化 |

**README 项目状态表格（唯一真相来源）**：

在 README 顶部维护一个「项目状态」表格，其他文档引用它，避免文档不一致：

| 指标 | 状态 |
|------|------|
| MCP 工具数 | HTTP 15 / stdio 15 |
| 测试覆盖 | 当前测试状态以 [README.md](../../README.md) 项目状态表为准 |
| 存储后端 | memory / PostgreSQL（工厂模式）|
| LLM Provider | openai / zhipu / custom |

### Module 2：配置系统

```
app/config/
├── settings.py    # 数据库/Redis/API Key/LLM 配置
├── logging.py
└── constants.py
```

**已有 .env.example**：项目根目录已提供 [.env.example](./.env.example)，新用户只需复制为 `.env` 并填入 API Key 即可运行。

### Module 3：日志系统

> **战略决策**：不引入 loguru，继续使用标准 logging 模块 + JSON formatter。项目已有 [app/utils/logging.py](./app/utils/logging.py)，引入 loguru 是替换而非新增，收益有限。

**增量替换策略**：
- 创建 lint 规则禁止新代码用 `print()`
- 在新写的或修改的模块中，强制使用 `app.utils.logging`
- 现有 `print()` 逐步替换，不一次性全局替换

**日志格式（JSON）**：

```json
{
  "time": "",
  "level": "ERROR",
  "service": "",
  "trace_id": "",
  "message": ""
}
```

### 验收标准

```
git clone → docker compose up -d → 打开 http://localhost:8000 → 看到服务
```

---

## Phase 1：生产级数据采集系统（2-3 周）

> 目标：从"内存日志工具"变成"企业级 Trace 存储系统"
> 项目水平：7.5 → 8.5

### Module 1：Storage 存储层

```
app/storage/
├── database.py           # 连接池管理（从 pg_store.py 拆分）
├── models.py             # SQLAlchemy 模型定义（未来引入）
├── repository.py         # Repository 接口定义
└── migrations/           # Schema 迁移 SQL 文件
    ├── 20260710_create_traces_table.sql
    ├── 20260710_create_sessions_table.sql
    ├── 20260711_create_errors_table.sql
    ├── 20260711_create_specs_table.sql
    ├── 20260712_create_network_records_table.sql
    └── 20260712_create_ui_events_table.sql
```

**migrations/ 目录管理策略**：

> **战略决策**：当前 DDL 硬编码在 [pg_store.py](./app/mcp/core/storage/pg_store.py#L62-L82) 的 `_ensure_init()` 里（`CREATE TABLE IF NOT EXISTS`），不可追溯、不可版本化。拆出来用 SQL 文件管理，零学习成本、版本控制清晰，未来 Alembic 可直接 import 这些 SQL 作为 baseline。

**migrations/ 目录结构**：

| 文件 | 内容 | 状态 |
|------|------|------|
| `20260710_create_traces_table.sql` | traces 表 DDL | ✅ 从 pg_store.py 导出 |
| `20260710_create_sessions_table.sql` | sessions 表 DDL | ✅ 从 pg_store.py 导出 |
| `20260711_create_errors_table.sql` | errors 表 DDL | ✅ 迁移文件已创建（代码 CRUD 待实现） |
| `20260711_create_specs_table.sql` | specs 表 DDL | ✅ 迁移文件已创建（代码 CRUD 待实现） |
| `20260712_create_network_records_table.sql` | network_records 表 DDL | ✅ 迁移文件已创建（代码 CRUD 待实现） |
| `20260712_create_ui_events_table.sql` | ui_events 表 DDL | ✅ 迁移文件已创建（代码 CRUD 待实现） |

**migrations/ 工作流程**：

1. 每次需要修改数据库 Schema，在 migrations/ 目录下创建按时间戳命名的 SQL 文件
2. 文件内容就是标准的 `CREATE TABLE ...` 或 `ALTER TABLE ...` 语句
3. 使用 `scripts/init_db.sh` 脚本按顺序执行所有 SQL 文件
4. 开发环境：`docker compose up` 时自动执行 `scripts/init_db.sh`

**scripts/init_db.sh 逻辑**：

```bash
#!/bin/bash
# 按文件名排序执行 migrations/ 目录下的所有 SQL 文件
for file in $(ls migrations/*.sql | sort); do
    echo "Executing $file..."
    psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$PG_DATABASE" -f "$file"
done
```

**数据库技术栈选型**：

| 组件 | 技术 | 用途 | 状态 |
|------|------|------|------|
| 主关系型数据库 | PostgreSQL 16 | trace / session / spec / error 持久化 | ✅ 已部分实现 |
| 驱动 | psycopg2-binary（同步）/ asyncpg（异步升级） | 数据库连接 | ✅ 已实现 |
| ORM | SQLAlchemy 2.0 + Alembic | schema 迁移管理 | 🔲 待引入 |
| 缓存 / 限流 / 队列 | Redis 7 | 限流计数、会话缓存、异步任务队列 | ✅ 已实现 (state_store) |

**PostgreSQL 核心优势**：
1. **JSONB 类型**完美匹配 trace data 的灵活 schema 需求
2. **强大的 MVCC** 机制确保高并发写入下的读取性能
3. **成熟的查询优化器**为复杂的分析查询提供强大支持

**数据库设计 — 已实现表（✅）**：

```sql
-- traces 表（app/mcp/core/storage/pg_store.py DDL_TRACES）
traces (id BIGSERIAL, request_id TEXT, timestamp DOUBLE PRECISION, step TEXT, data JSONB)
-- 索引：idx_traces_rid (request_id), idx_traces_ts (timestamp)

-- sessions 表（app/mcp/core/storage/pg_store.py DDL_SESSIONS）
sessions (session_id TEXT PRIMARY KEY, created_at DOUBLE PRECISION, last_active DOUBLE PRECISION, metadata JSONB)
-- 索引：idx_sessions_la (last_active)
```

**数据库设计 — 待建表（🔲）**：

```sql
-- errors 表（错误指纹去重聚合）
errors (id BIGSERIAL, trace_id TEXT, exception_type TEXT, message TEXT, 
        stack TEXT, file TEXT, line INTEGER, fingerprint TEXT, 
        occurrence_count INTEGER DEFAULT 1, created_at TIMESTAMP, updated_at TIMESTAMP)

-- specs 表（规范存储迁移，替代当前 dict+Lock）
specs (id TEXT PRIMARY KEY, kind TEXT, target TEXT, expect JSONB, 
       created_at TIMESTAMP, updated_at TIMESTAMP)

-- network_records 表（网络请求采集）
network_records (id BIGSERIAL, trace_id TEXT, method TEXT, url TEXT, 
                 status INTEGER, request_body TEXT, response_body TEXT, 
                 duration_ms INTEGER, created_at TIMESTAMP)

-- ui_events 表（前端事件采集）
ui_events (id BIGSERIAL, trace_id TEXT, action TEXT, selector TEXT, 
           timestamp DOUBLE PRECISION, metadata JSONB)
```

**迁移任务清单**：

| 任务 | 说明 | 优先级 |
|------|------|--------|
| ✅ spec_store 迁移到 TraceStorage 工厂模式 | 已完成（2026-07-19）：dict+Lock 主存 + add_log 持久化 + list_specs 从 trace_store `_restore_from_storage()` 恢复（C4 对标模式） | P0 |
| 引入 SQLAlchemy 2.0 + Alembic | 替换 pg_store.py 裸 SQL，管理 schema 变更 | P0（Repository 层前置依赖）|
| 创建 errors 表 + 指纹去重逻辑 | 独立表未建；当前 errors 通过 trace_repo.save_trace → add_log(error_id, "trace_data", ...) 持久化到 traces 表 JSONB data 字段 | P1 |
| 创建 network_records / ui_events 表 | 独立表未建；当前 network_records / ui_events 通过 save_network_record / save_ui_event → add_log 持久化到 traces 表（step 区分类型） | P1 |

### Module 2：Repository 层

> **战略决策**：当前 [pg_store.py](./app/mcp/core/storage/pg_store.py) 同时承担连接池管理（`_get_pool`）和数据操作（`PGTraceStore`/`PGSessionStore`），职责混合。拆分为 `database.py`（连接池）+ `trace_repository.py` + `session_repository.py`，业务逻辑与 SQL 解耦。
>
> 📌 **评估更新（2026-07-23，只读分析）**：598 行非"上帝文件"，单纯减行数不值得拆；核心是 `errors`/`specs` 无 ABC 的设计债（三后端契约不对齐、`spec_store` 靠 try/except 降级）。推荐方案调整为 `pg_pool.py`/`pg_schema.py`/`pg_store.py`/`pg_errors.py`/`pg_specs.py` 五模块 + 补 `ErrorStorage`/`SpecStorage` ABC + `async_pg_store.py` 同步拆（方案 C，2-2.5 人日，需 AI_RULES 审批）。发现隐藏缺陷：`_execute_with_retry` 读取路径覆盖不一致（无重连重试）。详见 [ROADMAP.md](./ROADMAP.md) 技术债务。

**架构演进**：

```
错误：  API → logs.py → pg_store.py (连接池 + SQL)
企业：  API → logs.py → Repository → database.py (连接池) → Database
```

**Repository 拆分方案**：

| 文件 | 职责 | 来源 |
|------|------|------|
| `app/storage/database.py` | 只负责数据库连接的创建和管理（工厂模式核心）| 从 pg_store.py 拆分 `_get_pool` / `close_pool` / `_ensure_init` |
| `app/storage/trace_repository.py` | `TraceRepository` 类，封装 traces 表所有 SQL 查询 | 从 pg_store.py 拆分 `PGTraceStore` |
| `app/storage/session_repository.py` | `SessionRepository` 类，封装 sessions 表所有 SQL 查询 | 从 pg_store.py 拆分 `PGSessionStore` |
| `app/storage/spec_repository.py` | `SpecRepository` 类，封装 specs 表所有 SQL 查询 | 新建（spec_store 迁移目标）|

**Repository 接口定义**（`app/storage/repository.py`）：

```python
# 抽象基类，定义统一接口
class TraceRepository(ABC):
    @abstractmethod
    def save_entry(self, request_id: str, entry: dict) -> None: ...
    
    @abstractmethod
    def get_entries(self, request_id: str) -> list[dict]: ...
    
    @abstractmethod
    def delete(self, request_id: str) -> None: ...
    
    @abstractmethod
    def cleanup_expired(self, ttl_seconds: int) -> int: ...


class SessionRepository(ABC):
    @abstractmethod
    def save(self, session_id: str, data: dict) -> None: ...
    
    @abstractmethod
    def get(self, session_id: str) -> Optional[dict]: ...
    
    @abstractmethod
    def delete(self, session_id: str) -> None: ...
    
    @abstractmethod
    def list_active(self, ttl_seconds: int) -> list[dict]: ...
    
    @abstractmethod
    def cleanup_expired(self, ttl_seconds: int) -> int: ...
```

**目录结构**：

```
app/storage/
├── database.py              # 连接池管理
├── repository.py            # Repository 接口定义（ABC）
├── trace_repository.py      # TraceRepository 实现
├── session_repository.py    # SessionRepository 实现
├── spec_repository.py       # SpecRepository 实现（新建）
└── migrations/              # Schema 迁移 SQL 文件
```

**拆分步骤**：

1. **第一步**：创建 `database.py`，提取连接池管理逻辑
2. **第二步**：创建 `repository.py`，定义 ABC 接口
3. **第三步**：创建 `trace_repository.py` + `session_repository.py`，实现接口
4. **第四步**：修改 `factory.py`，返回 Repository 实例而非 Store 实例
5. **第五步**：修改 `logs.py`，使用 Repository 接口
6. **第六步**：创建 `spec_repository.py`，完成 spec_store 迁移

**优势**：
- 业务逻辑不再直接接触 SQL，调用 Repository 的方法即可
- 未来换数据库或引入缓存只改 Repository 内部
- 为新模块（如 spec_store）提供良好示范

### Module 3：异步队列（优化项，非必须项）

> **战略决策**：Phase 1 先通过数据库优化手段验证性能，Redis Queue 作为"优化项"而非"必须项"。
> 当前项目规模下，同步写入 PG 的性能可能已经足够；过早引入异步队列会增加复杂度和运维成本。

**当前状态**：用户请求 → 保存日志 → 返回（同步阻塞）

**性能优化路径（优先级从高到低）**：

> **战略决策**：asyncpg 从 P1 调整到 P2。当前整个存储层（factory、pg_store、base 抽象）都是同步设计，换 asyncpg 需要把整个存储链路改成异步（factory 返回 awaitable、logs.py 的 add_log 变 async、所有调用方加 await），改动量太大、风险高。Phase 1 继续用 psycopg2 同步 + 批量插入优化，asyncpg 推到 Phase 2 再评估。

| 优先级 | 优化手段 | 说明 | 改动量 |
|--------|---------|------|--------|
| P0 | **批量插入** | 多个 trace entry 合并为一条 INSERT，减少网络往返 | 小（修改 save_entry 为批量接口）|
| P0 | **连接池调优** | 当前 minconn=2, maxconn=10，可根据并发量调整 | 极小（修改 pg_store.py 参数）|
| P2 | **异步写入（asyncpg）** | 使用 asyncpg 替代 psycopg2，FastAPI 异步路由直接 await | 大（需改整个存储链路为异步）|
| P2 | **Redis Queue（RQ）** | 用户请求 → Queue → 后台 Worker → 数据库 | 较大（新增 tasks/ 模块）|

**异步队列方案（P2 优化项）**：

```
app/tasks/
├── worker.py      # RQ Worker 进程
└── queue.py       # 队列操作封装
```

技术：Redis Queue (RQ)

**触发条件**：当同步写入延迟超过 50ms（通过 /metrics 观测），或并发量超过 100QPS 时，再引入 RQ。

### 验收标准

- 同步模式：1000 请求响应时间 < 200ms（P0）
- 批量插入后：1000 请求响应时间 < 100ms（P0）
- 异步模式（启用时）：1000 请求响应时间 < 50ms（P2）

---

## Phase 2：分布式链路追踪平台（3 周）

> 目标：支持一个请求的完整跨服务链路
> 项目水平：8.5 → 9

### Module 1：Telemetry SDK

```
app/telemetry/
├── tracer.py       # Span 创建
├── exporter.py     # OTLP 导出
└── context.py      # 上下文传递
```

### Module 2：OpenTelemetry Middleware

替换自写 middleware → OpenTelemetry 标准 Middleware

自动捕获：请求 / 时间 / 状态码

### Module 3：Exporter

```
Application → OTEL Collector → ai-debug-mcp
```

### 链路示例

```
GET /order
  ├── API span        50ms
  ├── Order Service   20ms
  ├── Payment Service 15ms
  ├── MySQL           10ms
  └── Redis            5ms
```

### 验收标准

显示一次请求的完整调用链 + 每段耗时。

---

## Phase 3：智能错误分析引擎（1 个月）

> 目标：不是"告诉 AI 错误"，而是"系统自己理解错误"

### Module 1：Error Fingerprint（错误指纹）

```
app/analyzer/
├── fingerprint.py
└── classifier.py
```

输入：Exception + Stack + File + Line
输出：error_hash

```
10000 次错误 → 归类为 50 类问题
```

### Module 2：错误分类器

```
timeout    → network issue
null       → code bug
permission → config
```

### Module 3：Root Cause Ranking（核心算法）

输入：错误上下文
输出：原因概率排序

```json
{
  "原因1": {"name": "数据库连接池不足", "confidence": 0.85},
  "原因2": {"name": "网络异常", "confidence": 0.40},
  "原因3": {"name": "SQL 错误", "confidence": 0.20}
}
```

实现：规则 + LLM（不用训练模型）

---

## Phase 4：RAG 知识库系统（1 个月）

> 注：当前仓库已先落地“指纹知识库基础版”（按错误指纹命中历史分析结论、LLM 成功后自动沉淀），本节描述的是下一阶段的“向量检索版 RAG”目标形态，而非当前默认交付状态。

> 让 AI 拥有记忆——AI 项目和普通项目的分水岭

```
历史 Bug → Embedding → Vector DB → AI 检索参考
```

```
app/knowledge/
├── embedding.py
├── vector.py
└── retriever.py
```

**向量数据库技术栈选型**：

| 组件 | 技术 | 用途 | 状态 |
|------|------|------|------|
| 向量数据库 | Qdrant | Bug embedding 存储 + 语义检索历史解决方案 | ✅ 已引入（`app/llm/qdrant_vector_store.py`，2026-07-26） |
| Python SDK | qdrant-client | 向量数据库操作 | ✅ 已引入（`requirements.txt` 锁定 `>=1.9.0`） |
| Embedding 模型 | 智谱 embedding-3 / OpenAI text-embedding-3-small | 将 Bug 描述转为向量 | ✅ 已接入（`QdrantVectorStore._get_embedding_client` 独立客户端，与 LLM provider 解耦） |

**Qdrant 选型理由**：
1. **部署轻量**：单容器即可运行，适合本项目体量
2. **优秀的 Python SDK**：FastAPI 集成简单
3. **开源项目**：数据不出域，无需依赖第三方云服务
4. **对比 Milvus**：部署更轻量，运维成本低
5. **对比 pgvector**：Qdrant 专门优化向量检索，性能更好

**数据结构**：
```
Bug / 原因 / 解决方案 / 代码修改 / embedding_vector
```

**数据流**：
```
Bug 记录 → embedding（智谱/OpenAI）→ 存 Qdrant → AI 检索 Top-K 相似历史 Bug
```

**效果**：再次出现同类问题，AI 直接参考历史解决方案。

---

## Phase 5：AI Debug Agent（核心）

> 目标：类似未来 Cursor

```
app/agent/
├── planner.py    # 规划
├── analyzer.py   # 分析
├── coder.py      # 生成修复
└── tester.py     # 验证
```

**Agent 流程**：
```
发现错误 → 分析 → 找到代码 → 生成修改 → 运行测试 → 提交结果
```

---

## Phase 6：自动修复平台

### Git Agent
```
git diff → git commit → git PR
```

### Test Agent
```
pytest / npm test / go test
```

### Security Agent
```
SQL 注入 / 权限 / 漏洞检查
```

### 完整闭环
```
发现异常
  ↓
分析代码（auto_test + stacktrace + git blame）
  ↓
定位文件
  ↓
生成 Patch（LLM 写修复代码）
  ↓
运行测试（verify + auto_test 回归）
  ↓
提交 PR
```

### 多 Agent 协同
```
Debugger Agent  → 发现 + 定位
Code Agent      → 生成修复
Test Agent      → 验证回归
Git Agent       → 提交 PR
Security Agent  → 安全审查
```

---

## 月份规划

| 时间 | Phase | 任务 | 水平 |
|------|-------|------|------|
| **第 1 个月** | Phase 0 + 1 | PostgreSQL + Repository 层 + migrations/ + Docker 部署（Redis Queue 为优化项）| 7.5 → 8.5 |
| **第 2 个月** | Phase 2 + 3 | OpenTelemetry + asyncpg + Error Fingerprint + Root Cause Ranking | 8.5 → 9 |
| **第 3 个月** | Phase 4 + 5 + 6 | RAG + AI Agent + 自动修复 | 9+ |

---

## PR 拆分策略（开源协作节奏）

将 Phase 0 + 1 的工作拆分为三个独立 PR，便于 review 和合并：

| PR | 内容 | 依赖 | 优先级 |
|----|------|------|--------|
| **PR #1** | [Phase 0] Standardize Project Layout and Docker Setup | 无 | P0 |
| **PR #2** | [Phase 1] Refactor Storage Layer: Introduce Repository Pattern | PR #1 | P0 |
| **PR #3** | [Phase 1] Implement Spec Store with PostgreSQL | PR #2 | P0 |

**PR #1 内容**：
- 实现 docker-compose.yml 最终版（补充 Redis 服务）
- 确保 .env.example 注入链路完整
- 创建 scripts/ 目录下的基础脚本（run_tests.sh / lint.sh / init_db.sh）
- 更新 README，docker-compose 作为第一启动方式

**PR #2 内容**：
- 将 pg_store.py 拆分为 database.py + trace_repository.py + session_repository.py
- 创建 repository.py ABC 接口定义
- 修改现有代码，使用新的 Repository 类
- 确保 171 个测试不回归

**PR #3 内容**：
- 创建 migrations/20260711_create_specs_table.sql 文件
- 基于 PR #2 建立的模式，创建 spec_repository.py
- 将 spec_store 的逻辑从内存迁移到数据库
- 补全 spec CRUD 测试

---

## 性能优化（贯穿各阶段）

### Prometheus 高基数
```
/user/123 → /user/{id}   # path 参数归一化
```

### 异步采集
```
用户请求 → Queue → 后台 worker 保存
```

### 存储管道
```
应用 → Kafka → Collector → ClickHouse/PostgreSQL → 查询
```

---

## GitHub 开源包装（同步做）

### README 升级
```
当前：AI Debug MCP Server
目标：AI-powered debugging infrastructure for modern AI coding agents
      Like Sentry + Cursor AI Debugging
```

### Demo 视频
```
1. 制造 Bug
2. AI 捕获
3. Cursor 调用 MCP
4. AI 定位原因
5. 生成修复代码
```

### 其他
- Docker 一键启动教程
- 接入 Cursor/Trae/Qoder 教程
- Benchmark 性能测试报告
- 和 Sentry 对比文档

---

## 技术债清单

| 技术债 | 当前状态 | 处置计划 |
|--------|----------|----------|
| ✅ spec_store 已迁移到 TraceStorage 工厂模式 | dict+Lock 主存 + add_log 持久化 + `_restore_from_storage()` 恢复 | ✅ 已完成（2026-07-19）|
| pg_store.py 职责混合 | 连接池管理 + SQL 操作混合 | 🟡 评估完成（2026-07-23）：有条件值得，推荐方案 C（补 ErrorStorage/SpecStorage ABC + pg_store/async_pg_store 同步拆 + 统一 _execute_with_retry 覆盖，2-2.5 人日，需 AI_RULES 审批）；方案 A（提 DDL 到 pg_schema.py）零风险试水。发现隐藏缺陷：读取路径无重连重试。详见 [ROADMAP.md](./ROADMAP.md) 技术债务 |
| SQLAlchemy + Alembic 未引入 | 裸 SQL 管理 | Phase 1 P5 评估引入 |
| errors/specs/network_records/ui_events 表未建 | 仅 traces/sessions 已建 | 按需创建迁移 SQL |
| asyncpg 异步写入 | 同步 psycopg2 | Phase 2 评估 |
| Redis Queue 异步队列 | 同步阻塞写入 | 触发条件：延迟 > 50ms 或 > 100QPS |

---

## 演进方向

### 近期（Phase 1.x 工程化增强）

1. Browser SDK 自动采集 — 浏览器端错误/网络/UI 事件自动进入 Trace 系统
2. SSE 实时 Dashboard — Trace 实时推送
3. Docker Compose 完善 — 一键启动完整开发环境
4. LLM Root Cause Analysis 增强 — 错误类型识别 + 根因分类

### 中期（Phase 2-3）

1. OpenTelemetry 分布式链路追踪
2. 智能错误分析引擎（错误指纹 + 根因排序）

### 远期（Phase 4-6）

1. ~~向量检索版 RAG 知识库系统（Qdrant 向量数据库）~~ ✅ 已完成（2026-07-26，`QdrantVectorStore` + OpenAI/智谱 Embeddings 语义召回 + uuid5 幂等 upsert + 静默降级）
2. AI Debug Agent（自动定位 + 生成修复 + 运行测试）
3. 自动修复平台（Git Agent + Test Agent + Security Agent）

---

## 附录：数据库技术栈总览

| 阶段 | 数据库组件 | 技术 | 用途 | 状态 |
|------|-----------|------|------|------|
| Phase 1 | 主关系型数据库 | PostgreSQL 16 | trace / session / spec / error 持久化 | ✅ 已部分实现 |
| Phase 1 | 缓存 / 限流 / 队列 | Redis 7 | 限流计数、会话缓存、异步任务队列 | ✅ 已实现 |
| Phase 4 | 向量数据库 | Qdrant | Bug embedding 存储 + 语义检索 | ✅ 已实现（`app/llm/qdrant_vector_store.py`，2026-07-26） |
| Phase 2+（可选） | 时序分析数据库 | ClickHouse | 高基数 trace 查询、性能指标聚合 | 🔲 待评估（trace > 1000 万/天时）|

**当前差距**：
- ✅ PostgreSQL traces/sessions 表已实现
- 🔲 PostgreSQL errors/specs/network_records/ui_events 独立表待建（当前通过 traces 表 JSONB data + step 字段区分实现持久化）
- ✅ spec_store 已迁移到 TraceStorage 工厂模式（2026-07-19）
- 🔲 SQLAlchemy 2.0 + Alembic 待引入（Repository 层前置依赖）

---

## 五维代码评估报告（2026-07-20）

> 评估范围：`app/` 全部核心模块（middleware / api / mcp / llm / config / main）
> 评估维度：①安全性 ②权限控制 ③数据流动 ④多并发处理 ⑤代码逻辑路径
> 评估方法：静态代码审查 + 架构文档对照

### 评估总览

| 维度 | 已完成项 | 待改进项 | 状态 |
|------|---------|---------|------|
| ① 安全性 | 11 | 1 | 🟢 P0/P1 已全部修复，P2 仅 CSRF（低风险）+ HTTPS（前置代理）|
| ② 权限控制 | 6 | 4 | 🟡 无 RBAC（P3 长期）|
| ③ 数据流动 | 6 | 2 | 🟢 P1 已修复，P2 仅 2 项 |
| ④ 多并发处理 | 6 | 3 | 🟢 P1/P2 已修复，P3 连接池调优 |
| ⑤ 代码逻辑路径 | 5 | 3 | 🟢 P0/P1 已修复，P2 可读性 + 废弃 API 已修 |

**整体结论**：项目安全基线扎实（fail-closed 鉴权 + 参数化 SQL + 入库前脱敏 + LLM 发送前脱敏），P0/P1/P2 问题已于 2026-07-20 修复（340 passed / 6 skipped / 0 failed）。

> ⚠️ **订正（2026-07-22 联合复核）**：本「五维评估」完成于 2026-07-20，**早于**后续数据流+安全复核，未覆盖两个高危项——**LFI（SEC-01）** 与 **SSRF（SEC-02）**，也未修正“默认免鉴权”“无会话隔离”的定位。下文若干“✅ 已修复”与当前代码不符（见逐条订正）。完整补充见 [SECURITY_REVIEW.md](./SECURITY_REVIEW.md) SEC-01~15。

---

### ① 安全性评估

#### 已完成 ✅

| 项 | 实现位置 | 说明 |
|----|---------|------|
| SQL 注入防护 | [pg_store.py](../../app/mcp/core/storage/pg_store.py) 全文 | 所有 SQL 使用参数化 `%s`，无字符串拼接 |
| fail-closed 鉴权 | [middleware.py:39-52](../../app/middleware.py#L39-L52) | 无 Key 且 `api_key` 已设 → 401；使用 `hmac.compare_digest` 恒定时间比较 |
| IP 限流 | [middleware.py:95-108](../../app/middleware.py#L95-L108) | `state_store.allow()` 按客户端 IP 计数，异常降级放行 |
| 请求体大小限制 | [middleware.py:56-78](../../app/middleware.py#L56-L78) | Content-Length 硬检查 → 413，防 OOM/DoS |
| 安全响应头 | [middleware.py:82-91](../../app/middleware.py#L82-L91) | X-Content-Type-Options / X-Frame-Options / Referrer-Policy / X-XSS-Protection |
| 入库前脱敏 | [redaction.py](../../app/mcp/core/redaction.py) + [trace_repo.py](../../app/mcp/core/trace_repo.py) | password/token/api_key/手机号统一掩码，fail-safe 默认开启 |
| LLM 发送前脱敏 | [analyzer.py:74-96](../../app/llm/analyzer.py#L74-L96) | `_redact_value_for_llm` 递归脱敏，防止敏感数据外发 LLM |
| CORS 安全 | [middleware.py:149-163](../../app/middleware.py#L149-L163) | `*` 时强制 `allow_credentials=False`，符合规范 |
| Git 路径白名单 | [git.py:25-33](../../app/mcp/core/git.py#L25-L33) | 防止通过任意路径探测其他 git 仓库内容 |
| 启动校验 | [main.py:33-41](../../app/main.py#L33-L41) | 拒绝 `0.0.0.0` + 无 API_KEY 的危险启动 |
| LLM 输出校验 | [analyzer.py:213-259](../../app/llm/analyzer.py#L213-L259) | schema 校验 + 字段截断 + fallback，防 LLM 原始输出泄露 |

#### 待改进 🔲

| 优先级 | 问题 | 位置 | 说明 |
|--------|------|------|------|
| **P0** | `PARSE_ERROR` 未导入 → NameError | [mcp_routes.py:47](../../app/api/mcp_routes.py#L47) | ✅ 已修复（2026-07-20）：补 import |
| **P0** | JSON 解析错误信息外泄 | [mcp_routes.py:47](../../app/api/mcp_routes.py#L47) | ❌ **订正（2026-07-22）：未修复**——`mcp_routes.py:47` 仍为 `f"无效 JSON: {e}"`，异常细节仍外泄（低危）。|
| P2 | 无 CSRF 防护 | 全局 | API 使用 Bearer Token 鉴权（非 Cookie），CSRF 风险低；如未来支持 Cookie 鉴权需补 |
| P2 | 无 HTTPS 强制 | 全局 | 依赖前置代理（DESIGN.md §9 已说明），未在应用层强制 |

---

### ② 权限控制评估

#### 已完成 ✅

| 项 | 实现位置 | 说明 |
|----|---------|------|
| fail-closed 鉴权 | [middleware.py:19-52](../../app/middleware.py#L19-L52) | 无 API_KEY 且已设 → 401；未设 `api_key` 则整体禁用（启动告警）|
| 公开路径白名单 | [middleware.py:20](../../app/middleware.py#L20) | `PUBLIC_PATHS=("/", "/health", "/demo", "/demo/silent-failure", "/ai-debug.js")` 免鉴权（5 项，`/metrics` 不在内需鉴权 — SEC-08） |
| 恒定时间比较 | [middleware.py:49](../../app/middleware.py#L49) | `hmac.compare_digest` 防时序攻击 |
| 启动校验 | [main.py:33-41](../../app/main.py#L33-L41) | `0.0.0.0` + 无 API_KEY 拒绝启动 |
| MCP 会话校验 | [mcp_routes.py:54-77](../../app/api/mcp_routes.py#L54-L77) | 未初始化会话访问 `tools/*` 被拒（400）|
| /ingest/* 统一鉴权 | [ingest.py](../../app/api/ingest.py) 全文 | 所有上报端点依赖 AuthMiddleware 兜底，不重复实现 |

#### 待改进 🔲

| 优先级 | 问题 | 说明 |
|--------|------|------|
| P2 | 无 RBAC 角色分级 | 所有合法 API_KEY 权限相同，无法区分读/写/删除权限 |
| P2 | 无 API_KEY 轮换机制 | 单一 API_KEY，无多 key 管理/过期/撤销 |
| P2 | 无操作级权限控制 | 如 `/api/spec DELETE` 与 `GET` 权限相同 |
| P3 | `/debug/echo` `/debug/token` 调试端点 | [debug.py:230-239](../../app/api/debug.py#L230-L239) 生产环境建议移除或加独立鉴权 |

---

### ③ 数据流动评估

#### 已完成 ✅

| 项 | 实现位置 | 说明 |
|----|---------|------|
| 输入校验 | [schemas/](../../app/schemas/) + Pydantic | 请求体经 Pydantic schemas 校验 |
| 入库前脱敏 | [trace_repo.py](../../app/mcp/core/trace_repo.py) + [redaction.py](../../app/mcp/core/redaction.py) | url/body/payload/message/frames 统一脱敏 |
| LLM 发送前脱敏 | [analyzer.py:74-96](../../app/llm/analyzer.py#L74-L96) | `_prepare_context_for_llm` 截断 + 递归脱敏 |
| 上下文截断 | [analyzer.py:125-181](../../app/llm/analyzer.py#L125-L181) | `truncate_context` 防 token 超限，超长标记 `_truncated` |
| 错误信息收敛 | [debug.py](../../app/api/debug.py) + [ingest.py](../../app/api/ingest.py) | 所有路由层 try/except 完整，错误信息不外泄 |
| 双传输一致 | [tools/__init__.py](../../app/mcp/tools/__init__.py) + [mcp_server.py](../../app/mcp_server.py) | HTTP / stdio 复用同一批 handler，业务逻辑不重复 |

#### 待改进 🔲

| 优先级 | 问题 | 位置 | 说明 |
|--------|------|------|------|
| **P1** | `save_trace` 多次写入非原子 | [trace_repo.py:102-136](../../app/mcp/core/trace_repo.py#L102-L136) | errors 缓冲 + trace_data + trace_meta + trace_link 四次写入，中途失败会数据不一致（已有 try/except 保护，不阻断主流程）|
| **P1** | `spec_store.update()` delete + add 非原子 | [spec_store.py:117-122](../../app/mcp/verifier/spec_store.py#L117-L122) | 先 `delete_logs` 再 `add_log`，并发 update 可能丢失数据 |
| P2 | `debug.py:67` fallback `str(error_info)` | [debug.py:67](../../app/api/debug.py#L67) | 异常 fallback 时 `str(error_info)` 可能泄露内部数据结构 |
| P2 | `mcp_routes.py:47` JSON 解析错误外泄 | [mcp_routes.py:47](../../app/api/mcp_routes.py#L47) | `f"无效 JSON: {e}"` 异常细节返回客户端 |

---

### ④ 多并发处理评估

#### 已完成 ✅

| 项 | 实现位置 | 说明 |
|----|---------|------|
| PG 连接池 | [pg_store.py:40-60](../../app/mcp/core/storage/pg_store.py#L40-L60) | `ThreadedConnectionPool` 线程安全，double-check locking 正确 |
| `_ensure_init` 并发安全 | [pg_store.py:100-118](../../app/mcp/core/storage/pg_store.py#L100-L118) | double-check locking + `_initialized` 标志 |
| `spec_store` dict + Lock | [spec_store.py:22-23](../../app/mcp/verifier/spec_store.py#L22-L23) | `_specs` dict + `_lock` 保护读写 |
| `errors` deque + Lock | [errors.py:21-22](../../app/mcp/core/errors.py#L21-L22) | `_recent` deque + `_lock` 保护，maxlen=200 自动丢弃 |
| `session registry` dict + Lock | [session.py:21-23](../../app/mcp/transports/session.py#L21-L23) | `_sessions` dict + `_lock` 保护 |
| PG 重试机制 | [pg_store.py:122-148](../../app/mcp/core/storage/pg_store.py#L122-L148) | `_execute_with_retry` 捕获 `OperationalError` 自动重连重试 |

#### 待改进 🔲

| 优先级 | 问题 | 位置 | 说明 |
|--------|------|------|------|
| **P1** | `spec_store.list_specs()` 持锁做 IO | [spec_store.py:146-148](../../app/mcp/verifier/spec_store.py#L146-L148) | ✅ 已修复（2026-07-20）：IO 移到锁外 |
| **P1** | `_restore_from_storage()` N+1 查询 | [spec_store.py:41-43](../../app/mcp/verifier/spec_store.py#L41-L43) | ✅ 已修复（2026-07-20）：优化为锁外恢复 |
| P2 | `analyzer._get_client()` 全局单例无锁 | [analyzer.py:42-59](../../app/llm/analyzer.py#L42-L59) | ✅ 已修复（2026-07-20）：加 spin-lock |
| P2 | `session.registry.get()` 返回引用 | [session.py:32-37](../../app/mcp/transports/session.py#L32-L37) | ✅ 已修复（2026-07-20）：返回 copy |
| P2 | `redaction._load_extra_rules()` 缓存无锁 | [redaction.py:59-79](../../app/mcp/core/redaction.py#L59-L79) | ✅ 已修复（2026-07-20）：加 double-check locking |
| P2 | `spec_store.update()` 非原子 | [spec_store.py:117-122](../../app/mcp/verifier/spec_store.py#L117-L122) | ✅ 已修复（2026-07-20）：先写后删 |
| P3 | PG 连接池 maxconn=10 | [pg_store.py:47-48](../../app/mcp/core/storage/pg_store.py#L47-L48) | 高并发（>10 并发写入）下可能排队，可按负载调优 |

---

### ⑤ 代码逻辑路径评估

#### 已完成 ✅

| 项 | 实现位置 | 说明 |
|----|---------|------|
| 路由层 try/except 完整 | [debug.py](../../app/api/debug.py) + [ingest.py](../../app/api/ingest.py) + [spec.py](../../app/api/spec.py) | 所有端点异常捕获完整，统一返回 500/400/422 |
| 异常钩子自身保护 | [exception_hook.py:36-41](../../app/mcp/hooks/exception_hook.py#L36-L41) | 钩子内 try/except 包裹，绝不掩盖原始报错 |
| LLM 重试 + fallback | [analyzer.py:262-325](../../app/llm/analyzer.py#L262-L325) | 主模型失败 → fallback 模型 → RuntimeError |
| 降级策略 | 各采集器 | runtime/network/git 失败降级不阻断整体 |
| 幂等性 | [exception_hook.py:28-32](../../app/mcp/hooks/exception_hook.py#L28-L32) + [pg_store.py:100-118](../../app/mcp/core/storage/pg_store.py#L100-L118) | `install_global_hook` 幂等，PG `CREATE TABLE IF NOT EXISTS` |

#### 待改进 🔲

| 优先级 | 问题 | 位置 | 说明 |
|--------|------|------|------|
| **P0** | `PARSE_ERROR` 未导入 → NameError | [mcp_routes.py:47](../../app/api/mcp_routes.py#L47) | ✅ 已修复（2026-07-20）：补 import |
| **P1** | 错误码误用 | [mcp_routes.py:84](../../app/api/mcp_routes.py#L84) | ✅ 已修复（2026-07-20）：`INVALID_REQUEST` → `INTERNAL_ERROR` |
| P2 | `initialize` 分支逻辑可读性差 | [mcp_routes.py:54-55](../../app/api/mcp_routes.py#L54-L55) | 三元表达式嵌套 `registry.create() if not session_id or not registry.get(session_id) else registry.get(session_id)`，可读性差且重复调用 `registry.get` |
| P2 | `asyncio.get_event_loop()` 已废弃 | [exception_hook.py:58](../../app/mcp/hooks/exception_hook.py#L58) + [exception_hook.py:86](../../app/mcp/hooks/exception_hook.py#L86) | ✅ 已修复（2026-07-20）：改用 `get_running_loop()` |
| P2 | `/health` PG 检查未 commit/rollback | [main.py:135-140](../../app/main.py#L135-L140) | ✅ 已修复（2026-07-20）：显式 `conn.commit()` + `try/finally` |
| P3 | `redaction.py:51` 手机号正则 `\b` 失效 | [redaction.py:51](../../app/mcp/core/redaction.py#L51) | ✅ 已修复（2026-07-20）：改用 `(?<!\d)...(?!\d)` |

---

### 改进优先级汇总

#### P0 — 必修 ✅ 已全部修复（2026-07-20）

1. **mcp_routes.py:47** `PARSE_ERROR` 未导入 → ✅ 补 import
2. **mcp_routes.py:47** JSON 解析错误信息外泄 → ❌ **订正：未修复**，仍为 `f"无效 JSON: {e}"`

#### P1 — 重要 ✅ 已全部修复（2026-07-20）

3. **mcp_routes.py:84** 错误码误用 → ✅ `INVALID_REQUEST` 改为 `INTERNAL_ERROR`
4. **spec_store.py:146-148** 持锁做 IO → ✅ 拆分为锁外预恢复 + 锁内读取
5. **spec_store.py:41-43** N+1 查询 → ✅ 优化为锁外恢复
6. **trace_repo.py:102-136** 多次写入非原子 → ✅ 已标注"最终一致"

#### P2 — 改进 ✅ 已全部修复（2026-07-20）

7. **analyzer.py:42-59** `_get_client` 无锁 → ⚠️ **订正：伪修复**——`_client_lock` 是模块级 bool（`analyzer.py:18`），检查-置位非原子，并非线程安全（`import threading` 却未用 `Lock`）；仍有竞态（最坏重复建客户端）。建议改真 `threading.Lock`。
8. **session.py:32-37** `registry.get` 返回引用 → ✅ 返回 `copy.copy(s)` 副本
9. **spec_store.py:117-122** update 非原子 → ✅ 先写后删
10. **exception_hook.py:58,86** `asyncio.get_event_loop()` 废弃 → ✅ 改用 `get_running_loop()`
11. **main.py:135-140** `/health` PG 检查 → ✅ 显式 `conn.commit()` + `try/finally`
12. **redaction.py:51** 手机号正则 → ✅ 改用 `(?<!\d)1[3-9]\d{9}(?!\d)`

#### P3 — 长期（架构演进）

13. **RBAC 角色分级** — 多 API_KEY + 权限分级
14. **API_KEY 轮换** — 多 key 管理 + 过期机制
15. **PG 连接池调优** — 按并发量调整 maxconn
16. **CSRF 防护** — 如未来支持 Cookie 鉴权

---

### 评估结论

**强项**：
- 安全基线扎实：fail-closed 鉴权 + 参数化 SQL + 入库前脱敏 + LLM 发送前脱敏
- 异常处理完整：所有路由层 try/except + 异常钩子自身保护
- 并发基础正确：PG 连接池 + double-check locking + dict+Lock

**弱项**：
- 1 个 P0 NameError bug 需立即修复（mcp_routes.py `PARSE_ERROR` 未导入）
- spec_store 持锁做 IO + N+1 查询，高并发下性能瓶颈
- 缺乏 RBAC，所有合法 API_KEY 权限相同
- 部分非原子操作（save_trace / spec_store.update）可能数据不一致

**建议下一步**：
1. 立即修复 P0（mcp_routes.py 两处）
2. 评估 P1 是否阻塞 v0.3.0 发布（建议修复后再发布）
3. P2/P3 纳入后续 Sprint

> 评估人：AI 代码审查 Agent
> 评估日期：2026-07-20
> 评估范围：v0.3.0 Release Audit 收口后全量代码

---

## 企业级架构综合评审（2026-07-22 高级架构师 / 高级代码审查员）

> 评审范围：项目全部核心源码（40+ 文件，约 5000 行），逐行阅读
> 评审维度：异步处理、缓存机制、请求分流、高并发数据预防
> 设计分析详见 [DESIGN.md](./DESIGN.md) §14

### 综合评分

| 维度 | 评分 | 说明 |
|------|------|------|
| **异步处理** | 6/10 | FastAPI async 基础好，但 PG/LLM/Redis 同步操作是瓶颈 |
| **缓存机制** | 5/10 | 有基础缓存（spec/redaction/linecache），缺 LLM 和 Dashboard 缓存 |
| **请求分流** | 7/10 | 路径/方法/后端分流完善，缺端点级限流和优先级队列 |
| **高并发预防** | 6/10 | 连接池/限流/清理机制完善，但池太小、内存存储不适合多 worker |
| **数据安全** | 8/10 | 脱敏/白名单/SSRF 防护/fail-closed 鉴权完善 |
| **可观测性** | 7/10 | Prometheus 指标 + JSON 日志 + trace_id 贯穿，缺连接池和 LLM 指标 |
| **可靠性** | 7/10 | 异常处理/优雅关闭/重试机制完善，缺降级策略 |

**总体评分：6.6/10** — 架构设计扎实，安全与可观测性达到企业级标准，但在高并发数据处理方面存在明确的优化空间。

### 9 个关键问题清单

| 编号 | 严重性 | 问题 | 位置 | 影响 |
|------|--------|------|------|------|
| R-01 | 🔴 | PostgreSQL 操作完全同步 | `pg_store.py:40-60` | 阻塞事件循环，maxconn=10 限制并发 |
| R-02 | 🔴 | LLM 调用同步阻塞 2-10s | `analyzer.py:276-339` | 线程池耗尽 |
| R-03 | 🔴 | 无 LLM 结果缓存 | `analyzer.py:342-376` | 同类错误重复分析，浪费 token |
| R-04 | 🔴 | Dashboard API 无查询缓存 | `dashboard.py:124-143` | 每次全量扫描+排序 |
| R-05 | 🔴 | PG 连接池不可配置 | `pg_store.py:46-48` | 硬编码 maxconn=10 |
| R-06 | 🔴 | 内存存储多 worker 数据隔离 | `memory_store.py:11-14` | gunicorn 多 worker 数据不一致 |
| R-07 | 🔴 | 定时清理无分布式锁 | `main.py:77-86` | 多 worker 重复执行 |
| R-08 | 🟡 | 异常聚合数据仅内存 | `errors.py:21-22` | occurrence_count 重启丢失 |
| R-09 | 🟡 | spec_store 恢复性能差 | `spec_store.py:31-59` | 500 次 PG 查询 |

### 三阶段优化路线图

#### Phase 1：短期优化（1-2 周，低风险高收益）

| 编号 | 改进项 | 具体操作 | 修改文件 | 验证方式 |
|------|--------|----------|----------|----------|
| P1-1 | PG 连接池可配置化 | `config.py` 新增 `pg_max_connections: int = 20`；`pg_store.py` 从 `settings.pg_max_connections` 读取 | `app/config.py`, `app/mcp/core/storage/pg_store.py` | 单元测试：验证配置生效 |
| P1-2 | LLM 分析结果缓存 | `analyzer.py` 新增 `_analysis_cache: dict`（LRU 100 条），按 `fingerprint` 缓存，TTL 1h | `app/llm/analyzer.py` | 单元测试：验证缓存命中/过期 |
| P1-3 | 端点级限流 | `middleware.py` 新增 `_ENDPOINT_LIMITS` 字典，`/ingest/*` 配额 120/min，`/analyze` 配额 10/min | `app/middleware.py` | 集成测试：验证不同端点不同限流 |
| P1-4 | Dashboard 查询缓存 | `dashboard.py` 新增模块级 `_cache` dict，TTL 30s，`_collect_all_traces` 命中时直接返回 | `app/api/dashboard.py` | 集成测试：验证缓存命中 |

#### Phase 2：中期优化（1 个月，中等风险中等收益）

| 编号 | 改进项 | 具体操作 | 修改文件 | 验证方式 |
|------|--------|----------|----------|----------|
| P2-1 | PG 异步化 | 引入 `asyncpg`，`PGTraceStore`/`PGSessionStore` 所有方法改为 `async def`，`logs.py` 改为异步 | `app/mcp/core/storage/pg_store.py`, `app/mcp/core/logs.py` | 集成测试 + 压测（wrk 100 并发） |
| P2-2 | LLM 调用异步化 | `debug_analyze` 改为 `async def`，`_retry_call` 改用 `httpx.AsyncClient` | `app/llm/analyzer.py`, `app/api/debug.py` | 集成测试 + 压测 |
| P2-3 | 异常聚合持久化 | 新增 `error_stats` PG 表（fingerprint, occurrence_count, first_seen, last_seen），`errors.record()` 同时写入 | `app/mcp/core/errors.py`, `app/mcp/core/storage/pg_store.py` | 单测 + 重启后验证数据恢复 |
| P2-4 | spec_store 独立表 | 新增 `specs` PG 表（id, kind, target, expect, created_at, updated_at），替代从 traces 扫描恢复 | `app/mcp/verifier/spec_store.py` | 单测 + 验证 CRUD |
| P2-5 | 滑动窗口限流 | 替换固定窗口为 Redis ZSET 滑动窗口，消除临界点突发 | `app/state/store.py` | 压测：验证窗口临界点不超标 |

#### Phase 3：长期优化（3 个月，架构升级）

| 编号 | 改进项 | 具体操作 | 修改文件 | 验证方式 |
|------|--------|----------|----------|----------|
| P3-1 | 数据分区 | ✅ 已完成（2026-07-24）：traces 表按月 RANGE 分区（PostgreSQL 声明式分区，非 pg_partman），自动预创建当月及未来 N 个月分区，惰性检查（每 1000 次写入），配置项 `pg_partition_enabled` 默认关闭 | `app/mcp/core/storage/pg_store.py`, `app/mcp/core/storage/async_pg_store.py`, `app/config.py` | 单元测试（6 用例）验证分区命名、时间范围计算、sync/async 一致性 |
| P3-2 | 归档策略 | ✅ 已完成（2026-07-24）：新增 `traces_archive` 表，`cleanup_expired` 先归档再删除（CTE `WITH moved AS DELETE...RETURNING` 原子移动），配置项 `pg_archive_enabled` 默认关闭 | `app/mcp/core/storage/pg_store.py`, `app/mcp/core/storage/async_pg_store.py`, `app/config.py` | 单元测试（5 用例）验证归档 SQL 调用、分区惰性检查频率 |
| P3-3 | 批量写入 | ✅ 已完成（2026-07-24）：storage ABC 新增 `save_entries` 默认实现 + MemoryTraceStore 覆写（单次锁）+ logs `add_logs_batch` + trace_repo save_trace 复用（META+LINK 批量，DATA 保留提交标记） | `app/mcp/core/trace_repo.py`, `app/mcp/core/storage/*.py` | 单元测试验证批量写入行为 |
| P3-4 | **OpenTelemetry** | ✅ 已完成（2026-07-24）：双模式设计——保留 `/metrics` Prometheus 文本端点向后兼容，同时引入 OTel SDK 支持 OTLP gRPC 导出；核心指标：`http_requests_total`、`http_errors_total`、`http_request_duration_seconds`；惰性初始化 + 失败降级；优雅关闭 | `app/config.py`, `app/observability.py`, `app/main.py`, `tests/unit/test_otel.py`, `requirements.txt`, `.env.example` | 单元测试（12 用例）验证 OTel 初始化、降级、中间件集成、向后兼容性 |
| P3-5 | 优雅降级 | ✅ 已完成（2026-07-24）：PG 不可用时自动降级到内存存储，配置项 `storage_fallback_to_memory` 控制，支持 fail-fast 模式 | `app/mcp/core/storage/factory.py`, `app/config.py` | 单元测试验证降级逻辑 |
| P3-6 | 消息队列削峰 | ✅ 已完成（2026-07-25）：有界 `asyncio.Queue(maxsize=N)` + K 常驻消费协程 + `asyncio.Semaphore(K)` 对齐 LLM RPM/TPM；队列满返回 429；优雅停机 drain；零侵入 analyzer.py | `app/llm/analysis_queue.py`, `app/api/debug.py`, `app/main.py` | 单元测试验证削峰与 drain 行为 |
| P3-7 | 多级缓存 | ✅ 已完成：L1 进程级 LRU + L2 Redis + L3 缓存预热（`app/llm/cache_prewarm.py`，2026-07-26，只写 L1 不刷新 L2 TTL），防穿透/雪崩/击穿 | `app/cache.py` + `app/llm/cache_prewarm.py` | 压测：验证缓存命中率 |
| P3-8 | 熔断器 | ✅ 已完成（2026-07-24）：LLM/PG 调用加 pybreaker 熔断，配置项控制熔断参数，熔断时返回结构化 fallback | `app/llm/analyzer.py`, `app/config.py` | 单元测试验证熔断行为 |

### 适用规模评估

| 阶段 | 日活 | 并发 | 部署方式 |
|------|------|------|----------|
| 当前版本 | <1,000 | <50 | 单 worker / Docker Compose |
| Phase 1 完成后 | <3,000 | <100 | 单 worker / Docker Compose |
| Phase 2 完成后 | <10,000 | <500 | 多 worker + Redis |
| Phase 3 完成后 | >10,000 | >500 | K8s 集群 + 分区 + 缓存 |

> 评审人：高级架构师 / 高级代码审查员
> 评审日期：2026-07-22
> 评审范围：全量代码逐行审查（40+ 文件）
> 参考架构：《无人机巡检平台高并发与架构优化技术术语手册》（令牌桶/消息队列/多级缓存/熔断器映射见 [DESIGN.md](./DESIGN.md) §14.6）
