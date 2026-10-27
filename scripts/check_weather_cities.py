# 联网检查 100 个全球城市的天气工具，并将逐项结果写入本地 JSON 报告。

import argparse
import asyncio
import csv
import json
import math
from datetime import date, datetime, timezone
from pathlib import Path

from dianstar.tools.local.weather import WeatherUnavailable, get_weather


ROOT = Path(__file__).resolve().parent.parent
CASES = ROOT / "tests" / "data" / "weather_cities.csv"
DEFAULT_REPORT = ROOT / ".local" / "weather_city_report.json"


def load_cases() -> list[dict[str, str]]:
    with CASES.open(encoding="utf-8", newline="") as source:
        rows = list(csv.DictReader(source))
    if len(rows) != 100:
        raise ValueError(f"Expected 100 city cases, found {len(rows)}.")
    if len({(row["city"], row["country_code"]) for row in rows}) != 100:
        raise ValueError("City cases must be unique by city and country.")
    if any(not row["region"] or not row["city"] or len(row["country_code"]) != 2 for row in rows):
        raise ValueError("Each case needs a region, city, and two-letter country code.")
    return rows


def validate(case: dict[str, str], weather: dict[str, object]) -> list[str]:
    problems = []
    if weather.get("country_code") != case["country_code"]:
        problems.append(f"country: expected {case['country_code']}, got {weather.get('country_code')}")
    if not isinstance(weather.get("location"), str) or not weather["location"].strip():
        problems.append("missing location")
    try:
        local_day = date.fromisoformat(str(weather["date"]))
        if abs((local_day - datetime.now(timezone.utc).date()).days) > 1:
            problems.append(f"unexpected local date: {local_day}")
    except (KeyError, ValueError):
        problems.append("missing or invalid date")
    temperatures = {}
    for field in ("temperature_max_c", "temperature_min_c"):
        value = weather.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            problems.append(f"missing or invalid {field}")
        else:
            temperatures[field] = value
    if len(temperatures) == 2 and temperatures["temperature_max_c"] < temperatures["temperature_min_c"]:
        problems.append("daily maximum is lower than minimum")
    current = weather.get("temperature_current_c")
    if "temperature_current_c" not in weather or (current is not None and (
        isinstance(current, bool) or not isinstance(current, (int, float)) or not math.isfinite(current)
    )):
        problems.append("missing or invalid current temperature field")
    return problems


async def check_one(case: dict[str, str], semaphore: asyncio.Semaphore) -> dict[str, object]:
    async with semaphore:
        for attempt in range(3):
            try:
                weather = await get_weather(case.get("query") or case["city"], "today")
                break
            except WeatherUnavailable as error:
                if "Weather service is unavailable" not in str(error) or attempt == 2:
                    return {**case, "status": "error", "reason": str(error)}
                await asyncio.sleep(attempt + 1)
            except Exception as error:
                return {**case, "status": "error", "reason": f"Unexpected {type(error).__name__}: {error}"}
    problems = validate(case, weather)
    return {**case, "status": "fail" if problems else "pass", "problems": problems,
            "result": {key: weather.get(key) for key in (
                "location", "country_code", "date", "temperature_current_c",
                "temperature_max_c", "temperature_min_c",
            )}}


async def run(limit: int, concurrency: int, output: Path) -> int:
    cases = load_cases()[:limit]
    semaphore = asyncio.Semaphore(concurrency)
    results = await asyncio.gather(*(check_one(case, semaphore) for case in cases))
    summary = {status: sum(row["status"] == status for row in results)
               for status in ("pass", "fail", "error")}
    report = {"checked_at_utc": datetime.now(timezone.utc).isoformat(),
              "source": "Open-Meteo live API", "cases": len(cases),
              "summary": summary, "results": results}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Cities: {len(cases)} | passed: {summary['pass']} | failed: {summary['fail']} | errors: {summary['error']}")
    for row in results:
        if row["status"] != "pass":
            print(f"{row['region']} / {row['city']} ({row['country_code']}): "
                  f"{row.get('reason') or '; '.join(row['problems'])}")
    print(f"Report: {output}")
    return 0 if summary["pass"] == len(cases) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Live weather checks for 100 worldwide cities")
    parser.add_argument("--limit", type=int, default=100, help="Run the first N cases (1-100)")
    parser.add_argument("--concurrency", type=int, default=3, help="Maximum simultaneous city checks (1-5)")
    parser.add_argument("--output", type=Path, default=DEFAULT_REPORT, help="JSON report path")
    args = parser.parse_args()
    if not 1 <= args.limit <= 100 or not 1 <= args.concurrency <= 5:
        parser.error("--limit must be 1-100 and --concurrency must be 1-5")
    return asyncio.run(run(args.limit, args.concurrency, args.output))


if __name__ == "__main__":
    raise SystemExit(main())
