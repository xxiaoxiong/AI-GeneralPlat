"""🐍 代码执行工具：python_exec（受限沙箱执行 Python 代码）"""
import sys
import io
import ast
import math
import json
import re
import traceback
from typing import Dict, List


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


def execute_python_exec(params: dict) -> str:
    code = params.get("code", "").strip()
    timeout_sec = min(int(params.get("timeout", 5)), 10)

    if not code:
        return "错误：代码为空"

    # AST 安全检查
    err = _check_ast_safety(code)
    if err:
        return f"安全检查失败: {err}"

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
        return f"执行超时（>{timeout_sec}s），请检查是否有死循环"

    if result_container["error"]:
        return f"执行错误:\n{result_container['error']}"

    parts = []
    stdout_out = result_container["output"] or ""
    if stdout_out:
        parts.append(f"输出:\n{stdout_out.rstrip()}")
    if result_container["return_val"]:
        parts.append(f"返回值: {result_container['return_val']}")
    if not parts:
        parts.append("代码执行成功（无输出）")

    return "\n".join(parts)


HANDLERS = {
    "python_exec": execute_python_exec,
}
