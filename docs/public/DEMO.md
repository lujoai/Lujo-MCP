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
<script src="/ai-debug.js"></script>
<script>
  window.AiDebug.init({ endpoint: "https://your-lujo-server" });
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

宿主 AI（Claude / Cursor / Trae）调用 MCP 工具 `context`，拿到上面的真实运行现场后，直接定位：

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
5. 在 MCP 客户端（Claude / Cursor / Trae）中调用 `context`，体验 AI 拿到真实运行现场

> 详细操作见 [Demo 演示流程](../../README.md) 与 下方「演示指南」。

---

## 演示指南 / Demo Guide

> **合并说明**：本节原为独立文件 `DEMO_GUIDE.md`（启动方式、六个演示步骤、常见问题、展示 Checklist），
> 已并入本文档。上面是「讲价值」的场景走查，本节是「照着演」的操作手册。
本文档介绍如何启动项目并展示核心功能。

> 演示能力不等同于默认交付状态；功能完成度与环境前提以内部文档为准。

### 一、启动项目

#### 方式一：Docker Compose（推荐）

```bash
# 进入项目目录
cd Lujo-MCP

# 复制环境变量模板
cp .env.example .env

# 编辑 .env，配置 LLM Provider
# 推荐使用智谱（免 VPN）：
# LLM_PROVIDER=zhipu
# OPENAI_API_KEY=your-zhipu-api-key

# 启动所有服务
docker compose up -d
```

#### 方式二：本地开发

```bash
# 安装依赖（生产部署用 requirements.txt，本地开发用 requirements-dev.txt）
pip install -r requirements-dev.txt

# 复制环境变量模板
cp .env.example .env

# 编辑 .env 配置

# 启动服务
python -m app.main
```

#### 验证服务启动

```bash
curl http://localhost:8000/
# 预期输出：{"status":"ok","service":"Lujo-MCP","version":"0.8.0"}
```

### 二、验证 Browser SDK

#### 1. 访问 Demo 页面

打开浏览器访问：`http://localhost:8000/demo`

页面包含以下测试区域：

| 区域 | 测试内容 |
|------|---------|
| XHR GET 捕获 | 测试基本 XHR GET 请求捕获 |
| XHR POST Body 捕获与脱敏 | 测试 POST 请求体捕获及密码脱敏 |
| XHR Response Body 捕获与脱敏 | 测试响应体捕获及 token 脱敏 |
| SDK 自身请求排除 | 验证无递归上报 |
| networkSampleRate 验证 | 测试采样率控制 |
| networkThrottleMs 验证 | 测试节流控制 |
| 业务兼容性验证 | 测试 xhr.onload / onreadystatechange 回调 |
| V3 网络错误自动上报 | 测试 fetch/XHR 失败自动转静默失败 |
| 默认 captureNetwork=true | 验证默认配置 |
| Request Body 序列化测试 | 测试 FormData、Blob、URLSearchParams |

#### 2. 测试步骤

##### 步骤 1：测试基本网络捕获

1. 点击「发送 XHR GET 请求」
2. 观察结果区域变为绿色 ✓
3. 确认请求被正确捕获

##### 步骤 2：测试 POST 请求与脱敏

1. 点击「发送 XHR POST 请求」
2. 观察请求体中 password 字段被脱敏为 `***`

##### 步骤 3：测试响应体脱敏

1. 点击「发送 XHR 获取 Token」
2. 观察响应体中 token 字段被脱敏为 `***`

##### 步骤 4：测试采样率控制

1. 点击「设置 networkSampleRate=0 测试」→ 确认请求不被捕获
2. 点击「设置 networkSampleRate=1 测试」→ 确认请求被捕获

##### 步骤 5：测试不同请求体类型

1. 点击「测试 FormData 请求」→ 确认 FormData 被正确序列化
2. 点击「测试 Blob 请求」→ 确认 Blob 显示类型和大小
3. 点击「测试 URLSearchParams 请求」→ 确认参数被正确序列化

##### 步骤 6：测试网络错误自动上报（V3）

1. 点击「触发失败请求并自动上报」
2. 观察页面展示的 silent failure payload
3. 确认 `observed_events` 中带有最近网络事件摘要，且 URL / body 已脱敏

### 三、展示 Network Capture

#### 1. 功能亮点

- **全链路覆盖**：同时支持 XMLHttpRequest 和 fetch
- **智能序列化**：自动识别并安全处理多种请求体类型
- **采样节流**：支持采样率和时间间隔控制，避免性能影响
- **递归保护**：SDK 自身上报请求自动排除
- **网络错误自动标记**：请求失败会自动转为 silent failure
- **UI 静默失败自动检测**：支持点击/提交后的 DOM/路由/网络观察窗口判定

#### 2. 演示要点

##### 请求体序列化展示

```javascript
// FormData 请求
// 输入：FormData { username: 'admin', password: '123456' }
// 输出："username=admin&password=123456"

// Blob 请求
// 输入：Blob { type: 'image/png', size: 10240 }
// 输出："[Blob: image/png, 10240 bytes]"

// URLSearchParams 请求
// 输入：URLSearchParams { a: 1, b: 2 }
// 输出："a=1&b=2"
```

##### 敏感信息脱敏

SDK 自动脱敏以下字段：
- password
- token
- secret
- authorization

##### SDK 初始化配置

```javascript
AiDebug.init({
  endpoint: "http://localhost:8000",
  // captureNetwork 默认开启（V2 新特性）
  // networkSampleRate 默认 1.0（全部采样）
  // networkThrottleMs 默认 0（无节流）
  // autoDetectNetworkErrors 默认 true（V3）
  // autoDetectUISilentFailures 默认 true（V6）
});
```

#### 3. 查看后端数据

```bash
# 查询网络请求记录
curl http://localhost:8000/api/dashboard/traces

# 查询单个 trace 的网络记录
curl http://localhost:8000/ingest/network/{trace_id}
```

### 四、展示 AI Debug 场景

#### 1. Dashboard 控制台

访问：`http://localhost:8000/dashboard`

查看：
- 最近请求追踪列表
- 错误统计
- 运行时快照

#### 2. AI 分析流程

##### 步骤 1：触发一个错误

1. 在 Demo 页面触发网络请求
2. 或在后端触发一个异常

##### 步骤 2：查看 AI 分析

1. 打开 Dashboard
2. 点击某个 trace 的「分析」按钮
3. 查看 LLM 自动分析的错误根因和修复建议

#### 3. 静默失败检测演示

使用 verify 功能检测"返回正常但不符合规范"的情况：

```bash
curl -X POST http://localhost:8000/api/debug/verify \
  -H "Content-Type: application/json" \
  -d '{
    "actual": {
      "status_code": 200,
      "body": {"data": null}
    },
    "spec": {
      "expect": {
        "body": {
          "data": {"type": "object", "required": true}
        }
      }
    }
  }'
```

#### 4. Browser SDK UI 静默失败自动检测演示（V6）

仓库内提供演示页 `app/web/silent_failure_demo.html`，用于本地验证 SDK 对点击/提交后“无 DOM 变化、无路由变化、无网络变化”的自动判定能力。

推荐方式：

1. 保持服务运行
2. 直接打开该 HTML 文件，或在本地静态文件服务器下访问
3. 点击「点击后“假装提交”但不更新 UI」
4. 观察右侧 payload 区域是否出现自动生成的 silent failure 上报内容

### 五、常见问题

#### Q1: Demo 页面无法加载？

确保服务已启动：
```bash
curl http://localhost:8000/
```

#### Q2: 网络请求捕获失败？

检查：
1. SDK 是否已正确初始化（查看控制台日志）
2. endpoint 是否配置正确
3. 网络请求是否被 `_isSelfRequest` 排除（SDK 自身请求会被跳过）
4. 若在验证 V3 / V6，确认 `autoDetectNetworkErrors` / `autoDetectUISilentFailures` 未被关闭

#### Q3: LLM 分析失败？

检查：
1. `.env` 中 API Key 是否配置正确
2. 网络是否能访问 LLM Provider（智谱无需 VPN）
3. 查看后端日志：`docker compose logs app`

### 六、展示 Checklist

- [ ] 服务启动成功
- [ ] Demo 页面加载正常
- [ ] XHR GET 请求捕获成功
- [ ] XHR POST 请求捕获成功（含脱敏）
- [ ] Response Body 捕获成功（含脱敏）
- [ ] networkSampleRate=0 验证通过
- [ ] networkSampleRate=1 验证通过
- [ ] FormData 请求序列化成功
- [ ] Blob 请求序列化成功
- [ ] SDK 自排除验证通过
- [ ] Dashboard 可访问
- [ ] AI 分析功能正常
