"""
MCP 工具：auto_test —— 自动遍历页面所有可交互元素并捕获缺陷。

仅暴露同步入口，内部新开事件循环跑 Playwright 异步 API，
避免与调用方的事件循环冲突。
"""
import logging

AUTO_TEST_DEF = {
    "name": "auto_test",
    "description": (
        "自动遍历页面所有可交互元素（按钮/链接/输入框），"
        "依次执行点击并监听控制台错误和网络 4xx/5xx。"
        "需要 url、不需要 request_id；不需要手动指定选择器，"
        "适合快速验收 AI 生成的前端页面、批量发现「点了没反应」的静默问题；"
        "定位单个已知问题请先用 diagnose_issue。"
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


async def _run(url: str, max_actions: int, capture_console: bool, capture_network: bool) -> dict:
    """内部 async 函数：用 Playwright 异步 API 执行遍历"""
    from playwright.async_api import async_playwright

    # FIX(v0.7.1-b9-3): 遍历期间 console/network 错误列表无界增长——此前只在返回前
    # 截断 [:20]，遍历中页面刷大量错误/4xx 会无界累积内存；现采集即限长（保前 N 条）。
    _MAX_CAPTURED_ERRORS = 100
    console_errors = []
    network_errors = []
    executed = []
    skipped = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page()

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
        return {"error": "playwright 未安装。安装: pip install playwright && playwright install chromium"}

    url = arguments["url"]
    from app.runtime.verifier.ui_runner import is_safe_url
    ok, reason = is_safe_url(url)
    if not ok:
        return {"error": f"URL 被安全策略拒绝：{reason}", "url": url}
    max_actions = min(arguments.get("max_actions", 20), 50)
    cc = arguments.get("capture_console", True)
    cn = arguments.get("capture_network", True)

    return await _run(url, max_actions, cc, cn)
