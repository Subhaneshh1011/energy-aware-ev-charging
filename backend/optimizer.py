from __future__ import annotations

import math
from datetime import datetime, timedelta


def optimize_charging(
    battery_capacity: float,
    current_charge: float,
    target_charge: float,
    charging_power: float,
    forecast: list[dict],
    current_carbon_intensity: float,
    urgency_factor: float = 0.0,
    deadline_hours: float | None = None,
    now: datetime | None = None,
) -> dict:
    now = now or datetime.now().astimezone()

    if charging_power <= 0 or battery_capacity <= 0:
        raise ValueError("battery_capacity and charging_power must be positive numbers.")
    if target_charge < current_charge:
        raise ValueError("target_charge must be greater than or equal to current_charge.")

    energy_needed = battery_capacity * max(target_charge - current_charge, 0) / 100
    if energy_needed <= 0:
        return {
            "start_time": now.isoformat(),
            "end_time": now.isoformat(),
            "expected_carbon": round(current_carbon_intensity, 2),
            "carbon_savings_percent": 0.0,
            "co2_saved_kg": 0.0,
            "energy_needed_kwh": 0.0,
            "duration_hours": 0.0,
            "message": "Battery already meets the target charge.",
        }

    duration_hours = energy_needed / charging_power
    interval_hours = _forecast_interval_hours(forecast)
    required_slots = max(1, math.ceil(duration_hours / interval_hours))

    if not forecast:
        end_time = now + timedelta(hours=duration_hours)
        return {
            "start_time": now.isoformat(),
            "end_time": end_time.isoformat(),
            "expected_carbon": round(current_carbon_intensity, 2),
            "carbon_savings_percent": 0.0,
            "co2_saved_kg": 0.0,
            "energy_needed_kwh": round(energy_needed, 2),
            "duration_hours": round(duration_hours, 2),
            "message": "Forecast unavailable, recommending immediate charging.",
        }

    baseline_values = [point["carbon_intensity"] for point in forecast[:required_slots]]
    if len(baseline_values) < required_slots:
        baseline_values.extend([current_carbon_intensity] * (required_slots - len(baseline_values)))
    baseline_average = sum(baseline_values) / len(baseline_values)

    urgency = max(0.0, min(1.0, urgency_factor))
    penalty_rate = max(5.0, baseline_average * 0.02)
    deadline = now + timedelta(hours=deadline_hours) if deadline_hours else None

    best_window = None
    best_score = None

    for start_index in range(0, max(1, len(forecast) - required_slots + 1)):
        window = forecast[start_index : start_index + required_slots]
        if len(window) < required_slots:
            continue

        start_time = window[0]["timestamp"]
        end_time = start_time + timedelta(hours=duration_hours)
        if deadline and end_time > deadline:
            continue

        average_carbon = sum(point["carbon_intensity"] for point in window) / len(window)
        delay_hours = max((start_time - now).total_seconds() / 3600, 0.0)
        score = average_carbon + (delay_hours * urgency * penalty_rate)

        if best_score is None or score < best_score:
            best_score = score
            best_window = (window, average_carbon, start_time, end_time)

    constrained = False
    if best_window is None:
        constrained = True
        earliest = forecast[:required_slots] if len(forecast) >= required_slots else forecast
        if not earliest:
            earliest = [{"timestamp": now, "carbon_intensity": current_carbon_intensity}]
        start_time = earliest[0]["timestamp"]
        average_carbon = sum(point["carbon_intensity"] for point in earliest) / len(earliest)
        end_time = start_time + timedelta(hours=duration_hours)
        best_window = (earliest, average_carbon, start_time, end_time)

    _, best_average, start_time, end_time = best_window
    carbon_savings_percent = max(0.0, ((baseline_average - best_average) / baseline_average) * 100) if baseline_average else 0.0
    co2_saved_kg = max(0.0, (baseline_average - best_average) * energy_needed / 1000)

    message = "Lowest-carbon charging window selected from the forecast."
    if urgency >= 0.7:
        message = "Urgency factor favored an earlier charging window."
    if constrained and deadline:
        message = "Deadline constraint forced the earliest feasible charging window."

    return {
        "start_time": start_time.isoformat(),
        "end_time": end_time.isoformat(),
        "expected_carbon": round(best_average, 2),
        "carbon_savings_percent": round(carbon_savings_percent, 2),
        "co2_saved_kg": round(co2_saved_kg, 3),
        "energy_needed_kwh": round(energy_needed, 2),
        "duration_hours": round(duration_hours, 2),
        "message": message,
    }


def _forecast_interval_hours(forecast: list[dict]) -> float:
    if len(forecast) < 2:
        return 1.0

    delta_hours = (forecast[1]["timestamp"] - forecast[0]["timestamp"]).total_seconds() / 3600
    return max(delta_hours, 1 / 60)
