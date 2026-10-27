"""离线公历日期计算：星期、日期加减、日期差和时区日期。"""

import datetime as dt
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DATE_TOOL = {
    "type": "function",
    "function": {
        "name": "calculate_date",
        "description": (
            "Calculate Gregorian calendar dates reliably without network access. "
            "Use info for a date and weekday, add for adding/subtracting calendar days, "
            "and difference for signed end_date minus date in days (not inclusive counting). "
            "Dates accept today, tomorrow, yesterday or exact YYYY-MM-DD, years 0001-9999. "
            "Relative dates use the selected timezone at tool execution time. "
            "Omit timezone to use the computer's local timezone; an explicit timezone must "
            "be an IANA name such as Asia/Shanghai or America/New_York. "
            "This tool does not manage schedules, set reminders, convert clock times, "
            "or calculate holidays, working days, lunar dates or month/year offsets."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "operation": {"type": "string", "enum": ["info", "add", "difference"],
                              "description": "info, add, or difference"},
                "date": {"type": "string", "description": "Date to inspect or start from; defaults to today"},
                "end_date": {"type": "string", "description": "Required only for difference; same date formats"},
                "days": {"type": "string", "description": "Required only for add: signed integer string, e.g. 30 or -7; at most 7 digits"},
                "timezone": {"type": "string", "description": "Optional IANA timezone; omit for local timezone"},
            },
            "required": ["operation"],
            "additionalProperties": False,
        },
    },
}
_WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")


class DateCalculationError(ValueError):
    """参数错误可交回模型纠正，且不产生文件或日程副作用。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _parse_date(value: str, today: dt.date) -> dt.date:
    if not isinstance(value, str):
        raise DateCalculationError("invalid_date", "日期须为 today、tomorrow、yesterday 或 YYYY-MM-DD。")
    if value in {"today", "tomorrow", "yesterday"}:
        try:
            return today + dt.timedelta(days={"today": 0, "tomorrow": 1, "yesterday": -1}[value])
        except OverflowError as error:
            raise DateCalculationError("date_out_of_range", "日期须在 0001-01-01 至 9999-12-31 之间。") from error
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise DateCalculationError("invalid_date", "日期格式须为 YYYY-MM-DD。")
    try:
        return dt.date.fromisoformat(value)
    except ValueError as error:
        raise DateCalculationError("invalid_date", "日期不存在；请核对年份、月份和日。") from error


def calculate_date(
    operation: str,
    date: str = "today",
    end_date: str | None = None,
    days: str | None = None,
    timezone: str | None = None,
) -> dict[str, object]:
    """Resolve one instant in the chosen zone, then perform calendar-day arithmetic."""
    if operation not in ("info", "add", "difference"):
        raise DateCalculationError("invalid_operation", "operation 须为 info、add 或 difference。")
    if (operation != "difference" and end_date is not None
            or operation != "add" and days is not None):
        raise DateCalculationError("unexpected_argument", "end_date 仅用于 difference，days 仅用于 add。")
    if operation == "difference" and end_date is None:
        raise DateCalculationError("missing_end_date", "difference 需要 end_date。")
    if operation == "add" and (not isinstance(days, str) or not re.fullmatch(r"[+-]?[0-9]{1,7}", days)):
        raise DateCalculationError("invalid_days", "add 需要 days：最多 7 位数字的有符号整数字符串。")

    moment = dt.datetime.now().astimezone()
    if timezone is not None:
        if not isinstance(timezone, str) or not timezone.strip():
            raise DateCalculationError("invalid_timezone", "timezone 须为有效的 IANA 时区名。")
        try:
            moment = moment.astimezone(ZoneInfo(timezone))
        except (ValueError, ZoneInfoNotFoundError) as error:
            raise DateCalculationError("invalid_timezone", "未知时区；请使用 Asia/Shanghai 等 IANA 时区名。") from error

    today = moment.date()
    start = _parse_date(date, today)
    result = {"operation": operation, "timezone": timezone or "local",
              "reference_date": today.isoformat(), "reference_utc_offset": moment.strftime("%z")}
    if operation == "difference":
        end = _parse_date(end_date, today)
        return {**result, "start_date": start.isoformat(), "end_date": end.isoformat(),
                "days": (end - start).days, "counting": "end_date_minus_start_date"}
    if operation == "add":
        offset = int(days)
        try:
            target = start + dt.timedelta(days=offset)
        except OverflowError as error:
            raise DateCalculationError("date_out_of_range", "结果须在 0001-01-01 至 9999-12-31 之间。") from error
        result.update({"start_date": start.isoformat(), "days": offset})
    else:
        target = start
    return {**result, "date": target.isoformat(), "weekday": _WEEKDAYS[target.weekday()],
            "weekday_iso": target.isoweekday()}
