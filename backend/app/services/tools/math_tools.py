"""🔢 计算数学工具：calculator"""
import math
from typing import Dict, Any, List


TOOLS: List[Dict[str, Any]] = [
    {
        "name": "calculator",
        "display_name": "🔢 数学计算器",
        "category": "计算数学",
        "description": "执行数学计算，支持加减乘除、幂运算、三角函数、对数等，如 'sqrt(16) + sin(pi/2)'",
        "parameters": {
            "expression": {"type": "string", "description": "数学表达式，支持 +,-,*,/,**,%,sqrt,sin,cos,tan,log,pi,e 等"},
        },
        "required": ["expression"],
    },
]

_SAFE_NS = {
    "__builtins__": {},
    "sqrt": math.sqrt, "sin": math.sin, "cos": math.cos, "tan": math.tan,
    "asin": math.asin, "acos": math.acos, "atan": math.atan, "atan2": math.atan2,
    "log": math.log, "log10": math.log10, "log2": math.log2, "exp": math.exp,
    "abs": abs, "round": round, "pow": math.pow,
    "floor": math.floor, "ceil": math.ceil,
    "pi": math.pi, "e": math.e, "inf": math.inf,
}

def execute_calculator(params: dict) -> str:
    expr = params.get("expression", "")
    try:
        result = eval(expr, _SAFE_NS)
        return f"计算结果: {expr} = {result}"
    except Exception as e:
        return f"计算错误: {e}"


HANDLERS = {
    "calculator": execute_calculator,
}
