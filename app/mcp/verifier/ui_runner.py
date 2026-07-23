"""
UI 自动遍历引擎（FR14）—— 基于 Playwright 的前端自动化验证。

可选依赖：playwright（未安装时导入失败 → 模块不可用，不影响其他功能）。

输入规范（spec.kind="ui"）：
  {
    kind: "ui",
    target: "https://example.com/page",
    expect: {
      state_change: { route_change?: str, dom_change?: str },
      interactions: [
        { action: "click"|"type"|"navigate", selector: str, value?: str,
          expect: { state_change?: dict, no_response?: bool } }
      ]
    }
  }

输出：{matched, diffs, silent_failure} —— 复用 assert_engine 契约。
"""

import ipaddress
import logging
import socket
from urllib.parse import urlparse

from app.config import settings

logger = logging.getLogger("ai-debug-mcp.ui_runner")


def is_safe_url(url: str) -> tuple[bool, str]:
    """SEC-02：校验 Playwright 目标 URL，防 SSRF。

    仅允许 http/https；默认拒绝回环/私网/链路本地（云元数据 169.254.x）/保留地址。
    可经 settings.ui_url_allow_private 放开，或 settings.ui_url_allowlist 精确放行主机。
    返回 (是否安全, 拒绝原因)。
    """
    try:
        parsed = urlparse(url or "")
    except Exception:
        return False, "URL 解析失败"
    if parsed.scheme not in ("http", "https"):
        return False, f"仅允许 http/https，拒绝 scheme={parsed.scheme!r}"
    host = parsed.hostname or ""
    if not host:
        return False, "URL 缺少主机名"
    allowlist = {h.strip().lower() for h in (settings.ui_url_allowlist or "").split(",") if h.strip()}
    if host.lower() in allowlist:
        return True, ""
    if settings.ui_url_allow_private:
        return True, ""
    # 解析主机为 IP 并分类；解析失败则拒绝（无法核实安全性）
    try:
        ips = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except Exception:
        return False, f"主机无法解析，默认拒绝：{host}"
    for ip in ips:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False, f"非法 IP：{ip}"
        if (addr.is_loopback or addr.is_private or addr.is_link_local
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified):
            return False, (f"拒绝内网/回环/元数据地址：{host}→{ip}"
                           "（本地联调请设 UI_URL_ALLOW_PRIVATE=true 或加入 UI_URL_ALLOWLIST）")
    return True, ""


_PLAYWRIGHT_AVAILABLE = False
try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout  # noqa: F401
    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    pass


def is_available() -> bool:
    """检查 Playwright 是否可用。"""
    return _PLAYWRIGHT_AVAILABLE


def run_ui_verification(spec: dict, timeout_ms: int = 30000) -> dict:
    """
    按 UI 规范自动遍历页面并验证交互结果。

    Args:
        spec: 规范 {kind:"ui", target, expect:{state_change?, interactions:[...]}}
        timeout_ms: 单个操作超时（毫秒）

    Returns:
        {matched, diffs, silent_failure, interactions: [{action, matched, diffs, silent_failure}]}
    """
    if not _PLAYWRIGHT_AVAILABLE:
        return {
            "matched": False,
            "diffs": [],
            "silent_failure": False,
            "error": "playwright 未安装。安装方法: pip install playwright && playwright install chromium",
        }

    target_url = spec.get("target", "")
    if not target_url:
        return {
            "matched": False,
            "diffs": [{"field": "target", "expected": "valid URL", "actual": target_url}],
            "silent_failure": False,
        }

    ok, reason = is_safe_url(target_url)
    if not ok:
        return {
            "matched": False,
            "diffs": [{"field": "target", "expected": "安全的 http(s) URL", "actual": reason}],
            "silent_failure": False,
        }

    interactions = (spec.get("expect") or {}).get("interactions") or []
    global_expect = (spec.get("expect") or {}).get("state_change") or {}

    all_matched = True
    all_diffs = []
    interaction_results = []

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_default_timeout(timeout_ms)

            # 导航到目标页面
            try:
                page.goto(target_url, wait_until="domcontentloaded")
            except Exception as e:
                return {
                    "matched": False,
                    "diffs": [{"field": "navigation", "expected": target_url, "actual": str(e)}],
                    "silent_failure": False,
                }

            # 验证全局状态
            if global_expect:
                gd = _verify_state(page, global_expect)
                if gd.get("diffs"):
                    all_diffs.extend(gd["diffs"])
                interaction_results.append({
                    "action": "navigate",
                    "matched": gd["matched"],
                    "diffs": gd.get("diffs", []),
                    "silent_failure": gd.get("silent_failure", False),
                })

            # 遍历交互
            for idx, intn in enumerate(interactions):
                action = intn.get("action", "click")
                selector = intn.get("selector", "")
                value = intn.get("value", "")
                expect = intn.get("expect") or {}

                try:
                    ir = _execute_interaction(page, action, selector, value, expect, timeout_ms)
                except Exception as e:
                    ir = {
                        "action": action,
                        "matched": False,
                        "diffs": [{"field": f"interactions[{idx}].{action}",
                                    "expected": "success", "actual": str(e)}],
                        "silent_failure": False,
                    }

                if not ir.get("matched"):
                    all_matched = False
                if ir.get("diffs"):
                    all_diffs.extend(ir["diffs"])
                interaction_results.append(ir)

            browser.close()

    except Exception as e:
        logger.exception("Playwright 执行异常")
        return {
            "matched": False,
            "diffs": [{"field": "playwright", "expected": "execution success", "actual": str(e)}],
            "silent_failure": False,
            "interactions": interaction_results,
        }

    # 静默失败判定：有交互失败，但不是因为异常
    silent_failure = (not all_matched) and not any(
        "error" in ir or "crash" in str(ir.get("diffs", "")).lower()
        for ir in interaction_results
    )

    return {
        "matched": all_matched,
        "diffs": all_diffs,
        "silent_failure": silent_failure,
        "interactions": interaction_results,
    }


def _execute_interaction(page, action: str, selector: str, value: str,
                         expect: dict, timeout_ms: int) -> dict:
    """执行单个 UI 交互并验证。"""
    if not selector:
        return {"action": action, "matched": False,
                "diffs": [{"field": "selector", "expected": "CSS selector", "actual": ""}],
                "silent_failure": False}

    # 检查元素是否存在
    try:
        el = page.wait_for_selector(selector, timeout=timeout_ms)
    except PlaywrightTimeout:
        return {
            "action": action,
            "matched": False,
            "diffs": [{"field": f"{action}({selector})",
                        "expected": "element found", "actual": "element not found"}],
            "silent_failure": True,  # 元素不存在 = 静默失败
        }

    # 执行动作
    prev_url = page.url
    try:
        if action == "click":
            el.click()
        elif action == "type":
            el.fill(value)
        elif action == "navigate":
            ok, reason = is_safe_url(value)
            if not ok:
                return {"action": action, "matched": False,
                        "diffs": [{"field": f"navigate({value})",
                                    "expected": "安全的 http(s) URL", "actual": reason}],
                        "silent_failure": False}
            page.goto(value, wait_until="domcontentloaded")
        elif action == "hover":
            el.hover()
        elif action == "select":
            page.select_option(selector, value)
        else:
            return {"action": action, "matched": False,
                    "diffs": [{"field": action, "expected": "valid action", "actual": action}],
                    "silent_failure": False}
    except Exception as e:
        return {
            "action": action,
            "matched": False,
            "diffs": [{"field": f"{action}({selector})",
                        "expected": "interaction success", "actual": str(e)}],
            "silent_failure": False,
        }

    # 验证结果
    return _verify_state(page, expect, action=f"{action}({selector})", prev_url=prev_url)


def _verify_state(page, expect: dict, action: str = "", prev_url: str = "") -> dict:
    """验证页面状态是否符合期望。"""
    diffs = []
    state_change = expect.get("state_change") or {}

    # route_change
    if "route_change" in state_change:
        exp_route = state_change["route_change"]
        try:
            page.wait_for_url(exp_route, timeout=5000)
        except Exception:
            current = page.url
            if prev_url and current == prev_url:
                # URL 没有变化 → 可能是静默失败
                diffs.append({
                    "field": f"{action}.route_change",
                    "expected": exp_route,
                    "actual": current,
                })

    # dom_change
    if "dom_change" in state_change:
        dom_sel = state_change["dom_change"]
        try:
            page.wait_for_selector(dom_sel, timeout=5000)
        except Exception:
            diffs.append({
                "field": f"{action}.dom_change",
                "expected": f"selector '{dom_sel}' appears",
                "actual": f"selector '{dom_sel}' not found",
            })

    # no_response 期望（无反应即正确）
    no_response = expect.get("no_response", False)
    if no_response:
        # 无 diffs 时 matched=true（无反应符合预期）
        pass

    matched = len(diffs) == 0

    # 静默: matched=false 且不是 Playwright 崩溃
    silent_failure = (not matched) and all(
        "timeout" not in str(d.get("actual", "")).lower() or "element not found" in str(d.get("actual", ""))
        for d in diffs
    )

    return {
        "action": action or "verify",
        "matched": matched,
        "diffs": diffs,
        "silent_failure": silent_failure,
    }
