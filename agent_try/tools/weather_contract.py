# 天气任务的完成规则：识别天气问题、裁剪工具数据并核对回答内容。

import json
import re
from collections import Counter

from ..loop import Observation, Verdict


WEATHER_WORDS = re.compile(r"天气|气温|温度|多少度|最高温|最低温|weather|temperature|temeprature", re.I)
DATE_WORDS = re.compile(r"今天|今日|明天|后天|\d{4}-\d{2}-\d{2}|\d{1,2}月\d{1,2}日|today|tomorrow", re.I)
TEMPERATURES = re.compile(r"(-?\d+(?:\.\d+)?)\s*(?:°\s*[Cc]|℃|摄氏度|度)")
CJK = re.compile(r"[\u4e00-\u9fff]")
WEATHER_REPLY_INSTRUCTIONS = (
    "请使用用户本轮问题的语言回答：英文问题用英文，中文问题用中文。"
    "For English weather answers, use the queried city name in English and label current, high, and low temperatures in °C."
    "天气问题必须先调用 get_weather，缺少日期时默认 today；缺少地点时询问用户地点。"
    "请根据用户的问题和天气查询结果回答。今天要回答当前气温及当日最高、最低气温；"
    "未来日期回答最高、最低气温。当前气温为空时说明暂不可用。"
    "不要写数据来源、观测时间或‘模型估计’，不要输出工具调用标记。"
    "只使用查询结果中的气温，准确写出查询到的城市，不要编造数值。"
)


def weather_data(weather: dict[str, object]) -> str:
    """Only send the model the fields needed to answer."""
    fields = ("location", "date", "temperature_current_c", "temperature_max_c", "temperature_min_c")
    return json.dumps({key: weather[key] for key in fields if key in weather}, ensure_ascii=False)


def _labeled_temperature(answer: str, label: str) -> float | None:
    match = re.search(rf"(?:{label})[^，。；,]{{0,24}}?{TEMPERATURES.pattern}", answer, re.I)
    return float(match.group(1)) if match else None


def english_question(question: str) -> bool:
    """Treat Latin-script questions without Chinese characters as English."""
    return not CJK.search(question) and bool(re.search(r"[A-Za-z]{2}", question))


def location_clarification(location: str, question: str, *, ambiguous: bool) -> str:
    """Ask for missing place information in the user's language."""
    city = location.replace("，", ",").partition(",")[0].strip(" \"'“”‘’")
    if english_question(question):
        if ambiguous:
            return f"I found several places named {city}. Which country or region do you mean?"
        return f"I couldn't find {city}. Could you check the spelling or tell me its country or region?"
    if ambiguous:
        return f"“{city}”可能对应多个地点。你要查哪个国家或地区的“{city}”？"
    return f"我没找到“{city}”。你能检查城市名称的拼写，或告诉我它所在的国家或地区吗？"


def same_weather_city(first: str, second: str) -> bool:
    """Allow a revised region, but keep the city and an explicit country."""
    def parts(location: str) -> list[str]:
        return [part.strip(" \"'“”‘’").casefold()
                for part in location.replace("，", ",").split(",")]

    original, revised = parts(first), parts(second)
    original_city = original[0].removesuffix("市")
    revised_city = revised[0].removesuffix("市")
    if not original_city or original_city != revised_city:
        return False
    return len(original) < 3 or (len(revised) >= 3 and original[-1] == revised[-1])


def weather_goal(question: str, history: list[dict[str, str]] | None) -> bool:
    if WEATHER_WORDS.search(question):
        return True
    if _follows_location_clarification(history):
        return True
    # A short date-only follow-up can reuse the city from the preceding exchange.
    if DATE_WORDS.fullmatch(question.strip().rstrip("？?。 ")):
        return any(WEATHER_WORDS.search(item["content"]) for item in (history or [])[-2:])
    return False


def _follows_location_clarification(history: list[dict[str, str]] | None) -> bool:
    if not history or len(history) < 2:
        return False
    previous, clarification = history[-2:]
    return (
        previous["role"] == "user"
        and clarification["role"] == "assistant"
        and bool(WEATHER_WORDS.search(previous["content"]))
        and bool(re.search(r"哪个国家|所在的国家|country or region|check the spelling", clarification["content"], re.I))
    )


def weather_date_unspecified(question: str, history: list[dict[str, str]] | None) -> bool:
    if DATE_WORDS.search(question):
        return False
    if _follows_location_clarification(history) and DATE_WORDS.search(history[-2]["content"]):
        return False
    return True


def verify_weather(
    answer: str, observations: list[Observation], *, question: str, date_unspecified: bool
) -> Verdict:
    english = english_question(question)
    if english and CJK.search(answer):
        return Verdict("retry", "The user asked in English. Answer entirely in English, including the city name.")
    observation = next((item for item in reversed(observations) if item.action.name == "get_weather"), None)
    if observation is None:
        if (not TEMPERATURES.search(answer) and
                re.search(r"城市|地点|地区|位置|city|location|place", answer, re.I) and
                re.search(r"[？?]", answer)):
            return Verdict("needs_input", answer=answer)
        return Verdict("retry", "天气问题必须先调用 get_weather。缺少地点时，请询问用户地点；缺少日期时默认 today。")

    weather = observation.result
    requested = observation.action.arguments["date"]
    if (re.search(r"今天|今日|today", question, re.I) and requested == "tomorrow") or (
        re.search(r"明天|tomorrow", question, re.I) and requested == "today"
    ):
        return Verdict("retry", "请重新查询用户指定的日期，再根据新结果回答。", retry_tool=True)
    explicit_date = re.search(r"\d{4}-\d{2}-\d{2}", question)
    if explicit_date and weather["date"] != explicit_date.group():
        return Verdict("retry", "请重新查询用户指定的日期，再根据新结果回答。", retry_tool=True)

    expected = [weather["temperature_max_c"], weather["temperature_min_c"]]
    if "temperature_current_c" in weather and weather["temperature_current_c"] is not None:
        expected.insert(0, weather["temperature_current_c"])
    actual = [float(value) for value in TEMPERATURES.findall(answer)]
    if re.search(r"°\s*[Ff]|℉", answer) or Counter(actual) != Counter(float(value) for value in expected):
        return Verdict("retry", "回答中的气温必须与查询结果完全一致，并包含当前气温（如果有）、最高和最低气温。不要添加其他气温。")
    if (_labeled_temperature(answer, "最高|高温|high|maximum|max") != float(weather["temperature_max_c"]) or
            _labeled_temperature(answer, "最低|低温|low|minimum|min") != float(weather["temperature_min_c"])):
        return Verdict("retry", "请把最高和最低气温与正确的数值对应起来。")
    if "temperature_current_c" in weather:
        current = weather["temperature_current_c"]
        if current is None:
            if not re.search(r"当前|现在|目前|current|now", answer, re.I) or not re.search(
                r"不可用|暂无|缺失|unavailable|missing", answer, re.I
            ):
                return Verdict("retry", "当前气温不可用，请在回答中说明。")
        elif _labeled_temperature(answer, "当前|现在|目前|实时|current|currently|now") != float(current):
            return Verdict("retry", "请把当前气温与正确的数值对应起来。")
    city = (observation.action.arguments["location"].split(",", 1)[0].strip(" \"'“”‘’")
            if english else str(weather["location"]).split(",", 1)[0].removesuffix("市"))
    if city.casefold() not in answer.casefold():
        return Verdict("retry", "回答中请明确写出查询到的城市。")
    _, month, day = (int(part) for part in str(weather["date"]).split("-"))
    stated_date = str(weather["date"]) in answer or f"{month}月{day}日" in answer
    if requested == "today":
        stated_date = stated_date or bool(re.search(r"今天|今日|当前|现在|today|current|now", answer, re.I))
    elif requested == "tomorrow":
        stated_date = stated_date or bool(re.search(r"明天|tomorrow", answer, re.I))
    if re.search(r"明天|tomorrow", question, re.I):
        stated_date = stated_date or bool(re.search(r"明天|tomorrow", answer, re.I))
    elif re.search(r"今天|今日|today", question, re.I):
        stated_date = stated_date or bool(re.search(r"今天|今日|当前|现在|today|current|now", answer, re.I))
    if not stated_date:
        return Verdict("retry", "回答中请明确写出查询日期。")
    if re.search(r"数据来源|Open-Meteo|模型估计|当地时间", answer, re.I):
        return Verdict("retry", "不要显示数据来源、查询时间或模型估计字样。")
    follow_up = "Which date would you like to check?" if english else "你想查询哪天的天气？"
    if date_unspecified and follow_up not in answer:
        answer = answer.rstrip() + " " + follow_up
    return Verdict("completed", answer=answer)
