# ai-debug-mcp 长期路线图

> 定位：项目长期演进路线图，汇总已完成阶段与待开发阶段的技术方向。
> 当前开发执行计划见 [DEV_PLAN.md](./DEV_PLAN.md)，技术架构设计见 [DESIGN.md](./DESIGN.md)，企业级架构综合评审见 [CODE_REVIEW.md](./CODE_REVIEW.md)。
> 最近更新：2026-07-23

---

## 已完成阶段

### Phase 0：项目标准化 ✅

- 版本单一来源（`app/__init__.py` 为唯一版本定义，全仓引用）
- 死代码清理（删除 `debug.py` 重复 schemas、冲突类重命名）
- 文档修正（README/DESIGN/DEV_PLAN 与代码实际状态对齐）

### Phase 1：短期优化 ✅

- PG 连接池可配置化（`pg_max_connections` 环境变量，默认 20）
- LLM 分析结果缓存（按 fingerprint 缓存，TTL 1h，LRU 100 条）
- 端点级限流（`/ingest/*` 120/min，`/analyze` 10/min）
- Dashboard 查询缓存（TTL 30s）
- SEC-12 中间件顺序修正、CORS 收紧、M8 chunked 请求体防护
- MemoryStore 容量上限（防 OOM）、periodic_cleanup 分布式锁、Redis 滑动窗口限流
- GitHub Actions CI（lint advisory + 单元测试门禁）

### Phase 2：P2 项收口 ✅

- M5 版本协商规范化、M13 pytest markers + mock 层
- 复合键名脱敏扩展（子串匹配 + 白名单 allowlist）
- errors 表持久化聚合（fingerprint + occurrence_count 落 PG）
- spec_store 独立表（消除 N+1 查询，不再从 traces 扫描恢复）
- PG 异步上下文 `to_thread` 过渡桥（同步驱动 + 事件循环兼容）
- SEC-04 会话隔离、SEC-08 `/metrics` 鉴权 toggle

### Phase 3：异步化根治 ✅

- PG 驱动 psycopg2 → asyncpg（feature flag 灰度，可回退）
- LLM 客户端 OpenAI → AsyncOpenAI（全链路 async/await）
- 多级缓存 L1（LRU 进程级）+ L2（Redis 分布式）

### Phase 4：Browser SDK V2 ✅

- 批量上报 + sendBeacon 降级 + 指数退避重试

---

## 待开发阶段

### Phase 5：数据层长期优化

| 编号 | 任务 | 说明 | 依赖 |
|------|------|------|------|
| P3-1 | traces 表按月分区 | 用 pg_partman 在线分区，需迁移历史数据 | Phase 3 异步化完成 |
| P3-2 | 归档策略 | >30 天自动归档到 traces_archive | P3-1 分区完成 |
| P3-3 | 批量写入 | executemany 替代单条 INSERT | 无 |
| P3-5 | 优雅降级 | PG 不可用时自动降级内存存储 | 无 |

### Phase 6：可观测性与可靠性

| 编号 | 任务 | 说明 | 依赖 |
|------|------|------|------|
| P3-4 | OpenTelemetry 集成 | 替换自研 Prometheus 指标，OTLP exporter，span 覆盖 PG/LLM/中间件 | Phase 3 异步化完成 |
| P3-6 | 消息队列削峰 | Celery + Redis broker，异步处理高写入 | 评估 broker 运维成本 |
| P3-7 | 多级缓存深化 | L1 LRU + L2 Redis 已落地，后续加 L3 预热 | 无 |
| P3-8 | 熔断器 | pybreaker，LLM/PG 调用熔断降级 | 无 |

### Phase 7：智能化

| 方向 | 说明 | 依赖 |
|------|------|------|
| 智能错误分析引擎 | 指纹聚合 + 根因排序 | errors 表持久化已落地 |
| RAG 知识库 | Qdrant 向量数据库，历史错误/修复方案检索 | 评估 Qdrant 运维成本 |
| AI Debug Agent | 自动修复，多 Agent 协同 | RAG 知识库 |

### Browser SDK 续作

| 版本 | 方向 | 说明 |
|------|------|------|
| V3 | 网络错误自动标记静默失败 | XHR/fetch error → 静默失败检测 |
| V4 | SDK 初始化追踪 + 请求关联 | trace_id 贯穿 SDK → 后端 |
| V5 | 增强 ingest 端点 | 批量 ingest、压缩传输 |
| V6 | 自动检测 UI 静默失败 | DOM 变化检测 + 断言 |

### 安全 follow-up

| 项 | 说明 | 优先级 |
|----|------|--------|
| C7 source-map 还原 | 前端 JS 场景需求，压缩堆栈解析 | 低 |
| 复合键名脱敏白名单调优 | 根据实际使用反馈调整 allowlist | 持续 |

---

## 技术债务

> 2026-07-23 清理：ruff 39 条 lint 违规已清零（F401/F841/E401 auto-fix + E402 noqa + 手动，CI 可转硬门禁）；`.env` UTF-8 BOM 已剥离；`test_api.py` 8 个鉴权 401 基线失败已修复（conftest env var 优先于 .env + `HOST=127.0.0.1` 避开 SEC-03）。`analyzer.py:108 冗余 import time` 经核实为过时条目（line 7 唯一 `import time` 且被使用，不存在冗余）。

> 2026-07-23 技术债清理（续）：`test_full_flow.py` 硬编码 PG 密码已修复（commit `ad6f8dd`，改为 `os.environ.setdefault('STORAGE_BACKEND','postgresql')` + PG 配置由 `.env` 经 `settings` 读取，不再硬编码凭据）；`pg_store.py` Repository 层拆分已完成只读评估，结论见下。

剩余技术债务：
- **`pg_store.py` 拆分**（评估完成，2026-07-23，未改代码）：598 行不算"上帝文件"，单纯减行数不值得拆。真正问题是**设计债**——`errors`/`specs` 表 CRUD 无 ABC 抽象（`upsert_error`/`save_spec`/`get_spec`/`list_specs_pg`/`delete_spec` 为 PG 专属模块级函数），导致 memory/pg/async_pg 三后端契约不对齐、`spec_store.py` 靠 try/except 降级。推荐"有条件值得"：拆分必须与补齐 `ErrorStorage`/`SpecStorage` ABC + 三后端同步对齐 + 统一 `_execute_with_retry` 覆盖**打包做**（方案 C，约 2-2.5 人日，需 `pg_store.py` + `async_pg_store.py` 同步拆，否则结构分叉）。纯文件搬运不批。
  - **隐藏缺陷**：`_execute_with_retry` 覆盖不一致——读取路径（`get_entries`/`list_request_ids`/`SessionStorage.get`/`list_active`/`get_spec`/`list_specs_pg`）走裸 `cursor.execute` + 手动 commit，无重连重试保护。拆分时应一并统一。
  - **零风险试水第一步**：方案 A，仅提取 4 个 DDL 常量到 `pg_schema.py`（58 行纯字符串、零逻辑耦合、不触碰连接池单例/测试 patch 站点），0.5-1 人时。
  - 流程：按 AI_RULES，启动完整拆分前需先提交"问题分析 + 影响范围 + 测试方案"等待审批，本次评估可作为审批材料。
- （已修复）`test_full_flow.py` 硬编码 PG 密码 → `ad6f8dd`（密码仍残留在 git 历史 commit 中，未推送远端，建议修改本地 PG 密码）
