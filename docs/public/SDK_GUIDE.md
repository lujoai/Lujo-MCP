# Lujo-MCP SDK 使用手册

> 当前发布目标：v0.7.9（2026-09-10，发布中）。本文包含 Node SDK 首发接线与 Browser SDK 使用边界。
> Browser SDK：`browser-sdk/ai-debug.js`，面向浏览器现场自动采集；Node SDK：`@lujoai/lujo-mcp-node-sdk`，面向 Node.js 服务端显式上报。
> 概括：两种 SDK 都把运行现场上报到 Lujo-MCP 服务端，但运行时职责不同，不能互相替代。

---

## 目录

- [SDK 选择与边界](#sdk-选择与边界)
- [Node SDK（服务端 Node.js）](#node-sdk服务端-nodejs)
- [1. 快速接入](#1-快速接入)
- [2. 初始化配置项](#2-初始化配置项)
- [3. 公开 API](#3-公开-api)
- [4. 自动采集行为](#4-自动采集行为)
- [5. 拦截规则（XHR / fetch）](#5-拦截规则xhr--fetch)
- [6. 脱敏](#6-脱敏)
- [7. V5 传输优化](#7-v5-传输优化gzip节流localstorage-降级)
- [8. 鉴权与 beacon 令牌](#8-鉴权与-beacon-令牌)

---

## SDK 选择与边界

| SDK | 适用运行时 | 采集方式 | 发布包 |
|-----|------------|----------|--------|
| Browser SDK | 浏览器页面 | 自动捕获 `window` 异常、XHR/fetch、UI、console 和静默失败 | 随 `@lujoai/lujo-mcp` 分发的 `browser-sdk/ai-debug.js` |
| Node SDK | Node.js 服务端（Node 18/20/22） | 应用代码显式调用错误/网络上报 API | `@lujoai/lujo-mcp-node-sdk` |

Browser SDK 依赖 DOM、浏览器网络对象、`localStorage` 和 `sendBeacon`，不要在 Node 服务进程中调用它的 `init()`。Node SDK 不安装浏览器钩子，不读取 DOM、XHR、`localStorage` 或 `sendBeacon`；它通过显式 API 将服务端异常和网络记录送到 `/ingest`。两者可以把 `session_id`、`trace_id` 和 `release` 设为同一业务值，供服务端关联现场。

## Node SDK（服务端 Node.js）

Node SDK 面向 Node.js 服务端的主动上报，随 v0.7.9 首次发布，支持 Node 18、20、22，并提供 CommonJS 和 ESM 两种包根入口。

安装独立包：

```bash
npm install @lujoai/lujo-mcp-node-sdk
```

CommonJS：

```js
const { createClient } = require("@lujoai/lujo-mcp-node-sdk");

const lujo = createClient({
  endpoint: "http://127.0.0.1:8000",
  apiKey: process.env.LUJO_MCP_API_KEY,
  release: "orders-service@1.4.0",
});

async function main() {
  try {
    await handleRequest();
  } catch (error) {
    lujo.reportError(error, { operation: "handleRequest" });
    await lujo.flush();
    throw error;
  } finally {
    await lujo.close();
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
```

ESM：

```js
import { createClient } from "@lujoai/lujo-mcp-node-sdk";

const lujo = createClient({
  endpoint: "http://127.0.0.1:8000",
  apiKey: process.env.LUJO_MCP_API_KEY,
});

lujo.reportNetworkError({
  method: "GET",
  url: "https://api.example.com/orders",
  status_code: 503,
  duration_ms: 420,
});

await lujo.flush();
await lujo.close();
```

### Node SDK API 与传输语义

| API | 语义 |
|-----|------|
| `createClient({ endpoint?, apiKey?, release?, ... })` | 创建一个进程内客户端；`endpoint` 是 Lujo-MCP 服务根地址，省略时使用 `http://127.0.0.1:8000`。`apiKey` 通过请求头发送，`release` 随错误现场透传。 |
| `reportError(error, extra?)` | 将 `Error` 或可序列化错误加入错误队列；`extra` 用于补充业务上下文。调用本身不保证已经完成网络发送。 |
| `reportNetworkError(record)` | 将一次网络失败记录加入网络事件队列；建议传 `method`、`url`、`status_code` 和 `duration_ms`。 |
| `flush()` | 等待当前队列发送和有限重试完成，并返回 `sent`、`failed`、`batches`、`attempts` 计数；单批不超过服务端允许的 100 条事件，调用方应检查 `failed`。 |
| `close()` | 应用退出或 worker 重启前调用；完成最后一次 flush、停止内部定时器并释放客户端资源。重复调用安全，关闭后不要继续提交事件。 |
| `getSessionId()` / `getTraceId()` | 读取当前会话/追踪标识；`setTraceId(id)` 可把多个上报关联到同一业务操作。 |

可选传输参数包括 `batchSize`（1–100）、`batchIntervalMs`、`maxRetries`、`retryDelayMs`、`maxRetryDelayMs` 和 `requestTimeoutMs`；客户端仍会把单批大小限制在 100 条以内。测试或自定义传输时可注入 `fetch` 实现。

客户端在发送前递归脱敏错误、网络记录和 `extra` 中的敏感字段；默认覆盖 `password`、`token`、`secret`、`authorization`、`cookie`、`api_key`、`private_key` 等键。不要把密钥拼到 URL 或错误消息中，脱敏是最后一道保护而不是传递秘密的方式。

传输失败按有限次数重试：429 和 5xx 属于临时错误，使用退避后重试；4xx 参数或鉴权错误不应无限重试。超过重试上限后，`flush()` 会报告发送失败，调用方可记录本地日志并决定是否继续抛出原始业务错误。SDK 不承诺离线持久化，进程退出前应显式 `await close()`。

Node SDK 不会自动拦截 `fetch`、`http`、`undici` 或 `axios`。需要透明网络采集、页面 DOM 变化和浏览器卸载兜底时，请使用 Browser SDK；需要服务端异常和网络失败时，请使用 Node SDK 的显式 API。

## 1. 快速接入

### 方式一：`<script>` 标签（无构建工具）

```html
<script src="/ai-debug.js"></script>
<script>
  AiDebug.init({ endpoint: "http://localhost:8000" });
</script>
```

> 服务端内置挂载路径为 `/ai-debug.js`（见 `app/main.py`），直接相对引入即可。

### 方式二：在浏览器构建工具中加载

```js
// ai-debug.js 运行在浏览器上下文，并把 AiDebug 暴露到 globalThis
import "./ai-debug.js";
globalThis.AiDebug.init({ endpoint: "http://localhost:8000" });

// CommonJS 仅适合浏览器打包/测试环境；不要在纯 Node 进程中调用 init()
const AiDebug = require("./ai-debug.js");
AiDebug.init({ endpoint: "http://localhost:8000" });
```

初始化后 Browser SDK 自动安装浏览器采集钩子（错误 / 网络 / XHR / UI / 静默失败 / 控制台 / 页面卸载），无需手动调用。Node 服务端请使用上面的 Node SDK 显式上报。

---

## 2. 初始化配置项

`AiDebug.init(options)` 仅接受下列已知键（其他键忽略）。首次调用生效，重复调用被 `_inited` 守卫忽略。

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `endpoint` | `""` | **必填**。服务端地址，如 `http://localhost:8000`（不含 `/ingest` 前缀） |
| `apiKey` | `""` | API Key（优先走请求头；`sendBeacon` 场景自动换 beacon 短时令牌） |
| `captureErrors` | `true` | 全局异常捕获 |
| `captureNetwork` | `true` | 网络请求捕获 |
| `captureUI` | `true` | UI 交互事件捕获 |
| `captureConsole` | `true` | 控制台日志捕获 |
| `redactFields` | `["password","token","secret","authorization"]` | 脱敏字段名单（空数组回退内置默认，防关闭脱敏） |
| `sampleRate` | `1.0` | 遥测事件发送采样率（network 自动捕获 / ui-event / console）；错误类上报（`reportError` / `reportSilentFailure` / `reportNetworkError` 与全局异常捕获）豁免采样必达 |
| `networkSampleRate` | `1.0` | 网络请求自动捕获采样率。注意：网络事件先过本采样、发送时再过 `sampleRate`（双重采样，实际送达率 ≈ 两者乘积） |
| `networkThrottleMs` | `0` | 网络上报节流间隔（`0`=无节流） |
| `autoDetectNetworkErrors` | `true` | V3：fetch/XHR 失败自动转静默失败 |
| `autoDetectUISilentFailures` | `true` | V6：点击/提交后无 DOM/路由/网络变化自动判定静默失败 |
| `uiSilentFailureTimeoutMs` | `1800` | V6：静默失败观察窗口（ms） |
| `uiSilentFailureObserveSelector` | `"body"` | V6：DOM 变化观察选择器 |
| `silentFailureContextSize` | `20` | `reportSilentFailure` 自动附加最近 N 条事件链 |
| `batchSize` | `20` | 队列满阈值，达到即 flush |
| `batchInterval` | `1000` | 定时 flush 间隔（ms） |
| `maxRetries` | `3` | XHR 失败最大重试次数 |
| `enableCompression` | `true` | V5：gzip 压缩开关 |
| `compressionThreshold` | `4096` | V5：payload > N 字节时启用 gzip |
| `throttleWindowMs` | `5000` | V5：节流时间窗（ms） |
| `maxBatchesPerWindow` | `2` | V5：每时间窗最多发送批次数 |
| `enableLocalStorageFallback` | `true` | V5：超重试后暂存 localStorage |
| `localStorageKey` | `"ai-debug-pending-batches"` | V5：localStorage 暂存键 |
| `maxPendingBatches` | `10` | V5：最多暂存批次数 |
| `release` | `""` | v0.5.1：发布标识，随错误 extra 透传（空=不发送） |

---

## 3. 公开 API

| 方法 | 说明 |
|------|------|
| `init(options)` | 初始化（自动安装采集钩子） |
| `destroy()` | 销毁实例：摘除全部监听器、还原被包装的全局 API（onerror/fetch/XHR/console）、停止全部定时器并清空队列/去重表（幂等，可重新 init）。页面卸载 / HMR 热更新场景建议显式调用。**已知限制**：destroy 只能还原到「SDK 安装前」的状态——若第三方脚本在 SDK 之后又包装了 fetch/XHR/console，SDK 无法感知与还原它们的包装（与业界 destroy 语义一致）；对包装顺序有要求的集成方请自行保存/恢复原始引用 |
| `flush()` | 手动立即 flush 批量队列 |
| `reportError(error, extra?)` | 手动上报异常（自动带堆栈解析） |
| `reportNetworkError(error)` | 手动上报网络错误，自动附最近 UI/network 上下文 |
| `reportUIEvent(event)` | 手动上报 UI 事件 |
| `reportSilentFailure(payload)` | 手动上报静默失败（自动附最近事件链） |
| `getSessionId()` | 获取当前会话 ID |
| `getTraceId()` | 获取当前 trace ID（贯穿所有上报） |
| `setTraceId(id)` | 设置 trace ID（关联同一业务操作） |
| `onNetworkCapture(cb)` | 注册网络捕获回调（测试/自定义消费） |
| `onSilentFailureReport(cb)` | 注册静默失败上报回调 |

### `reportSilentFailure(payload)` 参数

```js
AiDebug.reportSilentFailure({
  description: "点击登录按钮后无反应",   // 必填
  observed: "点击后无跳转、无请求",       // 可选项：现象描述
  expected: { type: "route_change", to: "/dashboard" },  // 可选项：期望行为
});
```

SDK 会自动从环形缓冲取最近 `silentFailureContextSize`（默认 20）条 network/UI 事件，拼为 `observed_events` 与 `trace_id` 一起上报；服务端按 `kind` 分类入库，供宿主 AI 通过 `diagnose_issue` / `context` 等 MCP 工具召回完整事件链。

---

## 4. 自动采集行为

初始化后 SDK 自动捕获以下事件类型并上报到对应端点：

| 事件类型 | 上报端点 | 触发条件 |
|----------|----------|----------|
| 异常（exception） | `/ingest/error` | 全局 `error`/`unhandledrejection`，或 `reportError` |
| 网络错误（network failure） | `/ingest/network` + 静默失败 | fetch/XHR 请求失败或被中断（V3 自动标记） |
| UI 交互（user action） | `/ingest/ui-event` | 点击等交互事件 |
| UI 静默失败 | `/ingest/silent-failure` | V6：点击/提交后无 DOM/路由/网络变化 |
| 控制台日志 | `/ingest/console` | `console.error` / `console.warn` |

初始化的 `trace_id` 与 `session_id` 贯穿所有上报，服务端据此关联同一 SDK 生命周期内的全部事件。

---

## 5. 拦截规则（XHR / fetch）

SDK 通过 monkey-patch 拦截浏览器网络请求，**两者同源捕获**（V2 起）：

- **XMLHttpRequest**：拦截 `open` / `send`，记录 method / url / request body / response body / status / duration。
- **fetch**：包装全局 `fetch`，同样捕获上述信息。
- **自排除**：SDK 自身发往 endpoint 的上报请求会被识别并跳过，避免递归上报（`_isSelfRequest`）。
- **请求体序列化**：自动安全处理 `FormData` / `Blob` / `URLSearchParams` / 普通 JSON 等多种类型。
- **响应/请求体脱敏**：在存储/上报前统一递归脱敏。

---

## 6. 脱敏

- **默认敏感键**（`redactFields` 为空时回退内置）：
  `password` / `token` / `secret` / `authorization` / `cookie` / `access_token` / `api_key` / `apikey` / `passwd` / `pwd` / `private_key` / `auth_token`
- 脱敏对请求体、响应体、URL、错误文本、控制台参数做**递归替换**（`_redact`，含 JSON 解析后逐层处理）。
- url / error 等字段在采集时即 `_redact`，避免敏感信息进入服务端。

---

## 7. V5 传输优化（gzip/节流/localStorage 降级）

| 能力 | 行为 |
|------|------|
| **批量上报** | 事件入队，满 `batchSize` 或到 `batchInterval` 定时 flush 到 `/ingest/batch` |
| **gzip 压缩** | `enableCompression=true` 且 payload > `compressionThreshold`（4096B）时 `Content-Encoding: gzip` |
| **节流** | 每 `throttleWindowMs`（5s）最多 `maxBatchesPerWindow`（2）批，超出延迟到下一窗 |
| **失败降级** | 重试耗尽（`maxRetries`）后，`enableLocalStorageFallback=true` 时暂存到 localStorage（键 `ai-debug-pending-batches`），下次启动恢复重发 |

> 服务端 `/ingest/batch` 对应上限：单次 ≤100 条事件、gzip 解压 ≤10MB。

---

## 8. 鉴权与 beacon 令牌

- 正常场景：`apiKey` 通过 `X-API-Key` / `Authorization: Bearer` 请求头发送。
- `sendBeacon` / `EventSource` 无法自定义 header 时，SDK 启动即调用 `POST /auth/beacon-token` 换取短时令牌，并每 25s 续期；上报改用 `?token=` 查询参数，避免永久 Key 进 URL 被明文记录（见 `beacon.py`）。

---

## 相关文档

- [API_REFERENCE.md](./API_REFERENCE.md) — REST API 与 MCP 工具参考
- [DEMO.md](./DEMO.md) — 完整调试场景演示
- [DEMO_GUIDE.md](./DEMO_GUIDE.md) — 演示操作步骤（含 SDK 各特性测试区）
- [README.md](../../README.md) — 项目总览
- [TROUBLESHOOTING.md](./TROUBLESHOOTING.md) — 异常排查
