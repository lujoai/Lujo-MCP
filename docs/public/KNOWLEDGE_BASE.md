# Lujo-MCP 知识库：经验积累与置信度进化

> Lujo-MCP 不只是「看到 Bug 现场」——它把每次调试的结论**沉淀为可复用的经验**，并且这些经验**跨重启保留、越验证越可信、团队共享**。这是 Lujo-MCP 与常见无状态 MCP 调试工具的本质差异。

## 与其他 MCP Server 的本质区别

MCP 服务器按「记忆能力」分三档：

| 档位 | 代表 | 存储 | 重启后 | 多人协作 |
|------|------|------|--------|----------|
| 无状态工具 | Playwright MCP、桌面自动化类 MCP | 无 | 一切从零开始 | 无经验概念 |
| 单人记忆 | 官方 `server-memory` | 本地 JSON 文件 | 保留（单机） | 不共享 |
| **共享知识库** | **Lujo-MCP** | **PostgreSQL `kb_entries` 表** | **保留（回灌最近条目）** | **连同一个库即共享** |

常见 MCP 自动化工具的价值是「代替人操作」，Lujo-MCP 的价值是「**积累调试经验、越用越准**」。

## 核心特性一：经验积累（Experience Accumulation）

### 写穿流水线

每次 AI 调试产生的结论都会实时落库（write-through），不等定时同步、不丢最后一刻的数据：

```
AI 调试完成
    │
    ▼
KnowledgeBaseStore.upsert()          ← 进程内主存（毫秒级命中）
    │  ├── analysis（根因分析，JSONB）
    │  ├── fix_suggestion（修复建议）
    │  └── fingerprint（错误指纹，主键去重）
    │
    ▼ 同步写穿
kb_entries 表（PostgreSQL）          ← 持久层（跨重启）
```

### 三级指纹检索

同一个 Bug 换了变量值、换了报错消息也能命中历史经验：

- **L1 精确指纹** —— 完全相同的错误直接命中
- **L1.5 归一化指纹** —— 去掉变量值后的「模式指纹」匹配（`IndexError: list index out of range` 与 `IndexError: list index out of range at line 42` 是同一模式）
- **L2 类型级 Jaccard** —— 同类型异常兜底召回

### 启动回灌

服务重启时自动把最近 `max_entries`（默认 100）条经验从 PG 加载回内存，正序插入保持 LRU 语义——不需要改 MCP 客户端任何配置，透明升级。

## 核心特性二：置信度进化（Confidence Evolution）

经验不是写完就定型的，每条经验带两个统计字段：

| 字段 | 含义 | 进化方式 |
|------|------|----------|
| `verify_count` | 验证次数 | 每次该经验的修复建议被验证成功 +1 |
| `case_confidence` | 置信度 | 只升不降（取历史最大值），越高越可信 |

```
首次调试    →  verify_count=0, confidence=0.0   （新经验，仅供参考）
验证通过 ×1 →  verify_count=1, confidence=0.7   （初步可信）
验证通过 ×3 →  verify_count=3, confidence=0.9   （高可信，优先复用）
```

AI 检索经验时可按置信度排序：**反复验证过的修复方案优先于未验证的猜测**，整个团队的知识库随使用时间单调变聪明。

## 数据库初始化

### 方式一：自动建表（推荐）

配置 `STORAGE_BACKEND=postgresql` 启动服务即可，`_ensure_init` 会自动执行建表（幂等，`IF NOT EXISTS`）：

```env
STORAGE_BACKEND=postgresql
PG_HOST=127.0.0.1
PG_PORT=5432
PG_DATABASE=lujo_mcp
PG_USER=postgres
PG_PASSWORD=你的密码
```

### 方式二：手动执行 SQL

完整建表语句（与仓库 `migrations/20260817_create_kb_entries_table.sql` 一致）：

```sql
-- kb_entries 表：RAG 知识库持久化（经验积累 + 置信度进化）
CREATE TABLE IF NOT EXISTS kb_entries (
    fingerprint            TEXT PRIMARY KEY,   -- 错误指纹（经验唯一标识）
    analysis               JSONB,              -- 根因分析（结构化）
    fix_suggestion         TEXT,               -- 修复建议
    source                 TEXT,               -- 经验来源（seed / llm / verify_loop）
    created_at             DOUBLE PRECISION NOT NULL,  -- 首次发现时间（epoch 秒）
    updated_at             DOUBLE PRECISION NOT NULL,  -- 最近更新时间（epoch 秒）
    normalized_fingerprint TEXT,               -- L1.5 归一化指纹（模式匹配）
    type_fingerprint       TEXT,               -- L2 类型指纹（同类型兜底）
    verify_count           INTEGER DEFAULT 0,  -- 验证次数（置信度进化）
    case_confidence        DOUBLE PRECISION DEFAULT 0  -- 置信度（只升不降）
);

-- 三级检索索引
CREATE INDEX IF NOT EXISTS idx_kb_entries_nfp ON kb_entries(normalized_fingerprint);
CREATE INDEX IF NOT EXISTS idx_kb_entries_tfp ON kb_entries(type_fingerprint);
-- 启动回灌索引（按最近更新取 Top-N）
CREATE INDEX IF NOT EXISTS idx_kb_entries_updated ON kb_entries(updated_at DESC);
```

psql 执行：

```powershell
$env:PGPASSWORD='你的密码'
psql -h 127.0.0.1 -U postgres -d lujo_mcp -f migrations/20260817_create_kb_entries_table.sql
```

### 方式三：Docker Compose 一键部署

```bash
PG_PASSWORD=你的密码 API_KEY=你的key docker-compose up -d
```

容器内自动建表（含 kb_entries），数据落在 `pgdata` 卷，重启不丢。PostgreSQL 连接排障见 [POSTGRESQL_FIX_GUIDE.md](./POSTGRESQL_FIX_GUIDE.md)。

## 部署模式

| 模式 | 做法 | 经验归属 |
|------|------|----------|
| 单人使用 | 本地/自带 PG | 自己的经验，重启保留 |
| 团队共享 | 多个服务实例连同一个 PG | **共享经验库**：任何人调试过的 Bug，所有人下次直接命中 |
| 多租户 SaaS | —— | 当前版本不含租户隔离（表无 `tenant_id` 字段），一个库内经验互通 |

## 可靠性设计

- **写穿失败不阻断调试**：PG 故障时仅记录 warning，KB 自动退回纯内存行为，主流程零影响
- **PG 初始化失败自动降级**：knowledge_store 降级为 no-op，服务照常启动
- **LRU 驱逐同步删除**：内存淘汰最久未用经验时，PG 行同步删除，两侧条数始终一致（≤ max_entries）
- **幂等迁移**：全部 `CREATE TABLE / INDEX IF NOT EXISTS`，重复执行安全
