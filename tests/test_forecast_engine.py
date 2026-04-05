from __future__ import annotations

from datetime import datetime, timedelta

from backend.forecast_engine import generate_forecast


def test_generate_forecast_produces_expected_horizon() -> None:
    start = datetime.fromisoformat("2026-04-04T10:00:00+05:30")
    readings = [
        {
            "timestamp": start + timedelta(hours=index),
            "carbon_intensity": value,
        }
        for index, value in enumerate([540, 530, 520, 510, 500, 490])
    ]

    forecast = generate_forecast(readings, horizon_hours=6, step_minutes=60)

    assert len(forecast) == 6
    assert forecast[0]["timestamp"] == start + timedelta(hours=6)
    assert forecast[-1]["timestamp"] == start + timedelta(hours=11)
    assert all(point["carbon_intensity"] >= 10 for point in forecast)
