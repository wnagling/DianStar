# 裁剪天气工具返回给模型的可信数据。

import json

def weather_data(weather: dict[str, object]) -> str:
    """Only send the model the fields needed to answer."""
    fields = (
        "location", "date", "temperature_current_c", "temperature_max_c", "temperature_min_c",
        "current_condition", "daily_most_severe_condition", "precipitation_sum_mm",
        "precipitation_probability_max_pct", "wind_speed_current_kmh", "wind_speed_max_kmh",
    )
    return json.dumps({key: weather[key] for key in fields if key in weather}, ensure_ascii=False)
