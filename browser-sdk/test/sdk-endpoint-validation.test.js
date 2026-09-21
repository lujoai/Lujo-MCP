/**
 * endpoint 合法性校验单测（W15 / P3-SDK-2）。
 *
 * 缺陷复现口径：init() 只判空不判格式，`endpoint: "localhost:8000"`（漏 scheme）
 * 会通过校验并装完钩子；此后所有上报 URL 由
 * `cfg.endpoint.replace(/\/+$/, "") + "/ingest/batch"` 拼出，是**相对地址**，
 * 被浏览器按页面 origin 解析 → 现场数据被静默 POST 到用户自己的业务服务器
 * （404/被业务日志吃掉），SDK 侧零告警。Node SDK 对同一输入抛 TypeError，
 * 两个 SDK 的校验强度不对称。
 *
 * 浏览器 SDK 的修法与 Node SDK 不同口径：SDK 注入宿主页面，init() 抛异常会
 * 直接打断宿主脚本，因此保持既有「告警 + 拒绝初始化」的失败安全语义
 * （与空 endpoint 同路径），只在装钩子**之前**把格式判掉。
 *
 * 本文件独立成篇： behavioural 用例会真正调用 init()，单独进程可把副作用
 * （钩子安装 / console 包装 / 定时器）限制在文件内。
 */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const SDK = require("../ai-debug.js");

// ── 纯函数判定表（无副作用） ────────────────────────────────────────

test("_isEndpointUsable：只接受 http(s) 绝对地址", () => {
  assert.equal(typeof SDK._isEndpointUsable, "function", "缺少 _isEndpointUsable 判定函数");

  // 合法
  assert.equal(SDK._isEndpointUsable("http://127.0.0.1:8000"), true);
  assert.equal(SDK._isEndpointUsable("https://lujo.example.com"), true);
  assert.equal(SDK._isEndpointUsable("http://localhost:8000/base/path"), true);
  assert.equal(SDK._isEndpointUsable("http://localhost:8000/"), true);

  // 非法：漏 scheme（本缺陷的主形态，会被当相对地址解析）
  assert.equal(SDK._isEndpointUsable("localhost:8000"), false);
  assert.equal(SDK._isEndpointUsable("127.0.0.1:8000"), false);
  assert.equal(SDK._isEndpointUsable("/ingest"), false);

  // 非法：非 http(s) scheme —— 上报走 fetch/XHR/sendBeacon，其他协议必然失败
  assert.equal(SDK._isEndpointUsable("ftp://example.com"), false);
  assert.equal(SDK._isEndpointUsable("file:///tmp/x"), false);
  assert.equal(SDK._isEndpointUsable("ws://127.0.0.1:8000"), false);

  // 非法：空 / 非字符串 / 畸形
  assert.equal(SDK._isEndpointUsable(""), false);
  assert.equal(SDK._isEndpointUsable(null), false);
  assert.equal(SDK._isEndpointUsable(undefined), false);
  assert.equal(SDK._isEndpointUsable(8000), false);
  assert.equal(SDK._isEndpointUsable({}), false);
  assert.equal(SDK._isEndpointUsable("http://"), false);
});

// ── 行为：非法 endpoint 必须拒绝初始化（先于装钩子） ─────────────────

test("init：非法 endpoint 拒绝初始化并告警，不得进入钩子安装", () => {
  const warnings = [];
  const origWarn = console.warn;
  const origLog = console.log;
  console.warn = function () {
    warnings.push(Array.prototype.join.call(arguments, " "));
  };
  // init 成功路径会 console.log 会话号；一并静音，避免污染 TAP 输出
  console.log = function () {};

  let initError = null;
  let initedAfter = null;
  try {
    SDK.init({ endpoint: "localhost:8000" });
    initedAfter = SDK._inited;
  } catch (e) {
    initError = e;
    initedAfter = SDK._inited;
  } finally {
    try {
      SDK.destroy({ flush: false });
    } catch (_) {
      // 清理失败不影响断言：本文件独占进程
    }
    console.warn = origWarn;
    console.log = origLog;
  }

  assert.equal(
    initError,
    null,
    "endpoint 校验必须先于钩子安装；实际在 init 内抛错说明已经越过校验进入安装阶段：" +
      (initError && initError.message),
  );
  assert.equal(initedAfter, false, "非法 endpoint 必须拒绝初始化（与空 endpoint 同语义）");
  assert.equal(warnings.length, 1, "必须留下一条告警，实际：" + JSON.stringify(warnings));
  assert.match(warnings[0], /\[ai-debug\]/);
  assert.match(warnings[0], /endpoint/);
  // 告警不得回显用户传入的 endpoint 原文：漏 scheme 的 endpoint 仍可能带
  // ?api_key=… 之类的查询参数，控制台/日志采集会把它带走。
  assert.equal(warnings[0].includes("localhost:8000"), false, "告警不得回显 endpoint 原文");
});

test("init：空 endpoint 仍走既有拒绝路径（回归护栏）", () => {
  const warnings = [];
  const origWarn = console.warn;
  console.warn = function () {
    warnings.push(Array.prototype.join.call(arguments, " "));
  };
  let initedAfter = null;
  try {
    SDK.init({ endpoint: "" });
    initedAfter = SDK._inited;
  } finally {
    console.warn = origWarn;
  }
  assert.equal(initedAfter, false);
  assert.equal(warnings.length, 1);
  assert.match(warnings[0], /endpoint/);
});
