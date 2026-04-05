from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup
from dateutil import parser as date_parser
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from backend.carbon_model import harmonize_generation_mix


LOGGER = logging.getLogger(__name__)
INDIA_TZ = ZoneInfo("Asia/Kolkata")
DASHBOARD_URL = "https://npp.gov.in/dashBoard/gc-map-dashboard-meritchart"
DEFAULT_TIMEOUT = 45
BASE_DIR = Path(__file__).resolve().parent.parent
APP_DATA_DIR = Path(os.getenv("APP_DATA_DIR", BASE_DIR / "backend" / "data"))
CACHE_FILE = APP_DATA_DIR / "cache" / "latest_grid_data.json"

LABEL_GROUPS = {
    "demand": ("demand", "total demand", "load", "total load", "requirement", "peak demand", "demand met"),
    "coal": ("coal", "thermal", "thermal generation", "lignite", "coal generation"),
    "gas": ("gas", "natural gas", "gas generation"),
    "hydro": ("hydro", "hydel", "hydro generation"),
    "solar": ("solar", "solar generation"),
    "wind": ("wind", "wind generation"),
    "nuclear": ("nuclear", "nuclear generation"),
}

PREFERRED_ENDPOINTS = [
    "https://npp.gov.in/dashBoard/demandmet1chartdata",
    "https://npp.gov.in/dashBoard/demandmet2chartdata",
]

LABEL_KEYS = ("name", "label", "type", "fuel", "category", "source")
VALUE_KEYS = ("value", "y", "mw", "amount", "generation", "demand", "load", "total")
MIN_DEMAND_MW = 1000.0
MAX_SOURCE_MW = 600000.0
MAX_TOTAL_GENERATION_MW = 1200000.0


class DataFetchError(RuntimeError):
    pass


async def fetch_grid_data() -> dict[str, Any]:
    errors: list[str] = []

    for strategy in (_fetch_via_endpoint_discovery, _fetch_via_playwright):
        try:
            record = await strategy()
            save_cached_grid_data(record)
            return record
        except Exception as exc:
            LOGGER.exception("Grid data strategy failed: %s", strategy.__name__)
            errors.append(f"{strategy.__name__}: {exc}")

    cached = load_cached_grid_data()
    if cached:
        cached["stale"] = True
        cached["message"] = "Live fetch failed, serving the latest cached reading."
        cached["errors"] = errors
        return cached

    raise DataFetchError("Unable to fetch live or cached grid data.")


async def _fetch_via_endpoint_discovery() -> dict[str, Any]:
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=DEFAULT_TIMEOUT,
        trust_env=False,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
            )
        },
    ) as client:
        response = await client.get(DASHBOARD_URL)
        response.raise_for_status()
        html = response.text

        candidates = [*PREFERRED_ENDPOINTS, *(await _discover_candidate_endpoints(client, html))]
        fragments: list[dict[str, Any]] = []
        for candidate in candidates:
            try:
                fragment = await _request_candidate_record(client, candidate)
                if fragment:
                    fragments.append(fragment)
                    combined = combine_records(fragments, source="api:combined")
                    if combined:
                        return combined
            except Exception:
                LOGGER.debug("Candidate endpoint failed: %s", candidate, exc_info=True)

    raise DataFetchError("No usable network payload discovered from the dashboard page.")


async def _discover_candidate_endpoints(client: httpx.AsyncClient, html: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    inline_sources = [script.get_text(" ", strip=True) for script in soup.find_all("script")]

    script_urls = []
    for script in soup.find_all("script", src=True):
        script_url = urljoin(DASHBOARD_URL, script["src"])
        script_urls.append(script_url)

    linked_script_text = []
    for script_url in script_urls[:8]:
        try:
            script_response = await client.get(script_url)
            if script_response.is_success and "javascript" in script_response.headers.get("content-type", ""):
                linked_script_text.append(script_response.text)
        except Exception:
            LOGGER.debug("Unable to load script asset: %s", script_url, exc_info=True)

    patterns = [
        r"""fetch\(\s*["']([^"']+)["']""",
        r"""url\s*:\s*["']([^"']+)["']""",
        r"""["'](\/[^"']*(?:api|json|dashboard|merit|map|data)[^"']*)["']""",
        r"""["'](https?://[^"']+)["']""",
    ]

    discovered = set()
    for blob in [html, *inline_sources, *linked_script_text]:
        for pattern in patterns:
            for match in re.findall(pattern, blob, flags=re.IGNORECASE):
                if not _looks_like_data_endpoint(match):
                    continue
                discovered.add(urljoin(DASHBOARD_URL, match))

    ranked = sorted(discovered, key=_endpoint_priority)
    return ranked[:20]


def _looks_like_data_endpoint(candidate: str) -> bool:
    parsed = urlparse(urljoin(DASHBOARD_URL, candidate))
    if parsed.scheme not in {"http", "https"}:
        return False
    text = parsed.path.lower()
    return any(token in text for token in ("api", "json", "dashboard", "merit", "data", "map"))


def _endpoint_priority(endpoint: str) -> tuple[int, int]:
    lower = endpoint.lower()
    score = 0
    if "api" in lower:
        score -= 5
    if "json" in lower:
        score -= 3
    if "dashboard" in lower:
        score -= 2
    return (score, len(endpoint))


async def _request_candidate_record(client: httpx.AsyncClient, endpoint: str) -> dict[str, Any] | None:
    response = await client.get(endpoint)
    response.raise_for_status()
    content_type = response.headers.get("content-type", "").lower()
    if "json" in content_type:
        payload = response.json()
        explicit = parse_merit_timeseries_payload(payload, source=f"api:{endpoint}")
        if explicit:
            return explicit
        return normalize_payload(payload, source=f"api:{endpoint}")

    text = response.text.strip()
    if text.startswith("{") or text.startswith("["):
        payload = json.loads(text)
        explicit = parse_merit_timeseries_payload(payload, source=f"api:{endpoint}")
        if explicit:
            return explicit
        return normalize_payload(payload, source=f"api:{endpoint}")

    parsed_html = parse_dom_content(html=response.text, body_text=_html_to_text(response.text), source=f"html:{endpoint}")
    if parsed_html:
        return parsed_html

    parsed_script = parse_script_payload(text, source=f"script:{endpoint}")
    if parsed_script:
        return parsed_script

    return None


async def _fetch_via_playwright() -> dict[str, Any]:
    captured_payloads: list[dict[str, Any]] = []
    response_tasks: list[asyncio.Task] = []

    async def capture_response(response) -> None:
        try:
            if response.request.resource_type not in {"xhr", "fetch"}:
                return
            content_type = response.headers.get("content-type", "").lower()
            if "json" not in content_type:
                return
            payload = await response.json()
            captured_payloads.append({"url": response.url, "payload": payload})
        except Exception:
            LOGGER.debug("Unable to capture response payload: %s", response.url, exc_info=True)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        page.on("response", lambda response: response_tasks.append(asyncio.create_task(capture_response(response))))

        try:
            await page.goto(DASHBOARD_URL, wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT * 1000)
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except PlaywrightTimeoutError:
                LOGGER.debug("Network idle timed out; continuing with partially loaded page.")

            await page.wait_for_timeout(3000)
            if response_tasks:
                await asyncio.gather(*response_tasks, return_exceptions=True)

            for payload in captured_payloads:
                normalized = normalize_payload(payload["payload"], source=f"browser-xhr:{payload['url']}")
                if normalized:
                    return normalized

            html = await page.content()
            body_text = await page.locator("body").inner_text()
            scraped = parse_dom_content(html=html, body_text=body_text, source="playwright-dom")
            if scraped:
                return scraped
        finally:
            with contextlib.suppress(Exception):
                await browser.close()

    raise DataFetchError("Playwright scraping did not find usable data.")


def normalize_payload(payload: Any, source: str) -> dict[str, Any] | None:
    timestamp = extract_timestamp(payload)
    labeled_values = extract_structured_metrics(payload)
    if not labeled_values:
        return None

    generation = {source_name: 0.0 for source_name in ("coal", "gas", "hydro", "solar", "wind", "nuclear")}
    demand = 0.0

    for label, value in labeled_values.items():
        canonical = _canonical_metric_name(label)
        if canonical == "demand":
            demand = max(demand, value)
        elif canonical in generation:
            generation[canonical] += value

    generation = harmonize_generation_mix(generation)
    total_generation = sum(generation.values())

    if total_generation <= 0:
        return None

    if demand <= 0:
        demand = total_generation

    record = {
        "demand": round(demand, 2),
        "generation": {key: round(value, 2) for key, value in generation.items()},
        "timestamp": timestamp,
        "source": source,
        "stale": False,
        "raw_labels": {key: round(value, 2) for key, value in labeled_values.items()},
    }
    if not _is_plausible_record(record):
        LOGGER.warning("Discarding implausible grid reading from %s: %s", source, record)
        return None
    return record


def parse_merit_timeseries_payload(payload: Any, source: str) -> dict[str, Any] | None:
    if not isinstance(payload, list) or not payload:
        return None
    if not all(isinstance(item, dict) for item in payload):
        return None
    required = {"updated_on", "name_of_data", "value_of_data"}
    if not all(required.issubset(item.keys()) for item in payload):
        return None

    latest_timestamp = max(
        _parse_datetime(item.get("updated_on")) or datetime.min.replace(tzinfo=INDIA_TZ)
        for item in payload
    )
    if latest_timestamp == datetime.min.replace(tzinfo=INDIA_TZ):
        return None

    labeled_values: dict[str, float] = {}
    for item in payload:
        item_timestamp = _parse_datetime(item.get("updated_on"))
        if item_timestamp != latest_timestamp:
            continue
        canonical = _canonical_metric_name(str(item.get("name_of_data", "")))
        value = _safe_metric_number(item.get("value_of_data"))
        if canonical and value is not None:
            labeled_values[canonical] = max(labeled_values.get(canonical, 0.0), value)

    if not labeled_values:
        return None

    generation = {source_name: 0.0 for source_name in ("coal", "gas", "hydro", "solar", "wind", "nuclear")}
    demand = 0.0
    for label, value in labeled_values.items():
        if label == "demand":
            demand = max(demand, value)
        elif label in generation:
            generation[label] = max(generation[label], value)

    total_generation = sum(generation.values())
    if demand <= 0 and total_generation <= 0:
        return None

    return {
        "demand": round(demand, 2),
        "generation": {key: round(value, 2) for key, value in generation.items()},
        "timestamp": latest_timestamp,
        "source": source,
        "stale": False,
        "raw_labels": {key: round(value, 2) for key, value in labeled_values.items()},
        "raw_series_points": len(payload),
    }


def extract_structured_metrics(payload: Any) -> dict[str, float]:
    metrics: dict[str, float] = {}
    _walk_structured_metrics(payload, metrics)
    return metrics


def _walk_structured_metrics(payload: Any, metrics: dict[str, float]) -> None:
    if isinstance(payload, dict):
        handled_here = False

        for key, value in payload.items():
            canonical = _canonical_metric_name(str(key))
            number = _safe_metric_number(value)
            if canonical and number is not None:
                metrics[canonical] = max(metrics.get(canonical, 0.0), number)
                handled_here = True

        label_key = next((key for key in payload if str(key).strip().lower() in LABEL_KEYS), None)
        value_key = next((key for key in payload if str(key).strip().lower() in VALUE_KEYS), None)
        if label_key and value_key:
            canonical = _canonical_metric_name(str(payload.get(label_key, "")))
            number = _safe_metric_number(payload.get(value_key))
            if canonical and number is not None:
                metrics[canonical] = max(metrics.get(canonical, 0.0), number)
                handled_here = True

        categories = payload.get("categories")
        series = payload.get("series")
        if isinstance(categories, list) and isinstance(series, list):
            for series_item in series:
                if not isinstance(series_item, dict):
                    continue
                data = series_item.get("data")
                if not isinstance(data, list):
                    continue
                for label, value in zip(categories, data):
                    canonical = _canonical_metric_name(str(label))
                    number = _safe_metric_number(value)
                    if canonical and number is not None:
                        metrics[canonical] = max(metrics.get(canonical, 0.0), number)
                        handled_here = True

        if handled_here:
            return

        for value in payload.values():
            if isinstance(value, (dict, list)):
                _walk_structured_metrics(value, metrics)
        return

    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, list) and len(item) == 2:
                canonical = _canonical_metric_name(str(item[0]))
                number = _safe_metric_number(item[1])
                if canonical and number is not None:
                    metrics[canonical] = max(metrics.get(canonical, 0.0), number)
                    continue
            _walk_structured_metrics(item, metrics)


def extract_timestamp(payload: Any) -> datetime:
    timestamp_candidates = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if any(token in str(key).lower() for token in ("time", "timestamp", "date", "updated")):
                    parsed = _parse_datetime(value)
                    if parsed:
                        timestamp_candidates.append(parsed)
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    if timestamp_candidates:
        return max(timestamp_candidates)
    return datetime.now(INDIA_TZ)


def parse_dom_content(html: str, body_text: str, source: str) -> dict[str, Any] | None:
    soup = BeautifulSoup(html, "lxml")
    labeled_values: dict[str, float] = {}

    for table in soup.find_all("table"):
        rows = []
        for row in table.find_all("tr"):
            cells = [cell.get_text(" ", strip=True) for cell in row.find_all(["th", "td"])]
            if len(cells) >= 2:
                rows.append(cells)

        for row in rows:
            canonical = _canonical_metric_name(row[0])
            value = _safe_metric_number(row[1])
            if canonical and value is not None:
                labeled_values[canonical] = max(labeled_values.get(canonical, 0.0), value)

    timestamp = _extract_timestamp_from_text(body_text) or datetime.now(INDIA_TZ)
    normalized = normalize_payload({"timestamp": timestamp.isoformat(), "values": labeled_values}, source=source)
    return normalized


def parse_script_payload(text: str, source: str) -> dict[str, Any] | None:
    labels_pattern = r"Demand|Load|Thermal|Coal|Gas|Hydro|Nuclear|Solar|Wind"
    patterns = [
        rf"""\[\s*["'](?P<label>{labels_pattern})["']\s*,\s*(?P<value>-?\d{{1,3}}(?:,\d{{3}})*(?:\.\d+)?|-?\d+(?:\.\d+)?)\s*\]""",
        rf"""(?:name|label)\s*[:=]\s*["'](?P<label>{labels_pattern})["'][^{{\[\]\n\r;]]{{0,80}}?(?:y|value|mw|amount|generation|demand|load|total)\s*[:=]\s*(?P<value>-?\d{{1,3}}(?:,\d{{3}})*(?:\.\d+)?|-?\d+(?:\.\d+)?)""",
    ]

    labeled_values: dict[str, float] = {}
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            canonical = _canonical_metric_name(match.group("label"))
            number = _safe_metric_number(match.group("value"))
            if canonical and number is not None:
                labeled_values[canonical] = max(labeled_values.get(canonical, 0.0), number)

    if not labeled_values:
        return None

    timestamp = _extract_timestamp_from_text(text) or datetime.now(INDIA_TZ)
    return normalize_payload({"timestamp": timestamp.isoformat(), "values": labeled_values}, source=source)


def combine_records(records: list[dict[str, Any]], source: str) -> dict[str, Any] | None:
    if not records:
        return None

    generation = {key: 0.0 for key in ("coal", "gas", "hydro", "solar", "wind", "nuclear")}
    demand = 0.0
    timestamps = []
    raw_labels: dict[str, float] = {}

    for record in records:
        if not record:
            continue
        demand = max(demand, float(record.get("demand", 0.0)))
        for key, value in record.get("generation", {}).items():
            generation[key] = max(generation.get(key, 0.0), float(value or 0.0))
        timestamp = record.get("timestamp")
        if isinstance(timestamp, datetime):
            timestamps.append(timestamp)
        for key, value in record.get("raw_labels", {}).items():
            raw_labels[key] = raw_labels.get(key, 0.0) + float(value or 0.0)

    generation = harmonize_generation_mix(generation)
    total = sum(generation.values())
    if total <= 0:
        return None
    if demand <= 0:
        demand = total

    return {
        "demand": round(demand, 2),
        "generation": {key: round(value, 2) for key, value in generation.items()},
        "timestamp": max(timestamps) if timestamps else datetime.now(INDIA_TZ),
        "source": source,
        "stale": False,
        "raw_labels": {key: round(value, 2) for key, value in raw_labels.items()},
    } if _is_plausible_record(
        {
            "demand": demand,
            "generation": generation,
        }
    ) else None


def load_cached_grid_data() -> dict[str, Any] | None:
    if not CACHE_FILE.exists():
        return None

    try:
        payload = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        payload["timestamp"] = datetime.fromisoformat(payload["timestamp"])
        if not is_plausible_record(payload):
            LOGGER.warning("Ignoring invalid cached grid data from %s", CACHE_FILE)
            return None
        return payload
    except Exception:
        LOGGER.exception("Failed to read cached grid data.")
        return None


def save_cached_grid_data(record: dict[str, Any]) -> None:
    if not is_plausible_record(record):
        LOGGER.warning("Skipping cache write for implausible grid data: %s", record)
        return
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    serializable = dict(record)
    timestamp = serializable["timestamp"]
    serializable["timestamp"] = timestamp.isoformat() if isinstance(timestamp, datetime) else str(timestamp)
    CACHE_FILE.write_text(json.dumps(serializable, indent=2), encoding="utf-8")


def _canonical_metric_name(label: str) -> str | None:
    lower_label = _normalize_label(label)
    for metric, synonyms in LABEL_GROUPS.items():
        if lower_label in synonyms:
            return metric
    return None


def _safe_metric_number(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None

    stripped = value.strip()
    if any(token in stripped for token in (":", "T", "/", "\\")):
        return None
    if not re.fullmatch(r"-?(?:\d{1,3}(?:,\d{3})*|\d+)(?:\.\d+)?", stripped):
        return None

    cleaned = stripped.replace(",", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(INDIA_TZ) if value.tzinfo else value.replace(tzinfo=INDIA_TZ)
    if isinstance(value, (int, float)):
        try:
            timestamp = float(value)
            if timestamp > 10_000_000_000:
                timestamp /= 1000
            return datetime.fromtimestamp(timestamp, tz=INDIA_TZ)
        except (ValueError, OSError, OverflowError):
            return None
    if not isinstance(value, str):
        return None

    candidate = value.strip()
    if not candidate:
        return None

    try:
        parsed = date_parser.parse(candidate, fuzzy=True)
    except (ValueError, TypeError, OverflowError):
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=INDIA_TZ)
    else:
        parsed = parsed.astimezone(INDIA_TZ)
    return parsed


def _extract_timestamp_from_text(text: str) -> datetime | None:
    patterns = [
        r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\s+\d{1,2}:\d{2}(?::\d{2})?\b",
        r"\b\d{1,2}\s+[A-Za-z]{3,9}\s+\d{2,4}\s+\d{1,2}:\d{2}(?::\d{2})?\b",
        r"\b[A-Za-z]{3,9}\s+\d{1,2},\s+\d{4}\s+\d{1,2}:\d{2}(?::\d{2})?\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        parsed = _parse_datetime(match.group(0))
        if parsed:
            return parsed
    return None


def _html_to_text(html: str) -> str:
    return BeautifulSoup(html, "lxml").get_text(" ", strip=True)


def _normalize_label(label: str) -> str:
    return re.sub(r"\s+", " ", label.strip().lower().replace("_", " ").replace("-", " ").replace(".", " ").rstrip(":"))


def _is_plausible_record(record: dict[str, Any]) -> bool:
    demand = float(record.get("demand", 0.0) or 0.0)
    generation = harmonize_generation_mix(record.get("generation"))
    total_generation = sum(generation.values())

    if demand < MIN_DEMAND_MW:
        return False
    if total_generation < MIN_DEMAND_MW:
        return False
    if total_generation > MAX_TOTAL_GENERATION_MW:
        return False
    if any(value < 0 or value > MAX_SOURCE_MW for value in generation.values()):
        return False

    if demand > 0:
        ratio = total_generation / demand
        if ratio < 0.4 or ratio > 1.6:
            return False

    return True


def is_plausible_record(record: dict[str, Any]) -> bool:
    return _is_plausible_record(record)
