"""
UI 自动遍历引擎（FR14）—— 基于 Playwright 的前端自动化验证。

可选依赖：playwright（未安装时导入失败 -> 模块不可用，不影响其他功能）。

输入规范（spec.kind="ui"）：
  {
    kind: "ui",
    target: "https://example.com/page",
    expect: {
      state_change: { route_change?: str, dom_change?: str },
      assertions: [
        { type: "text", selector: "#status", equals?: "done", contains?: "done" },
        { type: "url", equals?: "https://example.com/done", contains?: "/done" }
      ],
      interactions: [
        { action: "click"|"type"|"navigate"|"hover"|"select", selector: str, value?: str,
          expect: { state_change?: dict, assertions?: list, no_response?: bool } }
      ]
    }
  }

输出：沿用 {matched, diffs, silent_failure, interactions}，并补充 security、
assertions 与 failure_evidence 等结构化信息。
"""

import ipaddress
import logging
import socket
from urllib.parse import urlparse

from app.config import settings

logger = logging.getLogger("lujo-mcp.ui_runner")


def inspect_url_security(url: str) -> dict:
    """返回 URL 安全检查的结构化结果。"""
    assessment = {
        "checked_url": url,
        "allowed": False,
        "host": "",
        "scheme": "",
        "rule": "",
        "reason": "",
        "allowlist_match": False,
        "private_network": False,
        "resolved_ips": [],
        "restricted_ip": "",
    }

    try:
        parsed = urlparse(url or "")
    except Exception:
        assessment["rule"] = "parse_error"
        assessment["reason"] = "URL 解析失败"
        return assessment

    assessment["scheme"] = parsed.scheme or ""
    assessment["host"] = parsed.hostname or ""

    if parsed.scheme not in ("http", "https"):
        assessment["rule"] = "scheme_not_allowed"
        assessment["reason"] = f"仅允许 http/https，拒绝 scheme={parsed.scheme!r}"
        return assessment

    if not assessment["host"]:
        assessment["rule"] = "missing_host"
        assessment["reason"] = "URL 缺少主机名"
        return assessment

    allowlist = {
        host.strip().lower()
        for host in (settings.ui_url_allowlist or "").split(",")
        if host.strip()
    }
    if assessment["host"].lower() in allowlist:
        assessment["allowed"] = True
        assessment["allowlist_match"] = True
        assessment["rule"] = "allowlist"
        assessment["reason"] = "主机命中 UI_URL_ALLOWLIST，允许访问"
        return assessment

    if settings.ui_url_allow_private:
        assessment["allowed"] = True
        assessment["rule"] = "allow_private"
        assessment["reason"] = "UI_URL_ALLOW_PRIVATE=true，允许访问受限地址"
        return assessment

    try:
        ips = sorted({info[4][0] for info in socket.getaddrinfo(assessment["host"], None)})
    except Exception:
        assessment["rule"] = "unresolved_host"
        assessment["reason"] = f"主机无法解析，默认拒绝：{assessment['host']}"
        return assessment

    assessment["resolved_ips"] = ips

    for ip in ips:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            assessment["rule"] = "invalid_ip"
            assessment["reason"] = f"非法 IP：{ip}"
            return assessment

        is_private = (
            addr.is_loopback
            or addr.is_private
            or addr.is_link_local
            or addr.is_reserved
            or addr.is_multicast
            or addr.is_unspecified
        )
        if is_private:
            assessment["private_network"] = True
            assessment["restricted_ip"] = ip
            assessment["rule"] = "private_network"
            assessment["reason"] = (
                f"拒绝内网/回环/元数据地址：{assessment['host']}→{ip}"
                "（本地联调请设 UI_URL_ALLOW_PRIVATE=true 或加入 UI_URL_ALLOWLIST）"
            )
            return assessment

    assessment["allowed"] = True
    assessment["rule"] = "public_network"
    assessment["reason"] = "目标地址通过公网安全校验"
    return assessment


def is_safe_url(url: str) -> tuple[bool, str]:
    """SEC-02：校验 Playwright 目标 URL，防 SSRF。

    仅允许 http/https；默认拒绝回环/私网/链路本地（云元数据 169.254.x）/保留地址。
    可经 settings.ui_url_allow_private 放开，或 settings.ui_url_allowlist 精确放行主机。
    返回 (是否安全, 拒绝原因)。
    """
    assessment = inspect_url_security(url)
    return assessment["allowed"], assessment["reason"]


def _install_ssrf_guard(context) -> None:
    """FIX: P0-3 在 Playwright context 上安装逐跳 SSRF 守卫。

    初始 URL 的 inspect_url_security 只覆盖首跳；重定向后的每一跳 URL
    默认不经过校验，攻击者可借 302/JS 跳转到内网地址绕过 SSRF 防护。
    这里拦截 context 内所有网络请求，逐跳重新做安全检查，
    任一跳到私网/回环/链路本地即 abort（fail-closed）。

    残余风险说明：极端 DNS rebinding（TTL 极短、同一域名交替返回
    公网/内网 IP）场景仍可能绕过，生产环境建议叠加网络层防护。
    """
    # 浏览器内部 scheme：不发起网络请求、不构成 SSRF 面。此前经
    # inspect_url_security 的 scheme_not_allowed 一律 abort，会连带打断依赖
    # data:/blob:/about: 子资源（内联图片、JS 生成内容等）的正常页面渲染。
    _BROWSER_INTERNAL_SCHEMES = ("data:", "blob:", "about:")

    def handler(route):
        request_url = route.request.url
        # FIX(v0.7.1-b12-1): 放行浏览器内部 scheme（data/blob/about），它们不产生
        # 网络请求、无法用于 SSRF；其余（http/https/ws/file 等）仍走逐跳校验。
        if request_url.lower().startswith(_BROWSER_INTERNAL_SCHEMES):
            route.continue_()
            return
        assessment = inspect_url_security(request_url)
        if not assessment["allowed"]:
            logger.warning(
                "SSRF guard: 拒绝请求 %s（rule=%s, reason=%s）",
                request_url,
                assessment["rule"],
                assessment["reason"],
            )
            route.abort()
            return
        route.continue_()

    context.route("**/*", handler)


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
        {
          matched, diffs, silent_failure,
          interactions: [{action, matched, diffs, silent_failure, assertions?, failure_evidence?}],
          security: {target, interactions}
        }
    """
    target_url = spec.get("target", "")
    if not target_url:
        return {
            "matched": False,
            "diffs": [{"field": "target", "expected": "valid URL", "actual": target_url}],
            "silent_failure": False,
        }

    target_security = inspect_url_security(target_url)
    security_summary = {"target": target_security, "interactions": []}
    if not target_security["allowed"]:
        return {
            "matched": False,
            "diffs": [{
                "field": "target",
                "expected": "安全的 http(s) URL",
                "actual": target_security["reason"],
            }],
            "silent_failure": False,
            "security": security_summary,
            "failure_evidence": {
                "stage": "security_check",
                "error_type": "URLSafetyError",
                "reason": target_security["reason"],
                "url": target_url,
                "rule": target_security["rule"],
            },
        }

    if not _PLAYWRIGHT_AVAILABLE:
        return {
            "matched": False,
            "diffs": [],
            "silent_failure": False,
            "error": "playwright 未安装。安装方法: pip install playwright && playwright install chromium",
            "security": security_summary,
        }

    interactions = (spec.get("expect") or {}).get("interactions") or []
    global_expect = spec.get("expect") or {}

    all_matched = True
    all_diffs = []
    interaction_results = []

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            # FIX: P0-3 创建 context 并挂载逐跳 SSRF 守卫（拦截重定向/子资源请求）
            # FIX: P2 browser.close() 移入 finally，保证异常/超时路径也关闭浏览器
            try:
                context = browser.new_context()
                _install_ssrf_guard(context)
                page = context.new_page()
                page.set_default_timeout(timeout_ms)

                # 导航到目标页面
                try:
                    page.goto(target_url, wait_until="domcontentloaded")
                except Exception as e:
                    return {
                        "matched": False,
                        "diffs": [{"field": "navigation", "expected": target_url, "actual": str(e)}],
                        "silent_failure": False,
                        "security": security_summary,
                        "failure_evidence": {
                            "stage": "initial_navigation",
                            "error_type": type(e).__name__,
                            "reason": str(e),
                            "url": target_url,
                        },
                    }

                # 验证全局状态
                if _has_verifiable_expectations(global_expect):
                    gd = _verify_state(page, global_expect)
                    if not gd.get("matched"):
                        all_matched = False
                    if gd.get("diffs"):
                        all_diffs.extend(gd["diffs"])
                    interaction_results.append({
                        "action": "navigate",
                        "selector": "",
                        "matched": gd["matched"],
                        "diffs": gd.get("diffs", []),
                        "silent_failure": gd.get("silent_failure", False),
                        "assertions": gd.get("assertions", []),
                        "failure_evidence": gd.get("failure_evidence"),
                    })

                # 遍历交互
                for idx, intn in enumerate(interactions):
                    action = intn.get("action", "click")
                    selector = intn.get("selector", "")
                    value = intn.get("value", "")
                    expect = intn.get("expect") or {}

                    try:
                        ir = _execute_interaction(
                            page, action, selector, value, expect, timeout_ms, step_index=idx
                        )
                    except Exception as e:
                        ir = {
                            "action": action,
                            "selector": selector,
                            "step_index": idx,
                            "matched": False,
                            "diffs": [{"field": f"interactions[{idx}].{action}",
                                        "expected": "success", "actual": str(e)}],
                            "silent_failure": False,
                            "failure_evidence": {
                                "stage": "interaction",
                                "step_index": idx,
                                "selector": selector,
                                "error_type": type(e).__name__,
                                "reason": str(e),
                                "action": action,
                                "url": page.url,
                            },
                        }

                    if not ir.get("matched"):
                        all_matched = False
                    if ir.get("diffs"):
                        all_diffs.extend(ir["diffs"])
                    if "security" in ir:
                        security_summary["interactions"].append(ir["security"])
                    interaction_results.append(ir)
            finally:
                # FIX: P2 browser.close() 移入 finally（含超时/异常路径）
                browser.close()

    except Exception as e:
        logger.exception("Playwright 执行异常")
        return {
            "matched": False,
            "diffs": [{"field": "playwright", "expected": "execution success", "actual": str(e)}],
            "silent_failure": False,
            "interactions": interaction_results,
            "security": security_summary,
            "failure_evidence": {
                "stage": "playwright",
                "error_type": type(e).__name__,
                "reason": str(e),
                "url": target_url,
            },
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
        "security": security_summary,
    }


def _execute_interaction(page, action: str, selector: str, value: str,
                         expect: dict, timeout_ms: int, step_index: int | None = None) -> dict:
    """执行单个 UI 交互并验证。"""
    if not selector:
        return {"action": action, "selector": selector, "step_index": step_index, "matched": False,
                "diffs": [{"field": "selector", "expected": "CSS selector", "actual": ""}],
                "silent_failure": False,
                "failure_evidence": {
                    "stage": "validate_input",
                    "step_index": step_index,
                    "selector": selector,
                    "error_type": "MissingSelector",
                    "reason": "交互缺少 selector",
                    "action": action,
                    "url": page.url,
                }}

    # 检查元素是否存在
    try:
        el = page.wait_for_selector(selector, timeout=timeout_ms)
    except PlaywrightTimeout:
        return {
            "action": action,
            "selector": selector,
            "step_index": step_index,
            "matched": False,
            "diffs": [{"field": f"{action}({selector})",
                        "expected": "element found", "actual": "element not found"}],
            "silent_failure": True,  # 元素不存在 = 静默失败
            "failure_evidence": {
                "stage": "locate",
                "step_index": step_index,
                "selector": selector,
                "error_type": "SelectorNotFound",
                "reason": "element not found",
                "action": action,
                "url": page.url,
            },
        }

    # 执行动作
    prev_url = page.url
    security_result = None
    try:
        if action == "click":
            el.click()
        elif action == "type":
            el.fill(value)
        elif action == "navigate":
            security_result = inspect_url_security(value)
            if not security_result["allowed"]:
                return {"action": action, "selector": selector, "step_index": step_index, "matched": False,
                        "diffs": [{"field": f"navigate({value})",
                                    "expected": "安全的 http(s) URL", "actual": security_result["reason"]}],
                        "silent_failure": False,
                        "security": security_result,
                        "failure_evidence": {
                            "stage": "security_check",
                            "step_index": step_index,
                            "selector": selector,
                            "error_type": "URLSafetyError",
                            "reason": security_result["reason"],
                            "action": action,
                            "url": value,
                            "rule": security_result["rule"],
                        }}
            page.goto(value, wait_until="domcontentloaded")
        elif action == "hover":
            el.hover()
        elif action == "select":
            page.select_option(selector, value)
        else:
            return {"action": action, "selector": selector, "step_index": step_index, "matched": False,
                    "diffs": [{"field": action, "expected": "valid action", "actual": action}],
                    "silent_failure": False,
                    "failure_evidence": {
                        "stage": "validate_input",
                        "step_index": step_index,
                        "selector": selector,
                        "error_type": "UnsupportedAction",
                        "reason": f"unsupported action: {action}",
                        "action": action,
                        "url": page.url,
                    }}
    except Exception as e:
        return {
            "action": action,
            "selector": selector,
            "step_index": step_index,
            "matched": False,
            "diffs": [{"field": f"{action}({selector})",
                        "expected": "interaction success", "actual": str(e)}],
            "silent_failure": False,
            "failure_evidence": {
                "stage": "action",
                "step_index": step_index,
                "selector": selector,
                "error_type": type(e).__name__,
                "reason": str(e),
                "action": action,
                "url": page.url,
            },
        }

    # 验证结果
    result = _verify_state(page, expect, action=f"{action}({selector})", prev_url=prev_url)
    result["action"] = action
    result["selector"] = selector
    result["step_index"] = step_index
    if security_result is not None:
        result["security"] = security_result
    return result


def _verify_state(page, expect: dict, action: str = "", prev_url: str = "") -> dict:
    """验证页面状态是否符合期望。"""
    diffs = []
    assertions = []
    state_change = expect.get("state_change") or {}

    # route_change
    if "route_change" in state_change:
        exp_route = state_change["route_change"]
        actual_url = page.url
        matched = True
        try:
            page.wait_for_url(exp_route, timeout=5000)
        except Exception:
            actual_url = page.url
            matched = False
            if prev_url and actual_url == prev_url:
                diffs.append({
                    "field": f"{action}.route_change",
                    "expected": exp_route,
                    "actual": actual_url,
                })
            else:
                diffs.append({
                    "field": f"{action}.route_change",
                    "expected": exp_route,
                    "actual": actual_url,
                })
        assertions.append({
            "type": "route_change",
            "matched": matched,
            "expected": exp_route,
            "actual": page.url,
        })

    # dom_change
    if "dom_change" in state_change:
        dom_sel = state_change["dom_change"]
        matched = True
        try:
            page.wait_for_selector(dom_sel, timeout=5000)
        except Exception:
            matched = False
            diffs.append({
                "field": f"{action}.dom_change",
                "expected": f"selector '{dom_sel}' appears",
                "actual": f"selector '{dom_sel}' not found",
            })
        assertions.append({
            "type": "dom_change",
            "matched": matched,
            "expected": dom_sel,
            "actual": dom_sel if matched else None,
            "selector": dom_sel,
        })

    for assertion in _normalize_business_assertions(expect):
        result = _evaluate_business_assertion(page, assertion, action)
        assertions.append(result)
        if not result["matched"]:
            diffs.append(result["diff"])

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
        "assertions": assertions,
        "failure_evidence": _build_failure_evidence(action, assertions, page.url) if not matched else None,
    }


def _normalize_business_assertions(expect: dict) -> list[dict]:
    """兼容 expect.assertions 与 expect.text/url 简写。"""
    items = []
    raw_assertions = expect.get("assertions") or []
    if isinstance(raw_assertions, dict):
        raw_assertions = [raw_assertions]
    items.extend([item for item in raw_assertions if isinstance(item, dict)])

    text_assert = expect.get("text")
    if isinstance(text_assert, dict):
        items.append({"type": "text", **text_assert})

    url_assert = expect.get("url")
    if isinstance(url_assert, dict):
        items.append({"type": "url", **url_assert})
    elif isinstance(url_assert, str):
        items.append({"type": "url", "equals": url_assert})

    return items


def _has_verifiable_expectations(expect: dict) -> bool:
    """判断 expect 是否包含可直接在当前页面校验的断言。"""
    if not expect:
        return False
    if expect.get("state_change"):
        return True
    if expect.get("no_response"):
        return True
    return bool(_normalize_business_assertions(expect))


def _evaluate_business_assertion(page, assertion: dict, action: str) -> dict:
    """执行文本或 URL 等业务级断言。"""
    assertion_type = assertion.get("type")

    if assertion_type == "text":
        selector = assertion.get("selector", "")
        expected = assertion.get("equals")
        contains = assertion.get("contains")
        if not selector or (expected is None and contains is None):
            reason = "text 断言需要 selector 且至少提供 equals/contains 之一"
            return {
                "type": "text",
                "matched": False,
                "expected": assertion,
                "actual": None,
                "selector": selector,
                "diff": {
                    "field": f"{action}.text",
                    "expected": assertion,
                    "actual": reason,
                },
                "error_type": "InvalidAssertion",
            }

        try:
            actual = (page.text_content(selector, timeout=5000) or "").strip()
        except Exception as e:
            return {
                "type": "text",
                "matched": False,
                "expected": expected if expected is not None else {"contains": contains},
                "actual": None,
                "selector": selector,
                "diff": {
                    "field": f"{action}.text",
                    "expected": expected if expected is not None else {"contains": contains},
                    "actual": str(e),
                },
                "error_type": type(e).__name__,
            }

        if expected is not None:
            matched = actual == expected
            expected_value = expected
        else:
            matched = contains in actual
            expected_value = {"contains": contains}

        return {
            "type": "text",
            "matched": matched,
            "expected": expected_value,
            "actual": actual,
            "selector": selector,
            "diff": None if matched else {
                "field": f"{action}.text",
                "expected": expected_value,
                "actual": actual,
            },
        }

    if assertion_type == "url":
        expected = assertion.get("equals")
        contains = assertion.get("contains")
        if expected is None and contains is None:
            reason = "url 断言至少需要 equals 或 contains"
            return {
                "type": "url",
                "matched": False,
                "expected": assertion,
                "actual": page.url,
                "diff": {
                    "field": f"{action}.url",
                    "expected": assertion,
                    "actual": reason,
                },
                "error_type": "InvalidAssertion",
            }

        actual_url = page.url
        try:
            if expected is not None:
                page.wait_for_function(
                    "(exp) => window.location.href === exp",
                    expected,
                    timeout=5000,
                )
            else:
                page.wait_for_function(
                    "(needle) => window.location.href.includes(needle)",
                    contains,
                    timeout=5000,
                )
            actual_url = page.url
        except Exception:
            actual_url = page.url

        matched = actual_url == expected if expected is not None else contains in actual_url
        expected_value = expected if expected is not None else {"contains": contains}
        return {
            "type": "url",
            "matched": matched,
            "expected": expected_value,
            "actual": actual_url,
            "diff": None if matched else {
                "field": f"{action}.url",
                "expected": expected_value,
                "actual": actual_url,
            },
        }

    if assertion_type == "form":
        selector = assertion.get("selector", "")
        expected_values = assertion.get("values", {})
        if not selector or not expected_values:
            reason = "form 断言需要 selector 和 values"
            return {
                "type": "form",
                "matched": False,
                "expected": assertion,
                "actual": None,
                "selector": selector,
                "diff": {
                    "field": f"{action}.form",
                    "expected": assertion,
                    "actual": reason,
                },
                "error_type": "InvalidAssertion",
            }

        try:
            actual_values = {}
            form_element = page.query_selector(selector)
            if form_element:
                # 获取表单中各个输入字段的值
                inputs = form_element.query_selector_all("input, textarea, select")
                for inp in inputs:
                    # 尝试多种方式获取字段标识
                    name = (inp.get_attribute("name") or
                           inp.get_attribute("id") or
                           inp.get_attribute("data-testid") or
                           inp.get_attribute("placeholder"))
                    if name:
                        # 使用 evaluate 获取标签名
                        try:
                            tag_name = inp.evaluate("el => el.tagName.toLowerCase()")
                        except Exception:
                            tag_name = "input"  # 默认值

                        # 根据元素类型获取值
                        if inp.get_attribute("type") in ["checkbox", "radio"]:
                            actual_values[name] = inp.is_checked()
                        elif tag_name in ['input', 'textarea']:
                            actual_values[name] = inp.input_value()
                        elif tag_name == 'select':
                            actual_values[name] = inp.input_value()  # 获取选中的值
                        else:
                            actual_values[name] = inp.text_content()
            else:
                # 如果找不到表单元素，返回错误
                return {
                    "type": "form",
                    "matched": False,
                    "expected": expected_values,
                    "actual": None,
                    "selector": selector,
                    "diff": {
                        "field": f"{action}.form",
                        "expected": expected_values,
                        "actual": f"Form element not found with selector: {selector}",
                    },
                    "error_type": "ElementNotFound",
                }

            matched = True
            diff_details = []
            for field, expected_val in expected_values.items():
                actual_val = actual_values.get(field)
                if actual_val != expected_val:
                    matched = False
                    diff_details.append({
                        "field": field,
                        "expected": expected_val,
                        "actual": actual_val
                    })

            return {
                "type": "form",
                "matched": matched,
                "expected": expected_values,
                "actual": actual_values,
                "selector": selector,
                "diff": None if matched else {
                    "field": f"{action}.form",
                    "expected": expected_values,
                    "actual": actual_values,
                    "details": diff_details
                },
            }
        except Exception as e:
            return {
                "type": "form",
                "matched": False,
                "expected": expected_values,
                "actual": None,
                "selector": selector,
                "diff": {
                    "field": f"{action}.form",
                    "expected": expected_values,
                    "actual": str(e),
                },
                "error_type": type(e).__name__,
            }

    if assertion_type == "data_table":
        selector = assertion.get("selector", "")
        expected_rows = assertion.get("rows", 0)
        expected_columns = assertion.get("columns", 0)
        expected_headers = assertion.get("headers", [])

        if not selector:
            reason = "data_table 断言需要 selector"
            return {
                "type": "data_table",
                "matched": False,
                "expected": assertion,
                "actual": None,
                "selector": selector,
                "diff": {
                    "field": f"{action}.data_table",
                    "expected": assertion,
                    "actual": reason,
                },
                "error_type": "InvalidAssertion",
            }

        try:
            table_element = page.query_selector(selector)
            if not table_element:
                return {
                    "type": "data_table",
                    "matched": False,
                    "expected": assertion,
                    "actual": None,
                    "selector": selector,
                    "diff": {
                        "field": f"{action}.data_table",
                        # FIX: R3-6 expected_values 仅在 form 分支定义，
                        # 此处引用会 NameError 被外层 except 吞掉，返回误导性 error_type
                        "expected": {
                            "rows": expected_rows,
                            "columns": expected_columns,
                            "headers": expected_headers,
                        },
                        "actual": "Table element not found",
                    },
                    "error_type": "ElementNotFound",
                }

            # 计算表格行数和列数
            rows = table_element.query_selector_all("tr")
            actual_rows = len(rows) - 1  # 减去表头
            actual_headers = []

            # 获取表头
            header_cells = table_element.query_selector_all("th")
            for cell in header_cells:
                actual_headers.append(cell.text_content().strip())

            # 如果没有找到th，则尝试从第一行td获取
            if not actual_headers:
                first_row = table_element.query_selector("tr")
                if first_row:
                    header_cells = first_row.query_selector_all("td")
                    for cell in header_cells:
                        actual_headers.append(cell.text_content().strip())

            actual_columns = len(actual_headers)

            # 检查行数、列数和表头
            matched = True
            diff_details = []

            if expected_rows > 0 and actual_rows != expected_rows:
                matched = False
                diff_details.append({
                    "field": "rows",
                    "expected": expected_rows,
                    "actual": actual_rows
                })

            if expected_columns > 0 and actual_columns != expected_columns:
                matched = False
                diff_details.append({
                    "field": "columns",
                    "expected": expected_columns,
                    "actual": actual_columns
                })

            if expected_headers and actual_headers != expected_headers:
                matched = False
                diff_details.append({
                    "field": "headers",
                    "expected": expected_headers,
                    "actual": actual_headers
                })

            return {
                "type": "data_table",
                "matched": matched,
                "expected": {"rows": expected_rows, "columns": expected_columns, "headers": expected_headers},
                "actual": {"rows": actual_rows, "columns": actual_columns, "headers": actual_headers},
                "selector": selector,
                "diff": None if matched else {
                    "field": f"{action}.data_table",
                    "expected": {"rows": expected_rows, "columns": expected_columns, "headers": expected_headers},
                    "actual": {"rows": actual_rows, "columns": actual_columns, "headers": actual_headers},
                    "details": diff_details
                },
            }
        except Exception as e:
            return {
                "type": "data_table",
                "matched": False,
                "expected": {"rows": expected_rows, "columns": expected_columns, "headers": expected_headers},
                "actual": None,
                "selector": selector,
                "diff": {
                    "field": f"{action}.data_table",
                    "expected": {"rows": expected_rows, "columns": expected_columns, "headers": expected_headers},
                    "actual": str(e),
                },
                "error_type": type(e).__name__,
            }

    if assertion_type == "numeric_range":
        selector = assertion.get("selector", "")
        min_value = assertion.get("min")
        max_value = assertion.get("max")

        if not selector or (min_value is None and max_value is None):
            reason = "numeric_range 断言需要 selector 和 min/max 至少一个值"
            return {
                "type": "numeric_range",
                "matched": False,
                "expected": assertion,
                "actual": None,
                "selector": selector,
                "diff": {
                    "field": f"{action}.numeric_range",
                    "expected": assertion,
                    "actual": reason,
                },
                "error_type": "InvalidAssertion",
            }

        try:
            text_content = (page.text_content(selector, timeout=5000) or "").strip()
            # 尝试提取数字
            import re
            numbers = re.findall(r'-?\d+\.?\d*', text_content)
            if not numbers:
                return {
                    "type": "numeric_range",
                    "matched": False,
                    "expected": {"selector": selector, "min": min_value, "max": max_value},
                    "actual": text_content,
                    "selector": selector,
                    "diff": {
                        "field": f"{action}.numeric_range",
                        "expected": {"min": min_value, "max": max_value},
                        "actual": f"No numeric value found in '{text_content}'",
                    },
                    "error_type": "NoNumericValue",
                }

            actual_number = float(numbers[0])  # 取第一个找到的数字

            matched = True
            if min_value is not None and actual_number < min_value:
                matched = False
            if max_value is not None and actual_number > max_value:
                matched = False

            return {
                "type": "numeric_range",
                "matched": matched,
                "expected": {"selector": selector, "min": min_value, "max": max_value},
                "actual": actual_number,
                "selector": selector,
                "diff": None if matched else {
                    "field": f"{action}.numeric_range",
                    "expected": {"min": min_value, "max": max_value},
                    "actual": actual_number,
                },
            }
        except Exception as e:
            return {
                "type": "numeric_range",
                "matched": False,
                "expected": {"selector": selector, "min": min_value, "max": max_value},
                "actual": None,
                "selector": selector,
                "diff": {
                    "field": f"{action}.numeric_range",
                    "expected": {"min": min_value, "max": max_value},
                    "actual": str(e),
                },
                "error_type": type(e).__name__,
            }

    return {
        "type": assertion_type or "unknown",
        "matched": False,
        "expected": assertion,
        "actual": None,
        "diff": {
            "field": f"{action}.assertion",
            "expected": "supported assertion type",
            "actual": assertion_type,
        },
        "error_type": "UnsupportedAssertion",
    }


def _build_failure_evidence(action: str, assertions: list[dict], current_url: str) -> dict:
    """从失败断言中提取结构化留证信息。"""
    failed = [item for item in assertions if not item.get("matched")]
    if not failed:
        return None

    first = failed[0]
    evidence = {
        "stage": "assertion",
        "action": action or "verify",
        "url": current_url,
        "failed_assertions": [
            {
                "type": item.get("type"),
                "selector": item.get("selector"),
                "expected": item.get("expected"),
                "actual": item.get("actual"),
                "error_type": item.get("error_type"),
            }
            for item in failed
        ],
    }
    if first.get("selector"):
        evidence["selector"] = first["selector"]
    return evidence
