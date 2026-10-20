# 编排一轮提问：调用模型与工具，验收回答，并更新成功的对话历史。

from pathlib import Path

from .deepseek import ENV_FILE, DeepSeekSession, create_client, usable_history
from .loop import Observation, ToolAction, Verdict, run_loop
from .tools.weather import (
    WEATHER_TOOL, WeatherLocationAmbiguous, WeatherLocationNotFound,
    WeatherUnavailable, get_weather,
)
from .tools.weather_contract import (
    WEATHER_REPLY_INSTRUCTIONS, location_clarification, same_weather_city,
    verify_weather, weather_data, weather_date_unspecified, weather_goal,
)


async def ask_deepseek(
    question: str,
    *,
    history: list[dict[str, str]] | None = None,
    env_file: Path = ENV_FILE,
) -> str:
    """Run one bounded agent task, exposing only verified text to the caller."""
    if not question.strip():
        raise ValueError("Please provide a question.")

    cleaned_history = usable_history(history)
    must_check_weather = weather_goal(question, cleaned_history)
    date_unspecified = must_check_weather and weather_date_unspecified(question, cleaned_history)
    client = create_client(env_file)
    session = DeepSeekSession(
        client, question, cleaned_history, WEATHER_REPLY_INSTRUCTIONS, [WEATHER_TOOL],
    )
    first_failed_location: str | None = None
    not_found_count = 0

    async def act(action: ToolAction) -> Observation | Verdict:
        nonlocal first_failed_location, not_found_count
        if action.name != "get_weather":
            raise ValueError("DeepSeek returned an unsupported tool call.")
        if first_failed_location and not same_weather_city(
            first_failed_location, action.arguments["location"]
        ):
            return Verdict("needs_input", answer=location_clarification(
                first_failed_location, question, ambiguous=False,
            ))
        try:
            result = await get_weather(action.arguments["location"], action.arguments["date"])
        except WeatherLocationAmbiguous:
            return Verdict("needs_input", answer=location_clarification(
                action.arguments["location"], question, ambiguous=True,
            ))
        except WeatherLocationNotFound:
            not_found_count += 1
            first_failed_location = first_failed_location or action.arguments["location"]
            if not_found_count >= 2:
                return Verdict("needs_input", answer=location_clarification(
                    first_failed_location, question, ambiguous=False,
                ))
            session.observe(action, '{"error":"location_not_found"}')
            return Verdict(
                "retry",
                "地名查询无结果。请结合用户原话识别城市、行政区和国家，"
                "只调整同一城市的地名写法后重新调用 get_weather 一次。"
                "可以使用行政区简称；不要更换城市或猜测其他城市。",
                retry_tool=True,
            )
        except WeatherUnavailable as error:
            raise ValueError(f"Weather lookup failed: {error}") from error
        session.observe(action, weather_data(result))
        return Observation(action, result)

    def verify(answer: str, observations: list[Observation]) -> Verdict:
        if must_check_weather or observations:
            return verify_weather(
                answer, observations, question=question, date_unspecified=date_unspecified,
            )
        return Verdict("completed", answer=answer)

    async with client:
        answer = await run_loop(session.think, act, verify, max_steps=6)
    if history is not None:
        history.extend((
            {"role": "user", "content": question},
            {"role": "assistant", "content": answer},
        ))
    return answer
