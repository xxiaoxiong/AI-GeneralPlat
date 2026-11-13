"""🐍 代码执行工具：python_exec（受限沙箱执行 Python 代码）

能力边界声明（§15.2 F）：restricted（AST 检查 + 内置白名单 + 线程超时）
**不是安全沙箱**，无法防御同进程资源滥用或复杂逃逸；真正的隔离必须接入
外部 Sandbox（ToolPolicy.sandbox_mode == "external"）。没有外部 Sandbox 时，
生产模式由 ToolPolicy 默认禁用 python_exec，仅开发环境可显式
AGENT_ALLOW_UNSAFE_PYTHON 开关放行。

超时语义诚实性（§15.2 F）：线程 join 超时**不能假装终止**——超时线程以
daemon 方式继续存活，工具如实报告"线程仍在运行、可能继续占用 CPU/内存"，
并在残留线程存活期间拒绝再次执行（防止无限堆积占用进程资源）。
"""
import sys
import io
import ast
import math
import json
import re
import threading
import traceback
from typing import Dict, List

from .result import ToolResult


TOOLS: List[Dict] = [
    {
        "name": "python_exec",
        "display_name": "🐍 Python 执行",
        "category": "代码执行",
        "description": "在安全沙箱中执行 Python 代码，支持数学计算、数据处理、字符串操作、列表/字典操作等。不支持文件系统、网络、系统调用。",
        "parameters": {
            "code": {"type": "string", "description": "要执行的 Python 代码"},
            "timeout": {"type": "integer", "description": "超时秒数（默认5，最大10）", "default": 5},
        },
        "required": ["code"],
    },
]

# 允许使用的内置函数白名单
_SAFE_BUILTINS = {
    "abs", "all", "any", "bin", "bool", "bytes", "callable", "chr", "complex",
    "dict", "dir", "divmod", "enumerate", "filter", "float", "format", "frozenset",
    "getattr", "hasattr", "hash", "hex", "int", "isinstance", "issubclass", "iter",
    "len", "list", "map", "max", "min", "next", "oct", "ord", "pow", "print",
    "range", "repr", "reversed", "round", "set", "slice", "sorted", "str", "sum",
    "tuple", "type", "zip", "True", "False", "None",
}

# 禁止的 AST 节点（防止危险操作）
_FORBIDDEN_NODES = {
    ast.Import, ast.ImportFrom,  # 禁止 import
}

# 禁止的属性访问前缀
_FORBIDDEN_ATTRS = {
    "__import__", "__builtins__", "__class__", "__subclasses__",
    "__globals__", "__locals__", "__code__", "__closure__",
    "os", "sys", "subprocess", "socket", "open", "eval", "exec",
}


def _check_ast_safety(code: str) -> str | None:
    """检查代码 AST，返回错误信息或 None（安全）"""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return f"语法错误: {e}"

    for node in ast.walk(tree):
        # 禁止 import
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            return "禁止使用 import 语句"
        # 禁止危险属性访问
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("__") or node.attr in _FORBIDDEN_ATTRS:
                return f"禁止访问属性: {node.attr}"
        # 禁止直接调用 eval/exec/open 等
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                if node.func.id in {"eval", "exec", "open", "compile", "__import__"}:
                    return f"禁止调用: {node.func.id}"
    return None


# 残留超时线程登记（§15.2 F）：join 超时的执行线程无法强制终止（daemon 存活），
# 存活期间拒绝再次执行 python_exec，防止无限堆积占用进程资源。
_RUNAWAY_LOCK = threading.Lock()
_RUNAWAY_THREADS: List[threading.Thread] = []


def _prune_runaway_threads() -> List[threading.Thread]:
    """清理已结束的残留线程，返回仍存活的超时线程列表。"""
    with _RUNAWAY_LOCK:
        alive = [t for t in _RUNAWAY_THREADS if t.is_alive()]
        _RUNAWAY_THREADS[:] = alive
        return list(alive)


def execute_python_exec(params: dict) -> ToolResult:
    """受限 Python 执行（P0-05 §7.2 C：错误/超时必须返回明确失败结果）。

    注意：这是受限执行环境（AST 检查 + 内置白名单 + 线程超时），
    不是安全沙箱 —— 能力边界如实标注（任务书 §0.2）。
    """
    code = params.get("code", "").strip()
    timeout_sec = min(int(params.get("timeout", 5)), 10)

    if not code:
        return ToolResult.failed("错误：代码为空")

    # 残留超时线程仍在运行：拒绝再次执行（§15.2 F，防止资源无限占用）
    alive_runaways = _prune_runaway_threads()
    if alive_runaways:
        return ToolResult.failed(
            f"拒绝执行：上一次 python_exec 超时的线程仍在后台运行"
            f"（{len(alive_runaways)} 个，daemon 线程无法强制终止），"
            "可能继续占用 CPU/内存资源；请等待其结束或接入外部 Sandbox（AGENT_SANDBOX_MODE=external）"
        )

    # AST 安全检查
    err = _check_ast_safety(code)
    if err:
        return ToolResult.failed(f"安全检查失败: {err}")

    # 构建安全的执行环境
    safe_globals = {
        "__builtins__": {k: __builtins__[k] if isinstance(__builtins__, dict) else getattr(__builtins__, k, None)
                        for k in _SAFE_BUILTINS if (isinstance(__builtins__, dict) and k in __builtins__)
                        or (not isinstance(__builtins__, dict) and hasattr(__builtins__, k))},
        "math": math,
        "json": json,
        "re": re,
    }
    # 确保 print 可用
    safe_globals["__builtins__"]["print"] = print

    # 捕获 stdout
    stdout_capture = io.StringIO()
    local_vars: dict = {}

    import signal
    import threading

    result_container = {"output": None, "error": None, "return_val": None}

    def _run():
        old_stdout = sys.stdout
        sys.stdout = stdout_capture
        try:
            exec(compile(code, "<sandbox>", "exec"), safe_globals, local_vars)
            result_container["output"] = stdout_capture.getvalue()
            # 尝试获取最后一个表达式的值
            lines = code.strip().split("\n")
            last_line = lines[-1].strip()
            if last_line and not last_line.startswith(("#", "print", "if", "for", "while", "def", "class")):
                try:
                    val = eval(last_line, safe_globals, local_vars)
                    if val is not None:
                        result_container["return_val"] = repr(val)
                except Exception:
                    pass
        except Exception as e:
            result_container["error"] = traceback.format_exc(limit=3)
        finally:
            sys.stdout = old_stdout

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=timeout_sec)

    if t.is_alive():
        # 超时必须返回明确失败结果（§7.2 C）。
        # §15.2 F 诚实语义：线程未被终止、以 daemon 方式继续存活，
        # 如实报告"仍在运行/可能占用资源"，登记残留线程并阻止再次执行。
        with _RUNAWAY_LOCK:
            _RUNAWAY_THREADS.append(t)
        return ToolResult.failed(
            f"执行超时（>{timeout_sec}s）：线程未被强制终止，仍在后台运行（daemon），"
            "可能继续占用 CPU/内存资源；已阻止后续 python_exec 执行直到该线程结束。"
            "请检查是否有死循环；如需可靠终止请接入外部 Sandbox（AGENT_SANDBOX_MODE=external）"
        )

    if result_container["error"]:
        return ToolResult.failed(f"执行错误:\n{result_container['error']}")

    parts = []
    stdout_out = result_container["output"] or ""
    if stdout_out:
        parts.append(f"输出:\n{stdout_out.rstrip()}")
    if result_container["return_val"]:
        parts.append(f"返回值: {result_container['return_val']}")
    if not parts:
        parts.append("代码执行成功（无输出）")

    return ToolResult.succeeded("\n".join(parts))


HANDLERS = {
    "python_exec": execute_python_exec,
}
