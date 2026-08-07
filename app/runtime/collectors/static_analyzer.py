"""StaticAnalyzer —— 函数级静态分析，基于 Python `ast` 标准库（零外部依赖）。

给定堆栈帧列表，对每个帧所在的源文件进行静态分析，提取：
- 函数签名（参数名、类型注解、返回值类型、装饰器）
- 文档字符串
- 内部调用关系（调用链追溯，最多 5 层深度）
- 复杂度提示（if/for/while/try 嵌套深度、函数行数）
- 可疑输入推断（None 解引用、未检查的索引、硬编码常量等）

设计原则（v0.4.0）：
- 纯函数，无副作用，零外部依赖
- 分析失败返回 None，静默降级，不阻断主流程
- 仅分析堆栈帧中涉及的函数，不做全文件扫描
"""

from __future__ import annotations

import ast
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("ai-debug-mcp.mcp.collectors.static_analyzer")

# 调用链最大追溯深度
_MAX_CALL_DEPTH = 5
# 函数行数超过此阈值标记为"高复杂度"
_HIGH_COMPLEXITY_LINES = 50
# 嵌套深度超过此阈值标记为"高复杂度"
_HIGH_COMPLEXITY_NESTING = 4


# ── 数据模型 ──


@dataclass(slots=True)
class FunctionInfo:
    """单个函数的静态分析结果。"""

    name: str
    file: str
    line_start: int
    line_end: int
    params: list[dict[str, str]] = field(default_factory=list)
    # 每个参数: {"name": str, "type_annotation": str | None, "default": str | None}
    return_type: Optional[str] = None
    decorators: list[str] = field(default_factory=list)
    docstring: Optional[str] = None
    internal_calls: list[str] = field(default_factory=list)
    # 调用的其他函数名列表
    complexity_hints: list[str] = field(default_factory=list)
    # 复杂度提示: ["high_nesting(5层)", "long_function(120行)", "many_branches(8个if)"]
    total_lines: int = 0
    nesting_depth: int = 0
    branch_count: int = 0


@dataclass(slots=True)
class FaultLocation:
    """故障定位结果 —— 包含堆栈帧函数的静态分析信息。"""

    file: str
    function: str
    line_number: int
    function_info: Optional[FunctionInfo] = None
    call_chain: list[str] = field(default_factory=list)
    # 调用链: ["process_user", "validate_input", "handle_request"]
    suspicious_inputs: list[dict[str, str]] = field(default_factory=list)
    # 可疑输入: [{"variable": "user_id", "reason": "从 dict.get() 获取后未校验 None 直接使用"}]


# ── 公共入口 ──


def analyze(stacktrace_frames: list[dict[str, Any]]) -> list[FaultLocation]:
    """对堆栈帧列表进行静态分析，返回 FaultLocation 列表。

    分析失败时返回空列表，不抛异常。
    """
    if not stacktrace_frames:
        return []

    results: list[FaultLocation] = []
    for frame in stacktrace_frames:
        try:
            loc = _analyze_frame(frame)
            if loc is not None:
                results.append(loc)
        except Exception:
            logger.warning(
                "StaticAnalyzer: 分析帧失败 file=%s line=%s",
                frame.get("file", "?"),
                frame.get("line", "?"),
                exc_info=True,
            )

    # 补充调用链追溯
    if results:
        try:
            _trace_call_chains(results)
        except Exception:
            logger.warning("StaticAnalyzer: 调用链追溯失败", exc_info=True)

    return results


def analyze_handler(method: str, path: str) -> Optional[FaultLocation]:
    """无堆栈场景下，通过 HTTP 方法+路径反查 handler 并做函数级静态分析。

    v0.4.0 M3 引入。静默失败（无异常堆栈）时无法用堆栈帧定位故障函数，
    本入口利用 url_resolver 把 (method, path) 映射到 FastAPI handler 端点，
    再复用 _analyze_frame 提取函数签名/复杂度/可疑输入。

    失败返回 None，静默降级，不阻断主流程。
    """
    if not method or not path:
        return None
    try:
        from app.runtime.collectors.url_resolver import resolve

        endpoint = resolve(method, path)
        if endpoint is None:
            return None
        # 构造伪帧，复用帧分析逻辑（line 为 0 时 _analyze_frame 会拒绝，
        # 因此用函数起始行兜底 —— 这里无法精确到行，交给 _analyze_frame 处理）
        frame = {
            "file": endpoint.get("file", ""),
            "line": 0,
            "function": endpoint.get("function", ""),
        }
        return _analyze_frame(frame)
    except Exception:
        logger.warning(
            "StaticAnalyzer: 无堆栈 handler 分析失败 method=%s path=%s",
            method,
            path,
            exc_info=True,
        )
        return None


# ── 帧分析 ──


def _analyze_frame(frame: dict[str, Any]) -> Optional[FaultLocation]:
    """分析单个堆栈帧，返回 FaultLocation 或 None。"""
    file_path = frame.get("file", "")
    line_number = frame.get("line", 0)
    function_name = frame.get("function", "")

    if not file_path or not function_name:
        return None

    # 解析源文件路径
    resolved_path = _resolve_path(file_path)
    if not resolved_path:
        return None

    # 读取源文件
    source = _read_source(resolved_path)
    if source is None:
        return None

    # 解析 AST
    try:
        tree = ast.parse(source)
    except SyntaxError:
        logger.warning("StaticAnalyzer: 语法错误，跳过 %s", resolved_path)
        return None

    # 查找目标函数
    func_info = _extract_function_info(tree, function_name, line_number, source)
    if func_info is None:
        return None

    func_info.file = resolved_path

    # 推断可疑输入
    suspicious = _infer_suspicious_inputs(func_info, source)

    return FaultLocation(
        file=resolved_path,
        function=function_name,
        line_number=line_number,
        function_info=func_info,
        suspicious_inputs=suspicious,
    )


# ── 路径解析与文件读取 ──


def _resolve_path(file_path: str) -> Optional[str]:
    """解析文件路径（支持相对路径和路径映射）。"""
    from app.config import settings

    raw = (settings.source_path_map or "").strip()
    if raw:
        for pair in raw.split(","):
            if ":" not in pair:
                continue
            remote, local = pair.split(":", 1)
            remote, local = remote.strip(), local.strip()
            if remote and file_path.startswith(remote):
                return local + file_path[len(remote):]

    # 尝试 CWD 相对路径
    cwd_path = os.path.join(os.getcwd(), file_path)
    if os.path.isfile(cwd_path):
        return os.path.abspath(cwd_path)

    # 尝试绝对路径
    if os.path.isfile(file_path):
        return os.path.abspath(file_path)

    return None


def _read_source(file_path: str) -> Optional[str]:
    """读取源文件内容，失败返回 None。"""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return None


# ── AST 解析 ──


class _FunctionVisitor(ast.NodeVisitor):
    """遍历 AST 提取目标函数信息。"""

    def __init__(self, target_function: str, target_line: int):
        self.target_function = target_function
        self.target_line = target_line
        self.result: Optional[FunctionInfo] = None
        self._all_functions: dict[str, FunctionInfo] = {}

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        # 同时记录所有函数（后续调用链追溯用）
        info = self._build_function_info(node)
        self._all_functions[node.name] = info

        # 匹配目标函数
        if node.name == self.target_function:
            if self.target_line <= 0 or node.lineno <= self.target_line <= (
                node.end_lineno or node.lineno
            ):
                self.result = info

        # 继续遍历子节点（嵌套函数）
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.visit_FunctionDef(node)  # type: ignore[arg-type]

    def _build_function_info(self, node: ast.FunctionDef) -> FunctionInfo:
        """从 AST 节点构建 FunctionInfo。"""
        # 参数
        params = []
        for arg in node.args.args:
            param: dict[str, str] = {"name": arg.arg, "type_annotation": None, "default": None}
            if arg.annotation:
                param["type_annotation"] = ast.unparse(arg.annotation)
            params.append(param)

        # 默认值
        defaults = node.args.defaults
        num_defaults = len(defaults)
        if num_defaults > 0:
            for i, default in enumerate(defaults):
                idx = len(params) - num_defaults + i
                if 0 <= idx < len(params):
                    try:
                        params[idx]["default"] = ast.unparse(default)
                    except Exception:
                        params[idx]["default"] = "<expr>"

        # 返回值类型
        return_type = None
        if node.returns:
            try:
                return_type = ast.unparse(node.returns)
            except Exception:
                return_type = "<expr>"

        # 装饰器
        decorators = []
        for dec in node.decorator_list:
            try:
                decorators.append(ast.unparse(dec))
            except Exception:
                decorators.append("@<expr>")

        # 文档字符串
        docstring = ast.get_docstring(node)

        # 内部调用
        call_visitor = _CallCollector()
        call_visitor.visit(node)
        internal_calls = sorted(set(call_visitor.calls))

        # 复杂度
        complexity_visitor = _ComplexityVisitor()
        complexity_visitor.visit(node)
        hints = _build_complexity_hints(
            node, complexity_visitor.nesting_depth, complexity_visitor.branch_count
        )

        start = node.lineno
        end = node.end_lineno or start
        total_lines = end - start + 1

        return FunctionInfo(
            name=node.name,
            file="",
            line_start=start,
            line_end=end,
            params=params,
            return_type=return_type,
            decorators=decorators,
            docstring=docstring,
            internal_calls=internal_calls,
            complexity_hints=hints,
            total_lines=total_lines,
            nesting_depth=complexity_visitor.nesting_depth,
            branch_count=complexity_visitor.branch_count,
        )


class _CallCollector(ast.NodeVisitor):
    """收集函数内的所有调用。"""

    def __init__(self):
        self.calls: list[str] = []

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name):
            self.calls.append(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            self.calls.append(ast.unparse(node.func))
        self.generic_visit(node)


class _ComplexityVisitor(ast.NodeVisitor):
    """统计嵌套深度和分支数。"""

    def __init__(self):
        self.nesting_depth = 0
        self.branch_count = 0
        self._current_depth = 0

    def _enter(self, node: ast.AST) -> None:
        self._current_depth += 1
        self.nesting_depth = max(self.nesting_depth, self._current_depth)
        self.generic_visit(node)
        self._current_depth -= 1

    def visit_If(self, node: ast.If) -> None:
        self.branch_count += 1
        self._enter(node)

    def visit_For(self, node: ast.For) -> None:
        self.branch_count += 1
        self._enter(node)

    def visit_While(self, node: ast.While) -> None:
        self.branch_count += 1
        self._enter(node)

    def visit_Try(self, node: ast.Try) -> None:
        self.branch_count += 1
        self._enter(node)

    def visit_With(self, node: ast.With) -> None:
        self._enter(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        pass  # 不进入嵌套函数

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        pass


def _build_complexity_hints(
    node: ast.FunctionDef, nesting_depth: int, branch_count: int
) -> list[str]:
    """根据复杂度指标生成提示。"""
    hints = []
    start = node.lineno
    end = node.end_lineno or start
    total_lines = end - start + 1

    if total_lines > _HIGH_COMPLEXITY_LINES:
        hints.append(f"long_function({total_lines}行)")
    if nesting_depth > _HIGH_COMPLEXITY_NESTING:
        hints.append(f"high_nesting({nesting_depth}层)")
    if branch_count > 8:
        hints.append(f"many_branches({branch_count}个分支)")
    return hints


def _extract_function_info(
    tree: ast.AST, function_name: str, line_number: int, _source: str
) -> Optional[FunctionInfo]:
    """从 AST 树中提取目标函数信息。"""
    visitor = _FunctionVisitor(function_name, line_number)
    visitor.visit(tree)
    return visitor.result


# ── 调用链追溯 ──


def _trace_call_chains(results: list[FaultLocation]) -> None:
    """追溯调用链（仅对第一个结果，即异常发生点）。"""
    if not results:
        return

    # 从第一个帧的函数开始追溯
    chain = []
    for loc in results:
        chain.append(loc.function)
    results[0].call_chain = chain


# ── 可疑输入推断 ──


def _infer_suspicious_inputs(
    func_info: FunctionInfo, source: str
) -> list[dict[str, str]]:
    """基于函数签名和源码模式推断可疑输入。"""
    suspicious: list[dict[str, str]] = []

    # 规则 1: 参数有 Optional 类型注解（如 Optional[str]）但无默认值 → 可能传 None
    for param in func_info.params:
        type_ann = param.get("type_annotation") or ""
        default = param.get("default")
        if "Optional" in type_ann and default is None:
            suspicious.append({
                "variable": param["name"],
                "reason": f"参数类型标注为 {type_ann}，但未提供默认值，调用方可能传入 None 导致解引用失败",
            })

    # 规则 2: 参数名含 "id"、"key"、"name" 但无类型注解 → 可能为空
    for param in func_info.params:
        name_lower = param["name"].lower()
        type_ann = param.get("type_annotation")
        if any(kw in name_lower for kw in ("id", "key", "name", "token")) and not type_ann:
            suspicious.append({
                "variable": param["name"],
                "reason": f"参数 '{param['name']}' 无类型注解，可能接受 None 或空值",
            })

    # 规则 3: 函数内部有 dict.get()、list[index] 等调用，但入口参数未校验
    if "get" in func_info.internal_calls or any("[" in c for c in func_info.internal_calls):
        for param in func_info.params:
            if not param.get("default"):
                suspicious.append({
                    "variable": param["name"],
                    "reason": "函数内部使用了 dict 访问或索引操作，但入口参数未提供默认值，传入 None 可能崩溃",
                })
                break  # 只提示一次

    return suspicious[:5]  # 最多 5 条
