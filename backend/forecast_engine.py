from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any


class ReadingStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._connection = sqlite3.connect(self.db_path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._initialize()

    def _initialize(self) -> None:
        with self._lock:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS grid_readings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    demand REAL NOT NULL,
                    generation_json TEXT NOT NULL,
                    carbon_intensity REAL NOT NULL,
                    source TEXT,
                    stale INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            self._connection.commit()

    def add_reading(self, reading: dict[str, Any]) -> None:
        timestamp = reading["timestamp"]
        if isinstance(timestamp, datetime):
            timestamp_value = timestamp.isoformat()
        else:
            timestamp_value = str(timestamp)

        with self._lock:
            self._connection.execute(
                """
                INSERT INTO grid_readings (timestamp, demand, generation_json, carbon_intensity, source, stale)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    timestamp_value,
                    float(reading["demand"]),
                    json.dumps(reading["generation"]),
                    float(reading["carbon_intensity"]),
                    reading.get("source"),
                    1 if reading.get("stale") else 0,
                ),
            )
            self._connection.commit()

    def get_recent_readings(self, limit: int = 288) -> list[dict[str, Any]]:
        with self._lock:
            cursor = self._connection.execute(
                """
                SELECT timestamp, demand, generation_json, carbon_intensity, source, stale
                FROM grid_readings
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            )
            rows = cursor.fetchall()

        readings = []
        for row in reversed(rows):
            readings.append(
                {
                    "timestamp": datetime.fromisoformat(row["timestamp"]),
                    "demand": float(row["demand"]),
                    "generation": json.loads(row["generation_json"]),
                    "carbon_intensity": float(row["carbon_intensity"]),
                    "source": row["source"],
                    "stale": bool(row["stale"]),
                }
            )
        return readings

    def prune(self, keep_latest: int = 1000) -> None:
        with self._lock:
            self._connection.execute(
                """
                DELETE FROM grid_readings
                WHERE id NOT IN (
                    SELECT id FROM grid_readings
                    ORDER BY id DESC
                    LIMIT ?
                )
                """,
                (keep_latest,),
            )
            self._connection.commit()


def generate_forecast(
    readings: Iterable[dict[str, Any]],
    horizon_hours: int = 12,
    step_minutes: int = 60,
) -> list[dict[str, Any]]:
    items = list(readings)
    if not items:
        return []

    items.sort(key=lambda item: item["timestamp"])
    latest = items[-1]
    latest_timestamp: datetime = latest["timestamp"]
    history = [float(item["carbon_intensity"]) for item in items]

    moving_window = history[-min(6, len(history)) :]
    moving_average = sum(moving_window) / len(moving_window)

    slopes = []
    recent = items[-min(6, len(items)) :]
    for previous, current in zip(recent, recent[1:]):
        delta_hours = max((current["timestamp"] - previous["timestamp"]).total_seconds() / 3600, 1 / 60)
        slopes.append((current["carbon_intensity"] - previous["carbon_intensity"]) / delta_hours)

    trend_per_hour = sum(slopes) / len(slopes) if slopes else 0.0
    damping = 0.35
    lower_bound = max(min(history) * 0.7, 10.0)
    upper_bound = max(history) * 1.3

    forecast = []
    for step in range(1, math.ceil(horizon_hours * 60 / step_minutes) + 1):
        hours_ahead = (step * step_minutes) / 60
        projected = moving_average + (trend_per_hour * hours_ahead * damping)
        projected = max(lower_bound, min(projected, upper_bound))
        forecast.append(
            {
                "timestamp": latest_timestamp + timedelta(minutes=step * step_minutes),
                "carbon_intensity": round(projected, 2),
            }
        )

    return forecast
