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
        "不需要手动指定选择器，适合快速验收 AI 生成的前端页面。"
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

logger = logging.getLogger("ai-debug-mcp.auto_test")


async def _run(url: str, max_actions: int, capture_console: bool, capture_network: bool) -> dict:
    """内部 async 函数：用 Playwright 异步 API 执行遍历"""
    from playwright.async_api import async_playwright

    console_errors = []
    network_errors = []
    executed = []
    skipped = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()

        if capture_console:
            page.on("console", lambda msg: (
                msg.type in ("error", "warning") and
                console_errors.append({"type": msg.type, "text": msg.text})
            ) if msg.type in ("error", "warning") else None)

        if capture_network:
            page.on("response", lambda resp: (
                resp.status >= 400 and
                network_errors.append({"url": resp.url, "status": resp.status})
            ) if resp.status >= 400 else None)

        try:
            await page.goto(url, wait_until="networkidle", timeout=30000)
        except Exception as e:
            logger.error(str(e), exc_info=True)
            await browser.close()
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

        await browser.close()

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


async def auto_test_handler(arguments: dict) -> dict:
    """异步入口 —— 直接在当前事件循环中运行，避免嵌套循环冲突"""
    try:
        from playwright.async_api import async_playwright as _  # noqa: F401
    except ImportError:
        return {"error": "playwright 未安装。安装: pip install playwright && playwright install chromium"}

    url = arguments["url"]
    max_actions = min(arguments.get("max_actions", 20), 50)
    cc = arguments.get("capture_console", True)
    cn = arguments.get("capture_network", True)

    return await _run(url, max_actions, cc, cn)
