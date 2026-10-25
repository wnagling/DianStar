"""日期计算的边界、时区，以及经真实工具注册表执行的离线集成测试。"""

import datetime as dt
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from dianstar.agent import ask_deepseek
from dianstar.deepseek import _parse_unparsed_tool_calls
from dianstar.tools.date_calculator import DATE_TOOL, DateCalculationError, calculate_date
from dianstar.tools.weather import WEATHER_TOOL
from dianstar.tools.number_calculator import NUMBER_TOOL
from dianstar.tools.web import SEARCH_TOOL, FETCH_TOOL
from dianstar.tools.web_access import WEB_ACCESS_STATUS_TOOL as WEB_ACCESS_TOOL


class DateCalculatorTests(unittest.TestCase):
    def test_known_date_and_weekday(self):
        result = calculate_date("info", date="2026-01-01")
        self.assertEqual(result["date"], "2026-01-01")
        self.assertEqual(result["weekday"], "星期四")
        self.assertEqual(result["weekday_iso"], 4)

    def test_add_calendar_days_across_boundaries(self):
        for start, days, expected in (
            ("2026-01-31", "1", "2026-02-01"),
            ("2026-12-31", "+1", "2027-01-01"),
            ("2028-02-28", "1", "2028-02-29"),
            ("2028-03-01", "-1", "2028-02-29"),
            ("2026-02-28", "1", "2026-03-01"),
            ("2026-01-01", "0", "2026-01-01"),
            ("2026-03-07", "1", "2026-03-08"),
        ):
            with self.subTest(start=start, days=days):
                self.assertEqual(calculate_date("add", date=start, days=days)["date"], expected)

    def test_difference_is_signed_and_not_inclusive(self):
        for start, end, expected in (
            ("2028-02-28", "2028-03-01", 2),
            ("2028-03-01", "2028-02-28", -2),
            ("2026-01-01", "2026-01-01", 0),
        ):
            with self.subTest(start=start, end=end):
                result = calculate_date("difference", date=start, end_date=end)
                self.assertEqual(result["days"], expected)
                self.assertEqual(result["counting"], "end_date_minus_start_date")

    def test_same_instant_can_be_different_dates_in_different_timezones(self):
        instant = dt.datetime(2026, 1, 1, 0, 30, tzinfo=dt.timezone.utc)
        with patch("dianstar.tools.date_calculator.dt.datetime") as clock:
            clock.now.return_value = instant
            shanghai = calculate_date("info", timezone="Asia/Shanghai")
            los_angeles = calculate_date("info", timezone="America/Los_Angeles")
        self.assertEqual(shanghai["date"], "2026-01-01")
        self.assertEqual(shanghai["reference_utc_offset"], "+0800")
        self.assertEqual(los_angeles["date"], "2025-12-31")
        self.assertEqual(los_angeles["reference_utc_offset"], "-0800")

    def test_relative_dates_and_default_add_date(self):
        instant = dt.datetime(2026, 12, 31, 12, tzinfo=dt.timezone.utc)
        with patch("dianstar.tools.date_calculator.dt.datetime") as clock:
            clock.now.return_value = instant
            for word, expected in (("yesterday", "2026-12-30"), ("today", "2026-12-31"),
                                   ("tomorrow", "2027-01-01")):
                with self.subTest(word=word):
                    self.assertEqual(calculate_date("info", date=word, timezone="UTC")["date"], expected)
            self.assertEqual(calculate_date("add", days="30", timezone="UTC")["date"], "2027-01-30")

    def test_local_timezone_is_default_and_day_offsets_are_not_hour_offsets(self):
        instant = dt.datetime(2026, 1, 1, 0, 30, tzinfo=dt.timezone(dt.timedelta(hours=8)))
        with patch("dianstar.tools.date_calculator.dt.datetime") as clock:
            clock.now.return_value = instant
            local = calculate_date("info")
        self.assertEqual(local["timezone"], "local")
        self.assertEqual(local["date"], instant.astimezone().date().isoformat())
        result = calculate_date("add", date="2026-03-07", days="1", timezone="America/New_York")
        self.assertEqual(result["date"], "2026-03-08")

    def test_dst_offset_comes_from_selected_timezone(self):
        instant = dt.datetime(2026, 7, 1, 12, tzinfo=dt.timezone.utc)
        with patch("dianstar.tools.date_calculator.dt.datetime") as clock:
            clock.now.return_value = instant
            result = calculate_date("info", timezone="America/New_York")
        self.assertEqual(result["reference_utc_offset"], "-0400")

    def test_invalid_arguments_are_structured_errors(self):
        cases = [
            ({"operation": "remind"}, "invalid_operation"),
            ({"operation": "info", "date": "2026-02-29"}, "invalid_date"),
            ({"operation": "info", "date": "2026-13-01"}, "invalid_date"),
            ({"operation": "info", "date": "20260101"}, "invalid_date"),
            ({"operation": "info", "date": "2026-W01-1"}, "invalid_date"),
            ({"operation": "info", "date": "0000-01-01"}, "invalid_date"),
            ({"operation": "info", "timezone": "Unknown/Timezone"}, "invalid_timezone"),
            ({"operation": "info", "timezone": "../UTC"}, "invalid_timezone"),
            ({"operation": "info", "timezone": ""}, "invalid_timezone"),
            ({"operation": "add"}, "invalid_days"),
            ({"operation": "add", "days": "1.5"}, "invalid_days"),
            ({"operation": "add", "days": 1}, "invalid_days"),
            ({"operation": "add", "days": True}, "invalid_days"),
            ({"operation": "add", "days": "9" * 5000}, "invalid_days"),
            ({"operation": "difference"}, "missing_end_date"),
            ({"operation": "info", "days": "1"}, "unexpected_argument"),
            ({"operation": "add", "days": "1", "end_date": "2026-01-01"}, "unexpected_argument"),
        ]
        for arguments, code in cases:
            with self.subTest(code=code, operation=arguments["operation"]):
                with self.assertRaises(DateCalculationError) as captured:
                    calculate_date(**arguments)
                self.assertEqual(captured.exception.code, code)

    def test_out_of_range_calculation_does_not_escape_as_overflow(self):
        for start, days in (("0001-01-01", "-1"), ("9999-12-31", "1")):
            with self.subTest(start=start):
                with self.assertRaises(DateCalculationError) as captured:
                    calculate_date("add", date=start, days=days)
                self.assertEqual(captured.exception.code, "date_out_of_range")

    def test_dsml_date_tool_uses_the_same_schema_and_optional_arguments(self):
        raw = ('<invoke name="calculate_date">'
               '<parameter name="operation" string="true">add</parameter>'
               '<parameter name="date" string="true">2028-02-28</parameter>'
               '<parameter name="days" string="true">1</parameter></invoke>')
        actions = _parse_unparsed_tool_calls(raw, [WEATHER_TOOL, DATE_TOOL])
        self.assertEqual(actions[0].name, "calculate_date")
        self.assertEqual(calculate_date(**actions[0].arguments)["date"], "2028-02-29")


class DateToolLoopTests(unittest.IsolatedAsyncioTestCase):
    @patch("dianstar.deepseek.AsyncOpenAI")
    async def test_registry_advertises_and_dispatches_dates_and_model_can_correct_errors(self, client_class):
        def call(call_id, arguments):
            return SimpleNamespace(id=call_id, function=SimpleNamespace(
                name="calculate_date", arguments=json.dumps(arguments)))

        def tool_response(calls):
            return SimpleNamespace(choices=[SimpleNamespace(finish_reason="tool_calls",
                message=SimpleNamespace(content=None, tool_calls=calls))])

        client = client_class.return_value
        client.chat.completions.create = AsyncMock(side_effect=[
            tool_response([call("bad", {"operation": "info", "date": "2026-02-29"})]),
            tool_response([
                call("good", {"operation": "info", "date": "2028-02-29"}),
                call("difference", {"operation": "difference", "date": "2028-02-28", "end_date": "2028-03-01"}),
            ]),
            SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop",
                message=SimpleNamespace(content="2028-02-29 是星期二，两个日期相差 2 天。"))]),
        ])
        history = []
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env"
            env_file.write_text("DEEPSEEK_API_KEY=fake-test-key\n", encoding="utf-8")
            answer = await ask_deepseek("计算日期", env_file=env_file, history=history)
        requests = client.chat.completions.create.await_args_list
        self.assertEqual(requests[0].kwargs["tools"], [WEATHER_TOOL, DATE_TOOL, NUMBER_TOOL, WEB_ACCESS_TOOL, SEARCH_TOOL, FETCH_TOOL])
        self.assertIn("calculate_date", requests[0].kwargs["messages"][0]["content"])
        self.assertIn("invalid_date", requests[1].kwargs["messages"][-1]["content"])
        result_messages = requests[2].kwargs["messages"][-2:]
        self.assertEqual(json.loads(result_messages[0]["content"])["date"], "2028-02-29")
        self.assertEqual(json.loads(result_messages[1]["content"])["days"], 2)
        self.assertEqual([item["tool_call_id"] for item in result_messages], ["good", "difference"])
        self.assertEqual(history[-1]["content"], answer)
