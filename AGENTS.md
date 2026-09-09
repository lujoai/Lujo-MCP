# Lujo-MCP 项目执行规则

本文件是 Lujo-MCP 的项目级 Codex 规则。

同时遵守全局 AGENTS.md。

全局规则负责：

- Plus 额度控制
- Main / Luna-Max 分工
- 多智能体策略
- Git 通用规则
- 上下文效率

本文件只补充 Lujo-MCP 项目自己的架构、不变量、
测试、存储、安全与开发纪律。

---

## 1. 开始修改代码之前

任何 AI 修改 Lujo-MCP 代码前必须先阅读：

1. `AGENTS.md`
2. `docs/internal/AI_HANDOFF.md` 中 Architecture Frozen
3. 当前任务直接相关的 `DEV_PLAN`
4. 当前任务直接相关的 `CODE_REVIEW`
5. 本任务涉及的实现与测试

不需要每轮完整读取所有历史版本记录。

不要每次重新读取：

- 完整 CHANGELOG 历史
- CODE_REVIEW 所有历史发布记录
- DEV_PLAN 所有旧版本
- 所有 DESIGN 文档
- 整个仓库

只读取当前任务所需部分。

---

## 2. Architecture Frozen

严格遵守 `AI_HANDOFF.md` 中 Architecture Frozen 的规则。

新增能力前必须先判断：

- 属于哪个 Layer
- 为什么属于这个 Layer
- 要修改哪些文件
- 是否改变依赖方向
- 是否跨越架构冻结边界

如果 Layer 归属不明确：

先停止实现，
由主智能体判断。

不得让子智能体自行突破 Architecture Frozen。

---

## 3. 产品定位

当前产品定位：

- 单用户
- 本地自用
- 本地安装
- 供 Claude / Codex / Trae 等宿主智能体调用
- Lujo 负责采集、关联和查询运行现场
- 宿主智能体负责推理和修改业务代码

默认不把 Lujo 当成中央多人共享服务。

中央共享 PostgreSQL 下的多人 / 多项目隔离
目前不是承诺支持的产品场景。

未经明确立项：

不得新增：

- project_id
- tenant_id
- namespace
- 多租户 schema
- 强制 session_id

---

## 4. 必须保持的核心不变量

任何修改不得破坏：

### stdio

- stdout 必须保持纯 MCP 协议
- 日志必须走 stderr
- 不得向 stdout 输出 debug 信息

### Security

- 脱敏必须发生在存储边界之前
- 路径白名单必须保留
- 认证保持 fail-closed
- 安全默认值不得静默放宽

### Storage

- `STORAGE_BACKEND` 默认保持 `memory`
- 不得未经明确计划改变默认持久化行为
- memory 与 PostgreSQL 应尽可能保持行为语义一致

### HTTP / stdio

HTTP 与 stdio 应尽可能：

- 共用 handler
- 共用校验
- 共用门控
- 共用工具失败语义

不得让两个传输层长期产生不同业务契约。

### MCP Tool errors

工具真正失败时必须通过正确的：

`isError`

语义暴露。

验证类工具的“验证结论为失败”与“工具执行失败”
不得混为一谈。

---

## 5. session_id 与项目隔离

当前契约：

`session_id` 缺省 = 不过滤

不要把它写成：

- 必填
- 总是必须传
- 所有工具都必须有 session_id

memory 后端默认按 Lujo 进程隔离。

stdio-only：

`--no-http`

时不同宿主窗口的进程天然隔离。

多个项目同时启用 HTTP 采集时：

每个项目使用不同：

`--http-port`

Browser SDK 的 endpoint 必须指向对应 Lujo 实例。

未经正式立项：

不要通过 schema 增加 project 维度解决该问题。

---

## 6. PostgreSQL / asyncpg

测试默认必须保持与开发者真实 PostgreSQL 隔离。

不得因为本机 `.env` 配置 PostgreSQL，
让普通 unit / integration / e2e 测试意外写入真实数据库。

真库测试必须显式开启。

当前长期需要单独验证的 PG / asyncpg 风险包括：

1. PG error upsert 节流键目前需要核实
   `(fingerprint, session)` 语义。

2. `pg_async_enabled=True` 路径下，
   errors 的读写完整链路需要真实验证。

3. stdio 生命周期关闭 `_LIGHT_TOOL_EXECUTOR` 后，
   HTTP 路径的 executor 生命周期 / 自愈需要真实验证。

这些问题应单独立工作包。

不要在无关功能开发中顺手重构整个 storage layer。

---

## 7. Heavy tools

heavy 工具使用独立子进程。

必须保持：

- 并发门控
- `TOOL_BUSY` fast-fail
- timeout
- terminate / kill 语义
- 资源回收

修改 heavy tool、executor、multiprocessing、
冻结产物启动流程时视为高风险改动。

必须进行针对性的生命周期测试。

不要为了减少冷启动时间，
未经真实 P95 数据就引入常驻 worker 池或大规模生命周期重构。

---

## 8. 测试环境

Windows 本地开发使用：

`.venv/Scripts/python.exe`

目标 Python：

`3.12.x`

不要使用系统裸：

`python`

如果系统 Python 与项目 venv 版本不同，
以项目 `.venv` 为准。

Node 开发基线：

Node 22

npm packages 声明的公开 Node 下限不得被无意改变。

---

## 9. 基础验证命令

按当前任务选择最小相关测试。

Python lint：

```bash
.venv/Scripts/python.exe -m ruff check .
```

单元测试基线：

```bash
.venv/Scripts/python.exe -m pytest tests/unit -q
```

Node SDK 单元测试：

```bash
npm test --prefix node-sdk
```

文档链接检查：

```bash
.venv/Scripts/python.exe scripts/check_doc_links.py
```
