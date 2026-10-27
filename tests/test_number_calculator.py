import json
import tempfile
import unittest
from decimal import localcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from dianstar.app.runtime import run_turn
from dianstar.providers.deepseek import _parse_unparsed_tool_calls
from dianstar.tools.local.number_calculator import NUMBER_TOOL, NumberCalculationError, calculate_number


class NumberCalculatorTests(unittest.TestCase):
    def test_decimal_arithmetic_and_precedence(self):
        for expression, expected in (
            ("0.1+0.2", "0.3"), ("9.11-9.8", "-0.69"),
            ("(125.5-20.5)*3/7", "45"), ("2+3*4", "14"),
            ("-2**2", "-4"), ("(-2)**2", "4"), ("2**-3", "0.125"),
            ("1e3*.002", "2"), ("100*(1+5/100)**3", "115.7625"),
            ("-0.0", "0"), ("+(.5)", "0.5"),
            ("12345678901234567890123456789012345678901234567890+0",
             "12345678901234567890123456789012345678901234567890"),
        ):
            with self.subTest(expression=expression):
                result = calculate_number(expression)
                self.assertEqual(result["result"], expected)
                self.assertFalse(result["approximate"])

    def test_rounding_is_reported_including_intermediate_rounding(self):
        third = calculate_number("1/3")
        self.assertEqual(third["result"], "0." + "3" * 50)
        self.assertTrue(third["approximate"])
        self.assertEqual(third["precision"], 50)
        self.assertEqual(third["rounding"], "ROUND_HALF_EVEN")
        self.assertTrue(calculate_number("(1/3)*3")["approximate"])
        self.assertFalse(calculate_number("1/8")["approximate"])

    def test_decimal_context_is_isolated_from_caller(self):
        with localcontext() as context:
            context.prec = 3
            self.assertEqual(calculate_number("1/8")["result"], "0.125")
            self.assertEqual(len(calculate_number("1/7")["result"]), 52)
            self.assertEqual(context.prec, 3)
            self.assertFalse(any(context.flags.values()))

    def test_scientific_literals_are_parsed_from_source_without_float_loss(self):
        self.assertEqual(calculate_number("1e1000")["result"], "1" + "0" * 1000)
        self.assertEqual(calculate_number("1e-1000")["result"], "0." + "0" * 999 + "1")

    def test_invalid_and_excessive_inputs_return_specific_errors(self):
        cases = [
            (None, "invalid_expression"), (12, "invalid_expression"),
            ("", "invalid_expression"), ("1+", "invalid_expression"),
            ("__import__('os').system('echo bad')", "invalid_expression"),
            ("open('file','w')", "invalid_expression"),
            ("True", "invalid_expression"), ("1%2", "invalid_expression"),
            ("2^3", "invalid_expression"), ("1_000", "invalid_expression"),
            ("1//2", "unsupported_expression"), ("1(2)", "unsupported_expression"),
            ("1/0", "division_by_zero"), ("0**-1", "division_by_zero"),
            ("0**0", "invalid_calculation"), ("2**.5", "invalid_exponent"),
            ("2**1001", "invalid_exponent"), ("2**-1001", "invalid_exponent"),
            ("1e1001", "number_out_of_range"), ("1e-1001", "number_out_of_range"),
            ("1e1000*10", "number_out_of_range"),
            ("1e-1000/1e1000", "number_out_of_range"),
            ("9" * 51, "literal_too_precise"),
            ("1" * 513, "expression_too_long"),
            ("+" * 34 + "1", "expression_too_complex"),
            ("+".join(["1"] * 65), "expression_too_complex"),
        ]
        for expression, code in cases:
            with self.subTest(expression=expression):
                with self.assertRaises(NumberCalculationError) as captured:
                    calculate_number(expression)
                self.assertEqual(captured.exception.code, code)

    def test_dsml_dispatch_uses_the_numeric_schema(self):
        raw = ('<invoke name="calculate_number">'
               '<parameter name="expression" string="true">0.1+0.2</parameter></invoke>')
        action = _parse_unparsed_tool_calls(raw, [NUMBER_TOOL])[0]
        self.assertEqual(action.name, "calculate_number")
        self.assertEqual(calculate_number(**action.arguments)["result"], "0.3")


class NumberToolLoopTests(unittest.IsolatedAsyncioTestCase):
    @patch("dianstar.providers.deepseek.AsyncOpenAI")
    async def test_registered_tool_error_can_be_corrected_in_same_turn(self, client_class):
        def response(expression, call_id):
            call = SimpleNamespace(id=call_id, function=SimpleNamespace(
                name="calculate_number", arguments=json.dumps({"expression": expression})))
            return SimpleNamespace(choices=[SimpleNamespace(finish_reason="tool_calls",
                message=SimpleNamespace(content=None, tool_calls=[call]))])

        create = client_class.return_value.chat.completions.create = AsyncMock(side_effect=[
            response("1/0", "bad"), response("0.1+0.2", "good"),
            SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop",
                message=SimpleNamespace(content="除数不能为零；0.1+0.2=0.3。"))]),
        ])
        history = []
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env"
            env_file.write_text("DEEPSEEK_API_KEY=fake-test-key\n", encoding="utf-8")
            answer = await run_turn("计算数字", env_file=env_file, history=history)
        requests = create.await_args_list
        self.assertIn(NUMBER_TOOL, requests[0].kwargs["tools"])
        self.assertIn("calculate_number", requests[0].kwargs["messages"][0]["content"])
        self.assertEqual(json.loads(requests[1].kwargs["messages"][-1]["content"])["error"], "division_by_zero")
        observed = requests[2].kwargs["messages"][-1]
        self.assertEqual(observed["tool_call_id"], "good")
        self.assertEqual(json.loads(observed["content"])["result"], "0.3")
        self.assertEqual(history[-1]["content"], answer)
