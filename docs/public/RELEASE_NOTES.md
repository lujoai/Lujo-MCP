# Release Notes / 发布说明

> Post-release branch updates / 发布后主干增量：
> - Browser SDK 已继续补齐 V3 网络错误自动标记、V6 UI 静默失败自动检测
> - 调试分析链路已新增指纹知识库基础能力（命中优先 + 自动沉淀）
> - Dashboard 实时 SSE 推送（DASH-SSE-001，2026-07-30）：`DashboardEventBus` 广播总线 + SSE 端点 + 前端 EventSource
> - AI Debug Agent Phase 2 多 Agent DAG（AGENT-002，2026-07-30）：`RepairAgent` + `GitAgent`/`TestAgent`/`SecurityAgent` 并行审查
> - MCP 工具数增至 17（新增 `repair_async` / `repair_result`）
> - 测试基线：654 passed / 6 skipped / 0 failed
> - ⚠️ **beta-release 全量审查（2026-07-27）**：发现 P0×6 + P1×9 + P2×12 + 文档×5 = 32 项，阻断上线和开源。健康度 8.5/10 → 6.5/10。详见 `docs/internal/release/claude-audit-consolidated.md` §十一
> - 上述增量属于 `v0.3.0` 之后的主干演进，正式版本号以后续发版说明为准

**Version / 版本**: v0.3.0  
**Release Date / 发布日期**: 2026-07-25  
**Codename / 代号**: Stability & Production Ready

---

## 中文版本

### 📋 版本概述

v0.3.0 是 Lujo-MCP 项目的稳定性与生产就绪版本。本次发布重点完成了 MCP HTTP 流式通信闭环、稳定性验证收口、以及业务级 UI 验证能力增强，使项目从"代码已开发"阶段正式进入"可交付启用"状态。

### ✨ 新增功能

#### MCP 协议增强
- **MCP Streamable HTTP SSE 长连接** (`GET /mcp`)
  - 支持会话化订阅与消息推送消费
  - 实现 `notifications/session/ready` 推送
  - POST SSE 结果桥接到 GET 队列
  - DELETE 会话清理语义
  - 代码位置: `app/api/mcp_routes.py`, `app/mcp/transports/sse.py`

#### UI 验证能力增强
- **业务级 UI 场景验证**
  - 表单填写与提交验证 (`form` 断言)
  - 数据表格结构验证 (`data_table` 断言)
  - 数值范围验证 (`numeric_range` 断言)
  - 登录流程验证（组合现有功能）
  - 代码位置: `app/mcp/verifier/ui_runner.py`

#### 存储与数据优化
- **PostgreSQL 高级特性**
  - traces 表按月分区 (`PG_PARTITION_ENABLED=true`)
  - 数据归档策略 (`PG_ARCHIVE_ENABLED=true`)
  - asyncpg 异步存储 (`PG_ASYNC_ENABLED=true`)
  - 批量写入优化
  - 代码位置: `app/mcp/core/storage/pg_store.py`, `app/mcp/core/storage/async_pg_store.py`

#### 可观测性
- **OpenTelemetry 集成**
  - OTLP gRPC 指标导出
  - Prometheus `/metrics` 向后兼容端点
  - 代码位置: `app/observability.py`

- **熔断器机制**
  - LLM 调用熔断保护
  - PostgreSQL 连接熔断保护
  - 代码位置: `app/llm/analyzer.py`, `app/mcp/core/storage/pg_store.py`

#### 缓存优化
- **多级缓存架构**
  - L1 进程内 LRU 缓存（默认启用）
  - L2 Redis 分布式缓存（可选）
  - Dashboard 查询缓存
  - 代码位置: `app/llm/analyzer.py`, `app/api/dashboard.py`

### 🔧 功能优化

- **JSON-RPC 错误码规范化**
  - 区分 Parse Error (-32700) / Invalid Request (-32600) / Method Not Found (-32601)
  - 代码位置: `app/mcp/protocol/jsonrpc.py`

- **存储降级机制**
  - PostgreSQL 不可用时自动降级到 Memory Store
  - 由 `storage_fallback_to_memory` 配置控制
  - 代码位置: `app/mcp/core/storage/factory.py`

- **安全增强**
  - fail-closed 鉴权机制
  - 请求体大小限制（Content-Length + chunked）
  - IP / 端点级限流
  - 安全响应头默认启用
  - LFI / SSRF / URL 白名单防护
  - 代码位置: `app/middleware.py`, `app/mcp/verifier/ui_runner.py`

### 🐛 问题修复

- **M9 .env 未知键崩溃**
  - 修复 `pydantic-settings` 的 `extra_forbidden` 导致启动失败
  - 允许 `.env` 中存在多余键而不崩溃
  - 代码位置: `app/config.py`

- **SEC-13 非原子写入**
  - `spec_store.update()` 改为 crash-safe append
  - `trace_repo.save_trace()` 写入顺序优化
  - 代码位置: `app/mcp/verifier/spec_store.py`, `app/mcp/core/trace_repo.py`

- **M7 API_KEY 空串鉴权**
  - 空串/纯空白 `api_key` 归一化为 `None`
  - 代码位置: `app/config.py`

- **N3 stdio 关闭资源回收**
  - PG 连接池关闭
  - 后台任务取消
  - excepthook 卸载
  - 代码位置: `app/mcp_server.py`, `app/mcp/transports/stdio.py`

### ⚠️ 已知限制

1. **MCP server->client notifications**
   - 当前仅支持 `session/ready` 与 POST SSE 结果桥接
   - 更丰富的通知类型待扩展

2. **UI 验证环境依赖**
   - `verify_ui` 和 `auto_test` 需要 Playwright + Chromium
   - 需要目标页面环境可达

3. **分布式部署**
   - Redis 状态后端需要手动配置
   - 多实例限流共享需要 Redis 环境

4. **Docker 容器化**
   - Docker Compose 配置已提供
   - 容器化验证待环境支持（`STAB-007`）

### 🔄 兼容性说明

- **向后兼容**: v0.3.0 完全兼容 v0.2.x 的 API 与配置
- **配置迁移**: 无需迁移，新增配置项均有合理默认值
- **数据格式**: 存储格式无变化，可直接升级

### 📦 依赖版本要求

#### 核心依赖
```
fastapi>=0.115.0
uvicorn>=0.49.0
python-dotenv>=1.0.0
pydantic-settings>=2.0.0
openai>=1.0.0
psutil>=5.9.0
mcp>=1.0.0
httpx>=0.27.0
```

#### 存储依赖（可选）
```
psycopg2-binary>=2.9.0      # PostgreSQL 同步存储
asyncpg>=0.29.0             # PostgreSQL 异步存储
redis>=5.0.0                # Redis 缓存与状态后端
```

#### 可观测性依赖（可选）
```
pybreaker>=1.0.0            # 熔断器
opentelemetry-api>=1.20.0
opentelemetry-sdk>=1.20.0
opentelemetry-exporter-otlp-proto-grpc>=1.20.0
```

#### 开发依赖
```
pytest>=8.0.0
pytest-asyncio>=0.24.0
pytest-cov>=5.0.0
ruff>=0.8.0
```

### 📖 升级指引

#### 从 v0.2.x 升级到 v0.3.0

1. **备份现有配置**
   ```bash
   cp .env .env.backup
   ```

2. **拉取新版本**
   ```bash
   git pull origin main
   ```

3. **安装依赖**
   ```bash
   pip install -r requirements.txt
   ```

4. **检查配置（可选）**
   - 新增配置项均有默认值，无需手动配置
   - 如需启用高级功能，参考 `.env.example`

5. **启动服务**
   ```bash
   python -m app.main
   ```

6. **验证升级**
   ```bash
   # 运行单元测试
   pytest tests/unit/ -q
   
   # 检查服务健康状态
   curl http://localhost:8000/health
   ```

---

## English Version

### 📋 Release Overview

v0.3.0 is the Stability & Production Ready release of Lujo-MCP. This release focuses on completing the MCP HTTP streaming loop, stability verification convergence, and business-level UI verification capabilities, transitioning the project from "code developed" to "delivery-ready" status.

### ✨ New Features

#### MCP Protocol Enhancements
- **MCP Streamable HTTP SSE Long Connection** (`GET /mcp`)
  - Session-based subscription and message push consumption
  - `notifications/session/ready` push implementation
  - POST SSE result bridging to GET queue
  - DELETE session cleanup semantics
  - Location: `app/api/mcp_routes.py`, `app/mcp/transports/sse.py`

#### UI Verification Capabilities
- **Business-Level UI Scenario Verification**
  - Form filling and submission verification (`form` assertion)
  - Data table structure verification (`data_table` assertion)
  - Numeric range verification (`numeric_range` assertion)
  - Login flow verification (combining existing features)
  - Location: `app/mcp/verifier/ui_runner.py`

#### Storage & Data Optimization
- **PostgreSQL Advanced Features**
  - Monthly partitioning for traces table (`PG_PARTITION_ENABLED=true`)
  - Data archival strategy (`PG_ARCHIVE_ENABLED=true`)
  - asyncpg async storage (`PG_ASYNC_ENABLED=true`)
  - Batch write optimization
  - Location: `app/mcp/core/storage/pg_store.py`, `app/mcp/core/storage/async_pg_store.py`

#### Observability
- **OpenTelemetry Integration**
  - OTLP gRPC metrics export
  - Prometheus `/metrics` backward-compatible endpoint
  - Location: `app/observability.py`

- **Circuit Breaker Mechanism**
  - LLM call circuit breaker protection
  - PostgreSQL connection circuit breaker protection
  - Location: `app/llm/analyzer.py`, `app/mcp/core/storage/pg_store.py`

#### Cache Optimization
- **Multi-Level Cache Architecture**
  - L1 in-process LRU cache (enabled by default)
  - L2 Redis distributed cache (optional)
  - Dashboard query cache
  - Location: `app/llm/analyzer.py`, `app/api/dashboard.py`

### 🔧 Improvements

- **JSON-RPC Error Code Standardization**
  - Distinguish Parse Error (-32700) / Invalid Request (-32600) / Method Not Found (-32601)
  - Location: `app/mcp/protocol/jsonrpc.py`

- **Storage Fallback Mechanism**
  - Automatic fallback to Memory Store when PostgreSQL is unavailable
  - Controlled by `storage_fallback_to_memory` configuration
  - Location: `app/mcp/core/storage/factory.py`

- **Security Enhancements**
  - fail-closed authentication mechanism
  - Request body size limits (Content-Length + chunked)
  - IP / endpoint-level rate limiting
  - Security response headers enabled by default
  - LFI / SSRF / URL whitelist protection
  - Location: `app/middleware.py`, `app/mcp/verifier/ui_runner.py`

### 🐛 Bug Fixes

- **M9 .env Unknown Key Crash**
  - Fixed `pydantic-settings` `extra_forbidden` causing startup failure
  - Allows extra keys in `.env` without crashing
  - Location: `app/config.py`

- **SEC-13 Non-Atomic Writes**
  - `spec_store.update()` changed to crash-safe append
  - `trace_repo.save_trace()` write order optimization
  - Location: `app/mcp/verifier/spec_store.py`, `app/mcp/core/trace_repo.py`

- **M7 API_KEY Empty String Authentication**
  - Empty/whitespace-only `api_key` normalized to `None`
  - Location: `app/config.py`

- **N3 stdio Shutdown Resource Cleanup**
  - PG connection pool closure
  - Background task cancellation
  - excepthook uninstallation
  - Location: `app/mcp_server.py`, `app/mcp/transports/stdio.py`

### ⚠️ Known Limitations

1. **MCP server->client notifications**
   - Currently only supports `session/ready` and POST SSE result bridging
   - Richer notification types pending expansion

2. **UI Verification Environment Dependencies**
   - `verify_ui` and `auto_test` require Playwright + Chromium
   - Target page environment must be reachable

3. **Distributed Deployment**
   - Redis state backend requires manual configuration
   - Multi-instance rate limiting sharing requires Redis environment

4. **Docker Containerization**
   - Docker Compose configuration provided
   - Containerization verification pending environment support

### 🔄 Compatibility

- **Backward Compatible**: v0.3.0 is fully compatible with v0.2.x APIs and configurations
- **Configuration Migration**: No migration needed; new configuration items have reasonable defaults
- **Data Format**: Storage format unchanged; can upgrade directly

### 📦 Dependency Requirements

#### Core Dependencies
```
fastapi>=0.115.0
uvicorn>=0.49.0
python-dotenv>=1.0.0
pydantic-settings>=2.0.0
openai>=1.0.0
psutil>=5.9.0
mcp>=1.0.0
httpx>=0.27.0
```

#### Storage Dependencies (Optional)
```
psycopg2-binary>=2.9.0      # PostgreSQL sync storage
asyncpg>=0.29.0             # PostgreSQL async storage
redis>=5.0.0                # Redis cache and state backend
```

#### Observability Dependencies (Optional)
```
pybreaker>=1.0.0            # Circuit breaker
opentelemetry-api>=1.20.0
opentelemetry-sdk>=1.20.0
opentelemetry-exporter-otlp-proto-grpc>=1.20.0
```

#### Development Dependencies
```
pytest>=8.0.0
pytest-asyncio>=0.24.0
pytest-cov>=5.0.0
ruff>=0.8.0
```

### 📖 Upgrade Guide

#### Upgrading from v0.2.x to v0.3.0

1. **Backup Existing Configuration**
   ```bash
   cp .env .env.backup
   ```

2. **Pull New Version**
   ```bash
   git pull origin main
   ```

3. **Install Dependencies**
   ```bash
   pip install -r requirements.txt
   ```

4. **Check Configuration (Optional)**
   - New configuration items have defaults; no manual configuration needed
   - For advanced features, refer to `.env.example`

5. **Start Service**
   ```bash
   python -m app.main
   ```

6. **Verify Upgrade**
   ```bash
   # Run unit tests
   pytest tests/unit/ -q
   
   # Check service health
   curl http://localhost:8000/health
   ```

---

## 相关链接 / Related Links

- [完整功能交付矩阵 / Full Delivery Matrix](../internal/DELIVERY_MATRIX.md)
- [稳定性验证报告 / Stability Report](../internal/STABILITY_REPORT.md)
- [启动前检查清单 / Pre-flight Checklist](./PREFLIGHT_CHECKLIST.md)
- [异常排查指南 / Troubleshooting Guide](./TROUBLESHOOTING.md)
- [开发计划 / Development Plan](../internal/DEV_PLAN.md)
