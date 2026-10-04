"""auto_test 自动埋点 init script 的 JS 真实语义测试（Node 执行）。

背景（v1.0.0 修复工作包，先失败测试）：
旧版 ``_build_sdk_init_script`` 产出的 JS 在新文档创建早期（``document.head``
与 ``document.documentElement`` 均为 null）执行 ``appendChild`` 抛 TypeError，
被外层 ``catch (e) {}`` 吞掉——SDK 永不加载、早期异常无人观察、无任何可观测
信号。本文件把构造函数产出的 JS 交给真实 Node 运行时执行，用最小 DOM 桩验证
修复后的行为契约：

1. DOM 未建立时不静默失效：等待 documentElement 就绪后再挂载 script 标签；
2. 页面早期（SDK 加载完成前）console.error / error / unhandledrejection
   只进缓冲、不做任何"回放"：不得二次触发页面自有 error 监听器、不得二次输出
   页面 console.error；早期原始项由 ``__LUJO_SDK_DRAIN__`` 如实取回（单次排水）；
3. ``window.__LUJO_SDK_STATE__`` 暴露可观测阶段（buffering/loading/ready/failed）
   与 drained 计数（不再有 replayed）；
4. 幂等：同一文档重复执行不二次挂载、不二次 init；
5. DOM 长期不可用时以 failed 状态收场（有上限等待，不无限轮询）。

运行方式：pytest 内部落盘 harness 到 pytest 临时目录后 ``node harness.js``
执行（Node 22 为项目基线；node 不可用时按环境缺失惯例 skip）。
"""
import json
import shutil
import subprocess

import pytest

from app.config import settings

# ── Node harness：模拟"新文档刚创建"的页面环境 ──
# 关键模拟语义（与真实浏览器/ai-debug.js 对齐）：
# - document.head / documentElement 初始为 null，_ready 置 True 后出现；
# - script 标签 onload 后 window.AiDebug 出现（模拟 route fulfill 的 SDK 加载）；
# - AiDebug.init 幂等守卫 + 包装 console.error（记录后调原实现）+ window.onerror；
# - window.dispatchEvent('error') 同时触发 window.onerror（浏览器语义）；
# - 页面自有 error 监听器 / console.error 包装在 init script 之前安装，
#   用于证明采集链路不再二次触发页面监听器、不再重复输出 console。
_HARNESS_JS = r"""'use strict';
const fs = require('fs');
const script = fs.readFileSync(process.argv[2], 'utf8');
const scenario = process.argv[3] || 'empty-dom';
const waitMsOverride = Number(process.argv[4] || 0) || null;
const settleMs = Number(process.argv[5] || 1200);

// 页面自有 console.error 包装：在 init script 之前安装，记录每次真实输出。
const realConsoleError = console.error.bind(console);
const pageConsoleCalls = [];
console.error = function () {
  pageConsoleCalls.push(Array.prototype.join.call(arguments, ' '));
  return realConsoleError.apply(console, arguments);
};

const appended = [];
const fakeRoot = { appendChild(el) { appended.push(el); el._attached = true; } };
const document = {
  _ready: false,
  get head() { return document._ready ? fakeRoot : null; },
  get documentElement() { return document._ready ? fakeRoot : null; },
  createElement(tag) { return { tagName: tag, src: '', onload: null, onerror: null }; },
};

const window = { console };
if (waitMsOverride) window.__LUJO_SDK_WAIT_MS__ = waitMsOverride;

const listeners = {};
window.addEventListener = function (type, fn) {
  (listeners[type] = listeners[type] || []).push(fn);
};

// 页面自有 window error 监听器：记录每次被调用时的消息。
const pageErrorMessages = [];
window.addEventListener('error', function (ev) {
  pageErrorMessages.push(String((ev && ev.message) || ''));
});

window.dispatchEvent = function (ev) {
  (listeners[ev.type] || []).slice().forEach(function (fn) { fn(ev); });
  if (ev.type === 'error' && typeof window.onerror === 'function') {
    window.onerror(ev.message, ev.filename || '', ev.lineno || 0, ev.colno || 0, ev.error);
  }
  return true;
};

class ErrorEventShim {
  constructor(type, opts) {
    opts = opts || {};
    this.type = type;
    this.message = opts.message || '';
    this.filename = opts.filename || '';
    this.lineno = opts.lineno || 0;
    this.colno = opts.colno || 0;
    this.error = opts.error || null;
  }
}
globalThis.ErrorEvent = ErrorEventShim;

const sdkInitCalls = [];
const sdkConsoleEvents = [];
const sdkErrorEvents = [];

function loadFakeSdk() {
  const AiDebug = {
    _inited: false,
    init(opts) {
      if (this._inited) return;
      this._inited = true;
      sdkInitCalls.push(opts || {});
      const prev = console.error;
      console.error = function () {
        sdkConsoleEvents.push(Array.prototype.join.call(arguments, ' '));
        return prev.apply(console, arguments);
      };
      window.onerror = function (msg) {
        sdkErrorEvents.push(String(msg));
        return false;
      };
    },
  };
  window.AiDebug = AiDebug;
}

function fireOnloads() {
  appended.filter(function (s) { return typeof s.onload === 'function'; })
    .forEach(function (s) { s.onload(); });
}

function runInitScript() {
  const fn = new Function('window', 'document', 'console', script);
  fn(window, document, console);
}

// ready-dom：init script 运行时 documentElement 已存在（真实浏览器中
// addScriptToEvaluateOnNewDocument 的常见时序）；其余场景先空 DOM 再就绪。
if (scenario === 'ready-dom') {
  document._ready = true;
}
runInitScript();

if (scenario === 'empty-dom' || scenario === 'both') {
  // 页面脚本：早期故障发生在 DOM 建立与 SDK 加载之前
  console.error('EARLY-CONSOLE M1');
  window.dispatchEvent(new ErrorEventShim('error', { message: 'EARLY-PAGEERROR M2' }));
  window.dispatchEvent(new ErrorEventShim('error', {
    message: 'EARLY-PAGEERROR-WITHERR',
    filename: 'http://example.test/app.js',
    lineno: 42,
    colno: 7,
    error: new Error('EARLY-PAGEERROR-WITHERR'),
  }));
  setTimeout(function () { document._ready = true; }, 60);
  setTimeout(function () { loadFakeSdk(); fireOnloads(); }, 160);
  setTimeout(function () { console.error('LATE-CONSOLE M3'); }, 420);
} else if (scenario === 'ready-dom') {
  setTimeout(function () { loadFakeSdk(); fireOnloads(); }, 80);
  setTimeout(function () { console.error('LATE-CONSOLE M3'); }, 260);
}
// never-dom：documentElement 永不出现，等待有上限后 failed 收场

setTimeout(function () {
  const appendedBefore = appended.length;
  runInitScript(); // 幂等：重复执行不得二次挂载/二次 init
  setTimeout(function () {
    // 排水契约：第一次取回全部早期原始项，第二次必须为空（每个原始事件只排空一次）
    const drainFirst = (typeof window.__LUJO_SDK_DRAIN__ === 'function')
      ? window.__LUJO_SDK_DRAIN__() : null;
    const drainSecond = (typeof window.__LUJO_SDK_DRAIN__ === 'function')
      ? window.__LUJO_SDK_DRAIN__() : null;
    console.log('RESULT ' + JSON.stringify({
      appended: appended.length,
      appendedBeforeRerun: appendedBefore,
      sdkInitCount: sdkInitCalls.length,
      sdkEndpoint: sdkInitCalls.length ? (sdkInitCalls[0].endpoint || null) : null,
      sdkConsole: sdkConsoleEvents.map(String),
      sdkErrors: sdkErrorEvents.map(String),
      state: window.__LUJO_SDK_STATE__ || null,
      ready: !!window.__LUJO_SDK_READY__,
      drainFirst: drainFirst,
      drainSecond: drainSecond,
      pageErrorMessages: pageErrorMessages,
      pageConsoleCalls: pageConsoleCalls,
    }));
  }, 150);
}, settleMs);
"""


def _run_harness(monkeypatch, scenario: str, *, wait_ms: int = 0, settle_ms: int = 1200) -> dict:
    """产出 init script → Node 真实执行 → 返回 harness 的 RESULT JSON。"""
    from app.mcp.tools.auto_test_api import _build_sdk_init_script

    monkeypatch.setattr(settings, "auto_inject_sdk", True)
    monkeypatch.setattr(settings, "http_host", "127.0.0.1")
    monkeypatch.setattr(settings, "http_port", 8999)
    script = _build_sdk_init_script()
    assert isinstance(script, str) and script, "开关开启时必须产出注入脚本"

    node = shutil.which("node")
    if not node:
        pytest.skip("node 不可用，无法真实执行 init script JS")

    import pathlib
    import tempfile

    with tempfile.TemporaryDirectory(prefix="lujo-initjs-") as td:
        td_path = pathlib.Path(td)
        harness = td_path / "harness.js"
        harness.write_text(_HARNESS_JS, encoding="utf-8")
        script_file = td_path / "init-script.js"
        script_file.write_text(script, encoding="utf-8")
        proc = subprocess.run(
            [node, str(harness), str(script_file), scenario, str(wait_ms), str(settle_ms)],
            capture_output=True, text=True, timeout=40, cwd=td,
        )
    for line in proc.stdout.splitlines():
        if line.startswith("RESULT "):
            return json.loads(line[len("RESULT "):])
    pytest.fail(f"harness 未产出 RESULT（exit={proc.returncode}）"
                f"：stdout={proc.stdout!r} stderr={proc.stderr[-2000:]!r}")


def test_survives_empty_dom_and_captures_early_faults(monkeypatch):
    """B1/B6 断点：DOM 未建立时不得静默失效；早期事件只进缓冲、由排水取回。"""
    r = _run_harness(monkeypatch, "empty-dom")
    # 注入存活：script 标签最终挂载且只挂载一次
    assert r["appended"] == 1, f"DOM 空洞期 appendChild 失败被吞：{r}"
    assert r["sdkInitCount"] == 1, f"SDK 应加载并 init 恰好一次：{r}"
    assert r["sdkEndpoint"] == "http://127.0.0.1:8999"
    # SDK ready 之后的常规采集不回归
    assert any("LATE-CONSOLE" in s for s in r["sdkConsole"]), r["sdkConsole"]
    # 可观测状态 + ready 标记
    assert r["ready"] is True
    state = r["state"] or {}
    assert state.get("phase") == "ready", r["state"]
    # 排水契约（取代旧"回放"契约）：早期原始项第一次排水取回、第二次为空
    first = r["drainFirst"] or []
    second = r["drainSecond"] or []
    assert len(first) >= 2, f"早期事件必须保留在缓冲中等待排水：{r}"
    assert not second, f"排水必须清空缓冲，第二次应为空：{r}"
    assert "replayed" not in state, f"状态字段应以 drained 取代 replayed：{state}"
    assert state.get("drained") == len(first), f"drained 计数必须如实：{r['state']}"
    msgs = [str(it.get("message") or "") for it in first]
    assert any("EARLY-CONSOLE" in m for m in msgs), msgs
    assert any("EARLY-PAGEERROR M2" in m for m in msgs), msgs


def test_early_capture_does_not_retrigger_page_listeners_or_console(monkeypatch):
    """R3 核心断言：早期采集不得改变页面既有监听器/错误处理/日志行为。"""
    r = _run_harness(monkeypatch, "empty-dom")
    page_msgs = r["pageErrorMessages"]
    # 页面自有 error 监听器对每个真实早期异常只应被调用一次
    assert sum(1 for m in page_msgs if "EARLY-PAGEERROR M2" in m) == 1, page_msgs
    assert sum(1 for m in page_msgs if "EARLY-PAGEERROR-WITHERR" in m) == 1, page_msgs
    # 页面自有 console.error 包装调用次数 == 页面真实 console.error 调用次数
    calls = r["pageConsoleCalls"]
    assert len(calls) == 2, f"不得重复输出页面 console：{calls}"
    assert sum(1 for c in calls if "EARLY-CONSOLE" in c) == 1, calls
    assert sum(1 for c in calls if "LATE-CONSOLE" in c) == 1, calls
    assert not any("lujo-early" in c for c in calls), f"不得注入回放标记：{calls}"


def test_drained_early_events_keep_real_fields_without_fabrication(monkeypatch):
    """R3：携带真实 Error 的早期异常保留 exc_type/stack/file/line；缺失字段不伪造。"""
    r = _run_harness(monkeypatch, "empty-dom")
    first = r["drainFirst"] or []
    plain = next(it for it in first if "EARLY-PAGEERROR M2" in str(it.get("message")))
    assert "exc_type" not in plain and "stack" not in plain, plain
    assert "file" not in plain and "line" not in plain, plain
    real = next(it for it in first if "EARLY-PAGEERROR-WITHERR" in str(it.get("message")))
    assert real.get("exc_type") == "Error", real
    assert real.get("file") == "http://example.test/app.js", real
    assert real.get("line") == 42, real
    assert real.get("column") == 7, real
    assert "EARLY-PAGEERROR-WITHERR" in str(real.get("stack") or ""), real
    console_item = next(it for it in first if "EARLY-CONSOLE" in str(it.get("message")))
    assert console_item.get("kind") == "console", console_item
    assert "stack" not in console_item and "exc_type" not in console_item, console_item


def test_init_script_idempotent_rerun(monkeypatch):
    """幂等：同一文档重复执行 init script 不得二次挂载或二次 init。"""
    r = _run_harness(monkeypatch, "empty-dom")
    assert r["appendedBeforeRerun"] == 1
    assert r["appended"] == 1, f"重复执行导致二次挂载：{r}"
    assert r["sdkInitCount"] == 1, f"重复执行导致二次 init：{r}"


def test_never_ready_dom_reports_failure_state(monkeypatch):
    """DOM 长期不可用：有上限等待后以 failed 状态收场，不得无限轮询或静默。"""
    r = _run_harness(monkeypatch, "never-dom", wait_ms=250, settle_ms=700)
    state = r["state"] or {}
    assert state.get("phase") == "failed", f"应报告 failed（有界等待后）：{r}"


def test_ready_dom_immediate_mount(monkeypatch):
    """守卫用例（DOM 已就绪的挂载/采集为既有行为，修复前亦通过，如实记录）。"""
    r = _run_harness(monkeypatch, "ready-dom")
    assert r["appended"] == 1
    assert r["sdkInitCount"] == 1
    assert any("LATE-CONSOLE" in s for s in r["sdkConsole"])


# ── F1：finish() 的冲刷包装返回值语义（reviewer task-2 发现） ──
# 修正前 _FLUSH_SDK_JS 只在 catch 里 return false；window.AiDebug 缺失
# （SDK 从未初始化）时仍 return true → flush_ok=True → delivery 可能报
# complete。现契约：缺 SDK / 非对象、或内部异常，一律 false。
# 此处用真实 Node 执行生产表达式本身（单一事实源）。

_FLUSH_PROBE_JS = r"""'use strict';
const fs = require('fs');
const expr = fs.readFileSync(process.argv[2], 'utf8').trim();
const kind = process.argv[3] || 'missing';

function makeWindow(kind) {
  if (kind === 'missing') return {};
  if (kind === 'not-object') return { AiDebug: 42 };
  const calls = [];
  const w = { __calls: calls };
  if (kind === 'full') {
    w.AiDebug = {
      _flushBatch(v) { calls.push(['_flushBatch', v]); },
      destroy(opts) { calls.push(['destroy', !!(opts && opts.flush)]); },
    };
  } else if (kind === 'throwing') {
    w.AiDebug = { _flushBatch() { throw new Error('boom'); }, destroy() {} };
  }
  return w;
}

const window = makeWindow(kind);
const fn = new Function('window', 'return ' + expr);
const ret = fn(window);
console.log('RESULT ' + JSON.stringify({ ret: ret, calls: window.__calls || [] }));
"""


def _run_flush_probe(kind: str) -> dict:
    """真实 Node 执行生产冲刷包装表达式，返回 {ret, calls}。"""
    from app.mcp.tools.auto_test_api import _FLUSH_SDK_JS

    node = shutil.which("node")
    if not node:
        pytest.skip("node 不可用，无法真实执行冲刷包装 JS")

    import pathlib
    import tempfile

    with tempfile.TemporaryDirectory(prefix="lujo-flushjs-") as td:
        td_path = pathlib.Path(td)
        harness = td_path / "flush-harness.js"
        harness.write_text(_FLUSH_PROBE_JS, encoding="utf-8")
        expr_file = td_path / "flush-expr.js"
        expr_file.write_text(_FLUSH_SDK_JS, encoding="utf-8")
        proc = subprocess.run(
            [node, str(harness), str(expr_file), kind],
            capture_output=True, text=True, timeout=40, cwd=td,
        )
    for line in proc.stdout.splitlines():
        if line.startswith("RESULT "):
            return json.loads(line[len("RESULT "):])
    pytest.fail(f"flush harness 未产出 RESULT（exit={proc.returncode}）："
                f"stdout={proc.stdout!r} stderr={proc.stderr[-2000:]!r}")


def test_flush_wrapper_returns_false_without_sdk():
    """F1 红相：window.AiDebug 缺失或非对象时冲刷包装必须返回 false。

    否则 finish() 拿到 flush_ok=True，会在 SDK 从未初始化（页内 state.phase
    = failed）时仍报 delivery=complete。
    """
    missing = _run_flush_probe("missing")
    assert missing["ret"] is False, f"缺 window.AiDebug 必须返回 false：{missing}"
    assert missing["calls"] == [], missing

    not_object = _run_flush_probe("not-object")
    assert not_object["ret"] is False, f"AiDebug 非对象必须返回 false：{not_object}"


def test_flush_wrapper_returns_true_only_after_real_flush():
    """F1 守卫：AiDebug 齐备时确实排空并返回 true；钩子抛异常仍返回 false。"""
    full = _run_flush_probe("full")
    assert full["ret"] is True, full
    assert ["_flushBatch", False] in full["calls"], full
    assert ["destroy", True] in full["calls"], full

    throwing = _run_flush_probe("throwing")
    assert throwing["ret"] is False, throwing
