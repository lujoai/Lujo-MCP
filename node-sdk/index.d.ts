/**
 * Type declarations for @lujoai/lujo-mcp-node-sdk.
 *
 * 这些声明描述 `client.cjs` 的运行时契约（W15 / P3-SDK-5：此前包里没有 `types`
 * 也没有 `.d.ts`，TypeScript 使用者拿到的是 `any`）。声明与运行时导出的一致性由
 * `test/node-sdk.test.js` 的 "type declarations cover every runtime export"
 * 守卫——新增运行时导出而没有补声明会让测试红。
 */

/** `flush()` / `close()` 的结果计数。 */
export interface FlushResult {
  /** 服务端确认接收的事件数（按批次响应的 `results[].ok` 统计）。 */
  sent: number;
  /** 重试耗尽或被服务端拒绝而丢弃的事件数。 */
  failed: number;
  /** 本次 flush 实际发出的批次数。 */
  batches: number;
  /** 含重试在内的 HTTP 尝试总次数。 */
  attempts: number;
  /**
   * 使批次失败的非 2xx HTTP 状态码（如 401 密钥错、413 体积超限、503 不可用）。
   *
   * **键缺席表示"没有服务端拒绝"**：网络不可达（拿不到任何 HTTP 响应）与全部
   * 成功都不会带这个键，因此不要用 `result.lastErrorStatus === undefined`
   * 之外的方式区分"连不上"与"被拒"。2xx 但部分 `results[].ok === false`
   * 属业务级失败，同样不带此键。
   */
  lastErrorStatus?: number;
}

/** `reportError()` / `reportNetworkError()` 返回的关联标识。 */
export interface ReportRef {
  trace_id: string;
  session_id: string;
}

/** `reportError()` 的返回值，额外带解析出的栈帧数。 */
export interface ErrorReportRef extends ReportRef {
  frame_count: number;
}

/**
 * 网络失败记录。字段与服务端 `POST /ingest/network` 的 record 对齐；
 * 未显式给 `source` 时由客户端补上（默认 `"node-sdk"`）。
 * 值在出网前会经客户端侧脱敏（敏感键名整值掩码 + 字符串正则遮蔽）。
 */
export interface NetworkRecord {
  method?: string;
  url?: string;
  status_code?: number;
  headers?: Record<string, unknown>;
  source?: string;
  duration_ms?: number;
  request_body?: unknown;
  response_body?: unknown;
  [key: string]: unknown;
}

/** `createClient()` 的选项；缺省 `endpoint` 时回落 `LUJO_MCP_ENDPOINT` 或本地默认。 */
export interface NodeSdkOptions {
  /** Lujo 服务基址，必须是非空的 http(s) URL（非法值抛 `TypeError`）。 */
  endpoint?: string;
  /** API Key；也接受 `api_key`，缺省回落环境变量 `LUJO_MCP_API_KEY`。 */
  apiKey?: string;
  api_key?: string;
  /** 上报来源标记，默认 `"node-sdk"`。 */
  source?: string;
  /** 发布版本标记；给出时随 `extra.release` 一并上报。 */
  release?: string;
  /** 单批事件数上限（1..100，服务端上限同为 100）。 */
  batchSize?: number;
  /** 自动 flush 间隔（毫秒，0..3600000）。 */
  batchIntervalMs?: number;
  batchInterval?: number;
  /** 失败重试次数上限（0..10）。 */
  maxRetries?: number;
  /** 首次重试延迟（毫秒，0..60000），其后按指数退避。 */
  retryDelayMs?: number;
  /** 退避延迟上限（毫秒，0..120000）。 */
  maxRetryDelayMs?: number;
  /** 单次请求超时（毫秒，0..120000；0 表示不设超时）。 */
  requestTimeoutMs?: number;
  /** 显式指定会话 id；缺省自动生成。也接受 `session_id`。 */
  sessionId?: string;
  session_id?: string;
  /** 显式指定 trace id；缺省自动生成。也接受 `trace_id`。 */
  traceId?: string;
  trace_id?: string;
  /** 注入自定义 fetch（测试或代理场景）；缺省用 Node 18+ 全局 fetch。 */
  fetch?: typeof globalThis.fetch;
}

/** 客户端实例（`createClient()` 的返回值）。close() 之后再上报会抛错。 */
export interface NodeSdkClient {
  /** 入队一条错误上报；返回本次关联的 trace/session 与解析出的栈帧数。 */
  reportError(error: unknown, extra?: unknown): ErrorReportRef;
  /** 入队一条网络失败记录。 */
  reportNetworkError(record: NetworkRecord): ReportRef;
  /** 立即发送队列中的全部事件（含有限重试），并等待完成。 */
  flush(): Promise<FlushResult>;
  /** flush 后关闭客户端；幂等，关闭后再上报会抛错。 */
  close(): Promise<FlushResult>;
  getSessionId(): string;
  getTraceId(): string;
  /** 切换 trace id（空串或非字符串抛 `TypeError`），返回生效值。 */
  setTraceId(traceId: string): string;
}

export declare function createClient(options?: NodeSdkOptions): NodeSdkClient;

// 以下模块级函数作用于**默认客户端**（首次调用时按 LUJO_MCP_ENDPOINT /
// LUJO_MCP_API_KEY 惰性创建）。
export declare function reportError(error: unknown, extra?: unknown): ErrorReportRef;
export declare function reportNetworkError(record: NetworkRecord): ReportRef;
export declare function flush(): Promise<FlushResult>;
export declare function close(): Promise<FlushResult>;
export declare function getSessionId(): string;
export declare function getTraceId(): string;
export declare function setTraceId(traceId: string): string;

declare const api: {
  createClient: typeof createClient;
  reportError: typeof reportError;
  reportNetworkError: typeof reportNetworkError;
  flush: typeof flush;
  close: typeof close;
  getSessionId: typeof getSessionId;
  getTraceId: typeof getTraceId;
  setTraceId: typeof setTraceId;
};
export default api;
