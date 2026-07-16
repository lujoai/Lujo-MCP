# ai-debug-mcp 长期技术路线

> 定位：技术负责人 Roadmap，描述未来架构演进、Phase 规划、技术债和演进方向。
> 注意：本文件不记录当前开发任务，当前任务请见 [DEV_PLAN.md](./DEV_PLAN.md)。
> 来源：2026-07-10 架构师评审 + 产品迭代规划
> 最终定位：**AI 时代的软件可观测 + 自动调试基础设施**
> 核心原则：**先成为"优秀的数据采集系统"，再成为"聪明的 AI Debug 系统"**
> 没有高质量数据，AI 只是聊天机器人。

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
Phase 4  RAG 知识库系统（1 个月）
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

> **战略决策**：README 的"快速开始"部分，第一步是 `git clone`，第二步就是 `docker-compose up -d`。确保这个命令能一键拉起 PostgreSQL、Redis 和 App。

当前 docker-compose.yaml 已有 postgres + app，但**缺 Redis 服务**，需补全：

```yaml
# docker-compose.yaml 补充 Redis 服务
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
| MCP 工具数 | HTTP 15 / stdio 14 |
| 测试覆盖 | 当前测试状态以 [README.md](./README.md) 项目状态表为准 |
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
| `20260711_create_errors_table.sql` | errors 表 DDL | 🔲 待创建 |
| `20260711_create_specs_table.sql` | specs 表 DDL | 🔲 待创建 |
| `20260712_create_network_records_table.sql` | network_records 表 DDL | 🔲 待创建 |
| `20260712_create_ui_events_table.sql` | ui_events 表 DDL | 🔲 待创建 |

**migrations/ 工作流程**：

1. 每次需要修改数据库 Schema，在 migrations/ 目录下创建按时间戳命名的 SQL 文件
2. 文件内容就是标准的 `CREATE TABLE ...` 或 `ALTER TABLE ...` 语句
3. 使用 `scripts/init_db.sh` 脚本按顺序执行所有 SQL 文件
4. 开发环境：`docker-compose up` 时自动执行 `scripts/init_db.sh`

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
| spec_store 迁移到 PG 工厂模式 | 当前 spec_store 用 dict+Lock 主存 + add_log 备份，需改为复用 TraceStorage 工厂 | P0 |
| 引入 SQLAlchemy 2.0 + Alembic | 替换 pg_store.py 裸 SQL，管理 schema 变更 | P0（Repository 层前置依赖）|
| 创建 errors 表 + 指纹去重逻辑 | 支持 error_hash 聚合，避免重复刷屏 | P1 |
| 创建 network_records / ui_events 表 | 存储网络请求和前端事件采集数据 | P1 |

### Module 2：Repository 层

> **战略决策**：当前 [pg_store.py](./app/mcp/core/storage/pg_store.py) 同时承担连接池管理（`_get_pool`）和数据操作（`PGTraceStore`/`PGSessionStore`），职责混合。拆分为 `database.py`（连接池）+ `trace_repository.py` + `session_repository.py`，业务逻辑与 SQL 解耦。

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
| 向量数据库 | Qdrant | Bug embedding 存储 + 语义检索历史解决方案 | 🔲 待引入 |
| Python SDK | qdrant-client | 向量数据库操作 | 🔲 待引入 |
| Embedding 模型 | 智谱 embedding-2 / OpenAI text-embedding-3-small | 将 Bug 描述转为向量 | 🔲 待引入 |

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
| spec_store 未走 PG 工厂模式 | dict+Lock 主存 + add_log 备份 | Phase 1 P5 迁移到 PG |
| pg_store.py 职责混合 | 连接池管理 + SQL 操作混合 | Phase 1 P5 拆分为 database.py + repository |
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

1. RAG 知识库系统（Qdrant 向量数据库）
2. AI Debug Agent（自动定位 + 生成修复 + 运行测试）
3. 自动修复平台（Git Agent + Test Agent + Security Agent）

---

## 附录：数据库技术栈总览

| 阶段 | 数据库组件 | 技术 | 用途 | 状态 |
|------|-----------|------|------|------|
| Phase 1 | 主关系型数据库 | PostgreSQL 16 | trace / session / spec / error 持久化 | ✅ 已部分实现 |
| Phase 1 | 缓存 / 限流 / 队列 | Redis 7 | 限流计数、会话缓存、异步任务队列 | ✅ 已实现 |
| Phase 4 | 向量数据库 | Qdrant | Bug embedding 存储 + 语义检索 | 🔲 待引入 |
| Phase 2+（可选） | 时序分析数据库 | ClickHouse | 高基数 trace 查询、性能指标聚合 | 🔲 待评估（trace > 1000 万/天时）|

**当前差距**：
- ✅ PostgreSQL traces/sessions 表已实现
- 🔲 PostgreSQL errors/specs/network_records/ui_events 表待建
- 🔲 spec_store 未走 PG 工厂模式
- 🔲 SQLAlchemy 2.0 + Alembic 待引入（Repository 层前置依赖）
