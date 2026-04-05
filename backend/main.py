from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend.carbon_model import calculate_carbon_intensity
from backend.data_fetcher import CACHE_FILE, fetch_grid_data, is_plausible_record, load_cached_grid_data
from backend.forecast_engine import ReadingStore, generate_forecast
from backend.optimizer import optimize_charging

if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())


BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"
APP_DATA_DIR = Path(os.getenv("APP_DATA_DIR", BASE_DIR / "backend" / "data"))
LOG_DIR = Path(os.getenv("APP_LOG_DIR", APP_DATA_DIR / "logs"))
LOG_FILE = LOG_DIR / "app.log"
DB_FILE = APP_DATA_DIR / "grid_readings.sqlite3"
REFRESH_SECONDS = 60
MIN_REFRESH_GAP_SECONDS = 25
SOURCE_STALE_AFTER_SECONDS = 120


def configure_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )


configure_logging()
LOGGER = logging.getLogger(__name__)
STORE = ReadingStore(DB_FILE)


async def refresh_grid_snapshot(app: FastAPI) -> dict[str, Any] | None:
    async with app.state.refresh_lock:
        if _was_refreshed_recently(app):
            return getattr(app.state, "latest_grid_data", None)

        app.state.last_refresh_started_at = datetime.now(timezone.utc)
        try:
            grid_data = await fetch_grid_data()
            carbon_intensity = calculate_carbon_intensity(grid_data["generation"])
            grid_data["carbon_intensity"] = round(carbon_intensity, 2)
            app.state.latest_grid_data = grid_data
            app.state.last_refresh_completed_at = datetime.now(timezone.utc)
            STORE.add_reading(grid_data)
            STORE.prune()
            LOGGER.info("Grid snapshot refreshed from %s", grid_data.get("source"))
            return grid_data
        except Exception:
            app.state.last_refresh_completed_at = datetime.now(timezone.utc)
            LOGGER.exception("Unable to refresh grid snapshot.")
            return getattr(app.state, "latest_grid_data", None)


async def refresh_loop(app: FastAPI) -> None:
    while True:
        await refresh_grid_snapshot(app)
        await asyncio.sleep(REFRESH_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.latest_grid_data = None
    app.state.refresh_lock = asyncio.Lock()
    app.state.last_refresh_started_at = None
    app.state.last_refresh_completed_at = None
    initial = await refresh_grid_snapshot(app)
    if not initial:
        LOGGER.warning("Application started without live data; cache will be used when possible.")

    task = asyncio.create_task(refresh_loop(app))
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


app = FastAPI(
    title="Carbon-Aware EV Charging System",
    description="India grid carbon intensity tracker and EV charging optimizer.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/frontend", StaticFiles(directory=FRONTEND_DIR), name="frontend")


@app.get("/", include_in_schema=False)
async def dashboard() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


async def get_latest_grid(app: FastAPI, refresh: bool = False) -> dict[str, Any]:
    latest = getattr(app.state, "latest_grid_data", None)
    if refresh or not latest or _snapshot_needs_refresh(app, latest):
        latest = await refresh_grid_snapshot(app)

    if latest:
        return _serialize_datetimes(latest)

    raise HTTPException(status_code=503, detail="No live or cached grid data is available.")


def get_valid_history(limit: int = 288) -> list[dict[str, Any]]:
    return [item for item in STORE.get_recent_readings(limit=limit) if is_plausible_record(item)]


@app.get("/grid-data")
async def grid_data(refresh: bool = False) -> dict[str, Any]:
    return await get_latest_grid(app, refresh=refresh)


@app.get("/carbon-intensity")
async def carbon_intensity(refresh: bool = False) -> dict[str, Any]:
    latest = await get_latest_grid(app, refresh=refresh)
    return {
        "timestamp": latest["timestamp"],
        "carbon_intensity": latest["carbon_intensity"],
        "generation": latest["generation"],
        "demand": latest["demand"],
        "total_generation": round(sum(latest["generation"].values()), 2),
        "source": latest.get("source"),
        "stale": latest.get("stale", False),
        "message": latest.get("message"),
    }


@app.get("/forecast")
async def forecast() -> dict[str, Any]:
    history = get_valid_history(limit=288)
    if not history:
        latest = await get_latest_grid(app)
        history = [
            {
                "timestamp": datetime.fromisoformat(latest["timestamp"]),
                "demand": latest["demand"],
                "generation": latest["generation"],
                "carbon_intensity": latest["carbon_intensity"],
                "source": latest.get("source"),
                "stale": latest.get("stale", False),
            }
        ]

    predicted = generate_forecast(history, horizon_hours=12, step_minutes=60)
    return {
        "history": _serialize_series(history),
        "forecast": _serialize_series(predicted),
    }


@app.get("/optimal-charging")
async def optimal_charging(
    battery_capacity: float = Query(..., gt=0),
    current_charge: float = Query(..., ge=0, le=100),
    target_charge: float = Query(..., ge=0, le=100),
    charging_power: float = Query(..., gt=0),
    urgency_factor: float = Query(0.0, ge=0, le=1),
    deadline_hours: float | None = Query(None, gt=0),
) -> dict[str, Any]:
    if target_charge < current_charge:
        raise HTTPException(status_code=400, detail="target_charge must be greater than current_charge.")

    latest = await get_latest_grid(app)
    history = get_valid_history(limit=288)
    if not history:
        history = [
            {
                "timestamp": datetime.fromisoformat(latest["timestamp"]),
                "carbon_intensity": latest["carbon_intensity"],
            }
        ]

    forecast_points = generate_forecast(history, horizon_hours=12, step_minutes=60)
    recommendation = optimize_charging(
        battery_capacity=battery_capacity,
        current_charge=current_charge,
        target_charge=target_charge,
        charging_power=charging_power,
        forecast=forecast_points,
        current_carbon_intensity=latest["carbon_intensity"],
        urgency_factor=urgency_factor,
        deadline_hours=deadline_hours,
        now=datetime.fromisoformat(latest["timestamp"]),
    )
    recommendation["current_carbon_intensity"] = latest["carbon_intensity"]
    recommendation["generated_at"] = latest["timestamp"]
    return recommendation


@app.get("/system-status")
async def system_status() -> dict[str, Any]:
    latest = getattr(app.state, "latest_grid_data", None)
    valid_history = get_valid_history(limit=288)
    cached = load_cached_grid_data()
    latest_timestamp = latest["timestamp"] if latest else None
    status = "unavailable"
    if latest:
        status = "cached" if latest.get("stale") else "live"
    elif cached:
        status = "cached"

    return {
        "status": status,
        "latest_timestamp": latest_timestamp.isoformat() if isinstance(latest_timestamp, datetime) else latest_timestamp,
        "valid_history_points": len(valid_history),
        "recommended_history_points": 12,
        "auto_refresh_seconds": REFRESH_SECONDS,
        "cache_available": cached is not None,
    }


@app.get("/debug/grid-status")
async def grid_status() -> dict[str, Any]:
    latest = getattr(app.state, "latest_grid_data", None)
    valid_history = get_valid_history(limit=288)
    cached = load_cached_grid_data()
    return {
        "has_live_state": latest is not None,
        "latest_state": _serialize_datetimes(latest) if latest else None,
        "cached_data_present": cached is not None,
        "cached_data": _serialize_datetimes(cached) if cached else None,
        "cache_file": str(CACHE_FILE),
        "cache_file_exists": CACHE_FILE.exists(),
        "valid_history_points": len(valid_history),
        "total_history_points": len(STORE.get_recent_readings(limit=288)),
    }


def _serialize_datetimes(payload: dict[str, Any]) -> dict[str, Any]:
    serialized = dict(payload)
    timestamp = serialized.get("timestamp")
    if isinstance(timestamp, datetime):
        serialized["timestamp"] = timestamp.isoformat()
    return serialized


def _serialize_series(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    serialized = []
    for point in points:
        item = dict(point)
        timestamp = item.get("timestamp")
        if isinstance(timestamp, datetime):
            item["timestamp"] = timestamp.isoformat()
        serialized.append(item)
    return serialized


def _was_refreshed_recently(app: FastAPI) -> bool:
    last_completed_at = getattr(app.state, "last_refresh_completed_at", None)
    if not last_completed_at:
        return False
    return (datetime.now(timezone.utc) - last_completed_at).total_seconds() < MIN_REFRESH_GAP_SECONDS


def _snapshot_needs_refresh(app: FastAPI, latest: dict[str, Any] | None) -> bool:
    if not latest:
        return True

    if latest.get("stale"):
        return True

    last_completed_at = getattr(app.state, "last_refresh_completed_at", None)
    if not last_completed_at:
        return True
    if (datetime.now(timezone.utc) - last_completed_at).total_seconds() >= REFRESH_SECONDS:
        return True

    latest_timestamp = latest.get("timestamp")
    if isinstance(latest_timestamp, str):
        latest_timestamp = datetime.fromisoformat(latest_timestamp)
    if isinstance(latest_timestamp, datetime):
        now = datetime.now(latest_timestamp.tzinfo or timezone.utc)
        if (now - latest_timestamp).total_seconds() >= SOURCE_STALE_AFTER_SECONDS:
            return True

    return False
