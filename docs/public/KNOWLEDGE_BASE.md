# Lujo-MCP 知识库：经验积累与置信度进化

> **当前版本**。KB 自有经验默认写穿本地 SQLite「笔记本」（`KB_PERSIST_PATH`，默认工作目录 `lujo-kb.sqlite3`）——零安装、数据不出本机、进程重启自动回灌；`KB_PERSIST_ENABLED=false` 退回纯内存。`STORAGE_BACKEND=memory` 为唯一合法值（PostgreSQL 后端已正式移除，精确值 `postgresql` 会被启动即拒绝，见 [TROUBLESHOOTING.md L 节](./TROUBLESHOOTING.md#l-postgresql-后端移除说明--postgresql-removal)）。产品定位为单用户本地自用。
>
> Lujo-MCP 不只是「看到 Bug 现场」——它把每次调试的结论**沉淀为可复用的经验**，并且这些经验**跨重启保留、越验证越可信**。这是 Lujo-MCP 与常见无状态 MCP 调试工具的本质差异。

## 与其他 MCP Server 的本质区别

MCP 服务器按「记忆能力」分三档：

| 档位 | 代表 | 存储 | 重启后 | 多人协作 |
|------|------|------|--------|----------|
| 无状态工具 | Playwright MCP、桌面自动化类 MCP | 无 | 一切从零开始 | 无经验概念 |
| 单人记忆 | 官方 `server-memory` | 本地 JSON 文件 | 保留（单机） | 不共享 |
| **本地持久知识库** | **Lujo-MCP** | **memory 默认；SQLite KB 笔记本默认开启（唯一持久化路径）** | **运行现场在 memory 进程内；KB 经验由 SQLite 笔记本保留（回灌最近条目）** | **单用户本地使用** |

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
kb_entries（本地 SQLite 笔记本）← 持久层（跨重启，仅 KB 经验）
```

### 三级指纹检索

同一个 Bug 换了变量值、换了报错消息也能命中历史经验：

- **L1 精确指纹** —— 完全相同的错误直接命中
- **L1.5 归一化指纹** —— 去掉变量值后的「模式指纹」匹配（`IndexError: list index out of range` 与 `IndexError: list index out of range at line 42` 是同一模式）
- **L2 类型级 Jaccard** —— 同类型异常兜底召回

### 启动回灌

服务重启时按 `updated_at` 从持久层加载最近 `max_entries`（默认 100）条经验，再按更新时间正序插入内存：最久未更新的条目位于队首。进程内按访问顺序调整，重启不保留上次的访问顺序。

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

## 数据持久化（零配置，无需初始化）

当前版本的 KB 持久化**只有一条路径**：本地 SQLite「笔记本」（`KB_PERSIST_ENABLED=true` 默认开启）。首次写入时自动建表（`kb_entries`，schema 独立定义在 `app/runtime/core/storage/sqlite_kb_store.py`），无需手动执行任何 SQL、无需安装任何外部数据库。

历史说明：v0.8.x 及更早版本曾支持 PostgreSQL 持久化（`kb_entries` 表 + `_ensure_init` 首次访问自动建表），该后端已正式移除；旧 PG 的建表 SQL 已归档在仓库 [`archive/pg-migrations/`](../../archive/pg-migrations/)（仅供旧版用户查阅，当前版本不读取）。旧 PG kb_entries 数据的迁移指引见 [TROUBLESHOOTING.md L 节](./TROUBLESHOOTING.md#l-postgresql-后端移除说明--postgresql-removal)。

## 部署模式

| 模式 | 做法 | 经验归属 |
|------|------|----------|
| 单人使用 | 默认本地 memory + SQLite KB 笔记本 | 自己的经验；笔记本模式重启保留 |
| 中央多人共享 | 不在当前产品承诺内 | 不提供租户隔离或项目隔离 |
| 多租户 SaaS | —— | 当前版本不含租户隔离（表无 `tenant_id` 字段），一个库内经验互通 |

## 可靠性设计

- **PostgreSQL 后端已正式移除**：`STORAGE_BACKEND=memory` 是唯一合法值，KB 持久化由本地 SQLite 笔记本承担（初始化失败时降级 no-op、不阻断启动，行为不变）。原实验性 PG 路径的已知问题已随代码路径移除而消失（能力移除，非缺陷修复），移除说明与旧数据迁移指引见 [TROUBLESHOOTING.md L 节](./TROUBLESHOOTING.md#l-postgresql-后端移除说明--postgresql-removal)
- **驱逐顺序与持久删除**：进程内按访问顺序淘汰；重启回灌后、尚无新的访问调整时，优先淘汰最久未更新的经验。不承诺跨重启保留访问顺序；持久删除失败的风险见已知问题说明。
- **幂等迁移**：全部 `CREATE TABLE / INDEX IF NOT EXISTS`，重复执行安全
