# Lujo-MCP Demo：一个完整的 AI 调试场景

> 目标：用一个真实可感的前端 Bug 场景，快速理解 **Lujo-MCP 给 AI coding agents 带来什么价值** —— 让 AI 看到真实运行现场，而不是只读代码。

---

## 场景：React 登录页（Login）Bug

一个 React 应用，用户点击「登录」按钮后**页面毫无反应**：
- 没有报错弹窗
- 接口似乎也没请求
- 但登录就是没成功

传统做法：开发者手动打开 DevTools → 看 Console → 看 Network → 复现 → 截图 → 整理成提示词 → 丢给 AI。整个过程耗时 5–15 分钟，而且 AI 拿到的往往是不完整的二手信息。

---

## 用 Lujo-MCP 的完整流程

### 第 1 步：页面接入 Browser SDK

在 React 应用里引入 Lujo-MCP 的 Browser SDK（`ai-debug.js`），初始化后自动开始采集：

```html
<script src="/vendor/ai-debug.js"></script>
<script>
  window.LujoDebug.init({ endpoint: "https://your-lujo-server/api/ingest" });
</script>
```

### 第 2 步：用户点击 Login

```text
用户点击「Login」按钮
        ↓
Browser SDK 捕获三类现场数据
```

### 第 3 步：SDK 捕获真实运行现场

| 类别 | 捕获到的内容 |
|---|---|
| **console error** | `TypeError: Cannot read properties of undefined (reading 'token')` |
| **network failure** | `POST /api/auth/login` → 请求已发出但**响应被中断**（网络错误自动标记） |
| **user action** | 记录「点击 Login」这一交互事件，以及点击后 **DOM / 路由无任何变化**（UI 静默失败检测） |

### 第 4 步：Lujo-MCP 提供 Debug Context

这些原始事件经 Lujo-MCP 组装成 AI 可直接理解的结构化 **Debug Context**：

```json
{
  "trace_id": "t_20260811_ab12cd",
  "exception_type": "TypeError",
  "message": "Cannot read properties of undefined (reading 'token')",
  "stacktrace": "at Login.handleSubmit (Login.jsx:45)",
  "network_trace": {
    "method": "POST",
    "url": "/api/auth/login",
    "status": 0,
    "error": "network_failure"
  },
  "ui_events": [
    { "type": "click", "target": "button.login", "timestamp": 1754870400000 }
  ],
  "debug_experience": {
    "hint": "同类报错历史中 80% 是响应未序列化，token 字段缺失",
    "source": "Debug Experience RAG"
  }
}
```

### 第 5 步：AI Agent 分析原因

宿主 AI（Claude / Cursor / Trae）调用 MCP 工具 `get_debug_context`，拿到上面的真实运行现场后，直接定位：

> **根因推断**：`POST /api/auth/login` 网络请求失败（status 0，连接中断），且登录处理函数在访问 `res.token` 时抛 `TypeError`——很可能是后端未返回或前端未处理 `token` 字段，导致点击后静默无响应。

AI 不再需要你手动翻日志、拼提示词，就能给出基于**真实运行现场**的分析与修复建议。

---

## 对比：Without Context vs With Lujo Context

| 维度 | 无 Lujo Context | 有 Lujo Context |
|---|---|---|
| 信息源 | 靠开发者手动收集、转述 | AI 直接读取真实运行现场 |
| 网络细节 | 常常缺失（请求体/响应体/耗时） | 完整（含失败状态与原因） |
| 用户交互 | 需口头描述 | 自动记录点击 / 提交轨迹 |
| 定位速度 | 5–15 分钟人工整理 | 秒级拿到结构化上下文 |
| 历史经验 | 无 | Debug Experience RAG 自动召回 |

---

## 你可以这样复现

1. 启动 Lujo-MCP 服务：`python -m app.main` 或 `docker compose up -d`
2. 打开网络捕获 Demo：`http://localhost:8000/demo`
3. 点击页面测试按钮，制造一次网络错误 / 静默失败
4. 打开 Dashboard：`http://localhost:8000/dashboard` 查看追踪记录与 AI 分析
5. 在 MCP 客户端（Claude / Cursor / Trae）中调用 `get_debug_context`，体验 AI 拿到真实运行现场

> 详细操作见 [Demo 演示流程](../../README.md) 与 [DEMO_GUIDE.md](./DEMO_GUIDE.md)。
