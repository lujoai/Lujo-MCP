"""
MCP 工具：verify_ui —— 按 UI 规范启动 Playwright 自动遍历并验证交互结果。

输入 spec 规范的 kind 必须为 "ui"，含 target(页面 URL) + interactions 列表。
输出沿用 {matched, diffs, silent_failure, interactions}，并补充：
- business assertions（text/url）结果
- failure_evidence 结构化失败留证
- security 字段表达 allowlist / 私网限制校验结果

可选依赖：playwright（未安装时返回错误提示，不影响其他功能）。
"""
from app.runtime.verifier import ui_runner

VERIFY_UI_DEF = {
    "name": "verify_ui",
    "description": (
        "按 UI 规范启动浏览器自动遍历页面交互并验证结果。"
        "spec.kind 须为 'ui'，含 target(页面URL) 和 expect.interactions 列表。"
        "交互类型: click / type / navigate / hover / select。"
        "expect 可继续使用 state_change.dom_change/route_change，"
        "也支持 expect.assertions[] 中的 text/url/form/data_table/numeric_range 断言。"
        "新增业务断言类型："
        "form - 验证表单字段值 {type:'form', selector:'#form-id', values:{field1:'value1'}}，"
        "data_table - 验证数据表格 {type:'data_table', selector:'#table-id', rows:5, columns:3, headers:['Name','Age']}，"
        "numeric_range - 验证数值范围 {type:'numeric_range', selector:'#value', min:0, max:100}。"
        "返回 {matched, diffs, silent_failure, interactions[], security?}，"
        "失败交互会附带 assertions 与 failure_evidence。"
        "需要 Playwright（pip install playwright && playwright install chromium）。"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "spec": {
                "type": "object",
                "description": "UI 规范 {kind:'ui', target, expect}",
            },
            "spec_id": {
                "type": "string",
                "description": "已存储规范的 ID（与 spec 二选一）",
            },
            "timeout_ms": {
                "type": "integer",
                "description": "单个操作超时毫秒（默认 30000）",
            },
        },
        "required": [],
    },
}


def verify_ui_handler(arguments: dict) -> dict:
    """verify_ui 工具处理函数。"""
    spec = arguments.get("spec")
    spec_id = arguments.get("spec_id")
    timeout_ms = arguments.get("timeout_ms", 30000)

    # spec 优先；spec_id 从存储取
    if spec is None and spec_id:
        from app.runtime.verifier import spec_store
        spec = spec_store.get(spec_id)

    if spec is None:
        return {
            "matched": False,
            "diffs": [],
            "silent_failure": False,
            "error": "must provide spec or spec_id",
        }

    if spec.get("kind") != "ui":
        return {
            "matched": False,
            "diffs": [{"field": "kind", "expected": "ui", "actual": spec.get("kind")}],
            "silent_failure": False,
            "error": "spec.kind must be 'ui'",
        }

    return ui_runner.run_ui_verification(spec, timeout_ms=timeout_ms)
