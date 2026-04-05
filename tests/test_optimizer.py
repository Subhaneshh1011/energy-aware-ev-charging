from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from backend.optimizer import optimize_charging


def build_forecast(start: datetime, values: list[float]) -> list[dict]:
    return [
        {
            "timestamp": start + timedelta(hours=index),
            "carbon_intensity": value,
        }
        for index, value in enumerate(values, start=1)
    ]


def test_optimizer_prefers_lowest_carbon_window() -> None:
    now = datetime.fromisoformat("2026-04-04T15:00:00+05:30")
    forecast = build_forecast(now, [520, 500, 410, 390, 430, 470])

    recommendation = optimize_charging(
        battery_capacity=60,
        current_charge=30,
        target_charge=60,
        charging_power=7.5,
        forecast=forecast,
        current_carbon_intensity=530,
        now=now,
    )

    assert recommendation["expected_carbon"] == 410.0
    assert recommendation["carbon_savings_percent"] > 0


def test_optimizer_raises_for_invalid_charge_targets() -> None:
    now = datetime.fromisoformat("2026-04-04T15:00:00+05:30")

    with pytest.raises(ValueError):
        optimize_charging(
            battery_capacity=60,
            current_charge=80,
            target_charge=60,
            charging_power=7.5,
            forecast=[],
            current_carbon_intensity=530,
            now=now,
        )
