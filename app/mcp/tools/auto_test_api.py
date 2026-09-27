"""
MCP 工具：auto_test —— 自动遍历页面所有可交互元素并捕获缺陷。

仅暴露同步入口，内部新开事件循环跑 Playwright 异步 API，
避免与调用方的事件循环冲突。
"""
import json
import logging

AUTO_TEST_DEF = {
    "name": "auto_test",
    "description": (
        "本工具需要浏览器采集能力（Playwright）；未启用时调用会返回 "
        "CAPABILITY_MISSING 与启用指引。"
        "【前端现场采集入口】当用户报告前端问题（页面异常、白屏、『点了没反应』、"
        "接口表现不对、疑似静默失败）而服务端还没有任何上报现场时，应先用本工具"
        "打开目标页面自动遍历并采集真实运行现场，再分析修复。"
        "本机开发服务器（http://localhost:3000 等）默认放行，无需任何额外配置。"
        "自动遍历页面所有可交互元素（按钮/链接/输入框），"
        "依次执行点击并监听控制台错误和网络 4xx/5xx。"
        "遍历期间自动为页面注入采集脚本（Browser SDK），"
        "发现的错误将进入 Lujo 现场存储（可用 diagnose_issue 查询）。"
        "需要 url、不需要 request_id；不需要手动指定选择器，"
        "适合快速验收 AI 生成的前端页面、批量发现「点了没反应」的静默问题；"
        "已有上报现场时定位单个已知问题请先用 diagnose_issue。"
        "需要 Playwright（pip install playwright && playwright install chromium）。"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "要测试的页面 URL"},
            "max_actions": {"type": "integer", "default": 20},
            "capture_console": {"type": "boolean", "default": True},
            "capture_network": {"type": "boolean", "default": True},
        },
        "required": ["url"],
    },
}

logger = logging.getLogger("lujo-mcp.auto_test")


def is_available() -> bool:
    """返回当前 Python 运行时是否安装了 Playwright。"""
    try:
        from playwright.async_api import async_playwright as _  # noqa: F401
    except ImportError:
        return False
    return True


def _build_sdk_init_script() -> str | None:
    """构造 auto_test 页面自动埋点的 Playwright init script（v0.9.8）。

    产出自包含 JS：动态创建 ``<script src="http://{host}:{port}/ai-debug.js">``
    插入 document（init script 运行时 document.head 可能为 null，挂
    document.documentElement），script.onload 里执行
    ``AiDebug.init({ endpoint })``，另以短轮询兜底确保 init 执行
    （SDK init 自身幂等，重复调用无害）。

    - endpoint 使用 settings.http_host / http_port 实时值（默认 127.0.0.1:8710），
      SDK 采集的 console.error / 网络失败 / 静默失败经 POST /ingest/* 回流
      Lujo 存储，随后可经 diagnose_issue 查询；
    - 返回 None 表示注入被关闭（settings.auto_inject_sdk=False），调用方
      必须完全跳过注入；
    - SDK 加载失败（如被测页面 CSP 拦截、Lujo 未启动）静默降级，不干扰
      被测页面本身。
    """
    from app.config import settings

    if not settings.auto_inject_sdk:
        return None
    endpoint = f"http://{settings.http_host}:{settings.http_port}"
    # json.dumps 把 endpoint 安全编码为 JS 字符串字面量（引号/特殊字符转义）
    endpoint_js = json.dumps(endpoint)
    return (
        "(function () {\n"
        "  try {\n"
        "    if (window.__LUJO_SDK_INJECTED__) { return; }\n"
        "    window.__LUJO_SDK_INJECTED__ = true;\n"
        f"    var ENDPOINT = {endpoint_js};\n"
        "    var SDK_URL = ENDPOINT + '/ai-debug.js';\n"
        "    var initSdk = function () {\n"
        "      try {\n"
        "        if (window.AiDebug && typeof window.AiDebug.init === 'function') {\n"
        "          window.AiDebug.init({ endpoint: ENDPOINT });\n"
        "        }\n"
        "      } catch (e) {}\n"
        "    };\n"
        "    var s = document.createElement('script');\n"
        "    s.src = SDK_URL;\n"
        "    s.onload = initSdk;\n"
        "    s.onerror = function () {};\n"
        "    (document.head || document.documentElement).appendChild(s);\n"
        "    var tries = 0;\n"
        "    var timer = setInterval(function () {\n"
        "      tries += 1;\n"
        "      if (window.AiDebug) { initSdk(); }\n"
        "      if ((window.AiDebug && window.AiDebug._inited) || tries >= 20) {\n"
        "        clearInterval(timer);\n"
        "      }\n"
        "    }, 250);\n"
        "  } catch (e) {}\n"
        "})();"
    )


async def _run(url: str, max_actions: int, capture_console: bool, capture_network: bool) -> dict:
    """内部 async 函数：用 Playwright 异步 API 执行遍历"""
    from playwright.async_api import async_playwright

    # v0.9.8 浏览器回退链：playwright chromium → 系统 Chrome → 系统 Edge；
    # 冻结发行版不随包分发 chromium，靠系统浏览器通道兜底。
    # 探测是纯路径存在性检查（不 spawn driver），在事件循环内同步调用安全。
    from app.runtime.verifier.browser_launcher import resolve_launch_kwargs
    launch_kwargs = resolve_launch_kwargs()
    if launch_kwargs is None:
        from app.runtime.verifier.ui_runner import capability_missing_payload
        return capability_missing_payload()

    # FIX(v0.7.1-b9-3): 遍历期间 console/network 错误列表无界增长——此前只在返回前
    # 截断 [:20]，遍历中页面刷大量错误/4xx 会无界累积内存；现采集即限长（保前 N 条）。
    _MAX_CAPTURED_ERRORS = 100
    console_errors = []
    network_errors = []
    executed = []
    skipped = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, **launch_kwargs)
        try:
            page = await browser.new_page()

            # v0.9.8 auto_test 自动埋点：把 Browser SDK init script 装到本页
            # （add_init_script 在每次导航的页面脚本之前执行，对 goto 及后续
            # 导航都生效）。页面 console.error / 网络失败 / 静默失败经 SDK
            # POST /ingest/* 自动回流 Lujo 存储（diagnose_issue 可查），
            # 用户 HTML 零改动。开关关闭时构造函数返回 None，完全跳过。
            # 本文件唯一的 page 创建点即此处；Lujo 自身监听回环，SDK 回传
            # 请求经 _install_ssrf_guard 的 loopback 豁免放行。
            sdk_init_script = _build_sdk_init_script()
            if sdk_init_script is not None:
                await page.add_init_script(script=sdk_init_script)

            # SSRF 逐跳守卫：初始 URL 经 is_safe_url 校验，但 goto 重定向 / 点击触发的
            # 导航默认不校验，攻击者可借 302/JS 跳转内网绕过。复用 ui_runner 守卫逐跳拦截。
            from app.runtime.verifier.ui_runner import _install_ssrf_guard
            _install_ssrf_guard(page.context)

            if capture_console:
                page.on("console", lambda msg: (
                    msg.type in ("error", "warning") and
                    len(console_errors) < _MAX_CAPTURED_ERRORS and
                    console_errors.append({"type": msg.type, "text": msg.text})
                ) if msg.type in ("error", "warning") else None)

            if capture_network:
                page.on("response", lambda resp: (
                    resp.status >= 400 and
                    len(network_errors) < _MAX_CAPTURED_ERRORS and
                    network_errors.append({"url": resp.url, "status": resp.status})
                ) if resp.status >= 400 else None)

            try:
                await page.goto(url, wait_until="networkidle", timeout=30000)
            except Exception as e:
                logger.error(str(e), exc_info=True)
                return {"error": "Tool execution failed", "url": url}

            els = await page.query_selector_all(
                "button, a[href], input:not([type=hidden]), select, textarea, "
                "[role=button], [onclick]"
            )
            found = len(els)

            for idx, el in enumerate(els):
                if idx >= max_actions:
                    skipped.append({"index": idx, "reason": "超过最大交互数"})
                    continue
                try:
                    tag = await el.evaluate("el => el.tagName.toLowerCase()")
                    text = (await el.inner_text() or "")[:50]
                    hint = await el.evaluate("el => ({ tag: el.tagName, id: el.id, cls: el.className })")

                    if not await el.is_visible():
                        skipped.append({"index": idx, "tag": tag, "text": text, "reason": "不可见"})
                        continue

                    before = page.url
                    await el.click(timeout=5000)
                    await page.wait_for_timeout(500)
                    after = page.url

                    executed.append({
                        "index": idx, "tag": tag, "text": text,
                        "id": hint.get("id", ""), "class": hint.get("cls", "")[:60],
                        "changed_url": before != after,
                    })
                except Exception as e:
                    logger.error(str(e), exc_info=True)
                    executed.append({"index": idx, "error": "Tool execution failed", "silent_failure": False})

            return {
                "url": url,
                "found_elements": found,
                "executed_count": len(executed),
                "skipped_count": len(skipped),
                "executed": executed,
                "console_errors": console_errors[:20],
                "network_errors": network_errors[:20],
                "silent_failure_detected": len(network_errors) > 0
                    or any(e.get("silent_failure") for e in executed),
            }
        finally:
            # FIX: C2 —— 正常/异常/取消都及时关闭浏览器，避免 chromium 进程残留
            # （async_playwright 上下文退出为兜底，此处保证尽早释放）
            try:
                await browser.close()
            except Exception:
                logger.debug("auto_test 关闭浏览器失败（可能已关闭）", exc_info=True)


async def auto_test_handler(arguments: dict) -> dict:
    """异步入口 —— 直接在当前事件循环中运行，避免嵌套循环冲突"""
    try:
        from playwright.async_api import async_playwright as _  # noqa: F401
    except ImportError:
        # P0-B：能力缺失=执行失败——与 verify_ui 共用统一 CAPABILITY_MISSING
        # 载荷；本工具在 heavy 子进程执行，dict 经 IPC 回传，不 raise。
        from app.runtime.verifier.ui_runner import capability_missing_payload

        return capability_missing_payload()

    url = arguments["url"]
    from app.runtime.verifier.ui_runner import is_safe_url
    ok, reason = is_safe_url(url)
    if not ok:
        return {"error": f"URL 被安全策略拒绝：{reason}", "url": url}
    max_actions = min(arguments.get("max_actions", 20), 50)
    cc = arguments.get("capture_console", True)
    cn = arguments.get("capture_network", True)

    return await _run(url, max_actions, cc, cn)
