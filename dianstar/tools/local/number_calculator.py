"""受限十进制表达式计算，不执行 Python 代码。"""

import ast
from decimal import (Context, Decimal, DecimalException, DivisionByZero, Inexact,
                     Overflow, ROUND_HALF_EVEN, Underflow, localcontext)
import re


NUMBER_TOOL = {
    "type": "function",
    "function": {
        "name": "calculate_number",
        "description": (
            "Calculate a numeric expression locally using decimal arithmetic. Supports "
            "+, -, *, /, parentheses, unary signs, scientific-notation literals, and ** "
            "with integer exponents from -1000 to 1000. To compare numbers, calculate their "
            "difference. Percentages must be expressed as /100; ^ and % are not supported. "
            "No functions, variables, strings, code execution, units or symbolic algebra. "
            "Maximum expression length 512, AST nodes 128, evaluation depth 32, numeric "
            "literal precision 50 digits and decimal magnitude exponent +/-1000. "
            "Arithmetic uses 50 significant digits with ROUND_HALF_EVEN; the result is a "
            "decimal string and approximate=true means intermediate or final arithmetic "
            "was rounded. Division by zero, excessive complexity and range errors are returned as errors."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "expression": {"type": "string", "description": "Numeric expression, e.g. (0.1+0.2)*3, 9.11-9.8, or 100*(1+5/100)**3"},
            },
            "required": ["expression"],
            "additionalProperties": False,
        },
    },
}
_MAX_LENGTH = 512
_MAX_NODES = 128
_MAX_DEPTH = 32
_PRECISION = 50
_MAX_EXPONENT = 1000
_LITERAL = re.compile(r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")


class NumberCalculationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _bounded(value: Decimal) -> Decimal:
    if not value.is_finite():
        raise NumberCalculationError("invalid_result", "结果须为有限实数。")
    if abs(value.adjusted()) > _MAX_EXPONENT:
        raise NumberCalculationError("number_out_of_range", "数字量级超出支持范围。")
    return value


def _evaluate(node: ast.AST, expression: str, depth: int = 0) -> Decimal:
    if depth > _MAX_DEPTH:
        raise NumberCalculationError("expression_too_complex", "表达式嵌套过深，请拆分计算。")
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        literal = ast.get_source_segment(expression, node)
        if literal is None or not _LITERAL.fullmatch(literal):
            raise NumberCalculationError("invalid_literal", "仅支持十进制数字及科学计数法。")
        value = Decimal(literal)
        if len(value.as_tuple().digits) > _PRECISION:
            raise NumberCalculationError("literal_too_precise", "单个数字最多支持 50 位有效数字。")
        return _bounded(value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _evaluate(node.operand, expression, depth + 1)
        return _bounded(+value if isinstance(node.op, ast.UAdd) else -value)
    if not isinstance(node, ast.BinOp) or not isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow)):
        raise NumberCalculationError("unsupported_expression", "仅支持数字、括号、正负号、加减乘除及整数次幂。")
    left = _evaluate(node.left, expression, depth + 1)
    right = _evaluate(node.right, expression, depth + 1)
    if isinstance(node.op, ast.Add):
        result = left + right
    elif isinstance(node.op, ast.Sub):
        result = left - right
    elif isinstance(node.op, ast.Mult):
        result = left * right
    elif isinstance(node.op, ast.Div):
        result = left / right
    else:
        if abs(right) > _MAX_EXPONENT or right != right.to_integral_value():
            raise NumberCalculationError("invalid_exponent", "幂指数须为 -1000 至 1000 的整数。")
        if left.is_zero() and right < 0:
            raise NumberCalculationError("division_by_zero", "零不能取负整数次幂。")
        result = left ** int(right)
    return _bounded(result)


def calculate_number(expression: str) -> dict[str, object]:
    """Parse an allowlisted AST and return a bounded, explicitly rounded decimal result."""
    if not isinstance(expression, str) or not expression.strip():
        raise NumberCalculationError("invalid_expression", "请提供非空的数字表达式。")
    if len(expression) > _MAX_LENGTH:
        raise NumberCalculationError("expression_too_long", "表达式最多 512 个字符，请拆分计算。")
    expression = expression.strip()
    if not re.fullmatch(r"[0-9eE+\-*/(). \t\r\n]+", expression):
        raise NumberCalculationError("invalid_expression", "表达式只能包含十进制数字、科学计数法、括号及 + - * / **。")
    try:
        tree = ast.parse(expression, mode="eval")
    except (SyntaxError, ValueError, RecursionError) as error:
        raise NumberCalculationError("invalid_expression", "表达式语法无效。") from error
    if sum(1 for _ in ast.walk(tree)) > _MAX_NODES:
        raise NumberCalculationError("expression_too_complex", "表达式过于复杂，请拆分计算。")
    context = Context(prec=_PRECISION, rounding=ROUND_HALF_EVEN,
                      Emin=-_MAX_EXPONENT, Emax=_MAX_EXPONENT)
    context.traps[Underflow] = True
    try:
        with localcontext(context) as current:
            result = _evaluate(tree.body, expression)
            approximate = bool(current.flags[Inexact])
            text = "0" if result.is_zero() else format(result, "f")
            if "." in text:
                text = text.rstrip("0").rstrip(".")
    except DivisionByZero as error:
        raise NumberCalculationError("division_by_zero", "除数不能为零。") from error
    except (Overflow, Underflow) as error:
        raise NumberCalculationError("number_out_of_range", "计算结果或中间值超出支持范围。") from error
    except DecimalException as error:
        raise NumberCalculationError("invalid_calculation", "计算无效，例如 0 的 0 次幂。") from error
    return {"expression": expression, "result": text, "approximate": approximate,
            "precision": _PRECISION, "rounding": ROUND_HALF_EVEN}
