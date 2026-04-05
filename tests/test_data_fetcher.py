from __future__ import annotations

import json
from pathlib import Path

from backend.data_fetcher import combine_records, is_plausible_record, parse_merit_timeseries_payload


FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_parse_merit_timeseries_payload_extracts_latest_demand_snapshot() -> None:
    payload = load_fixture("demandmet1chartdata.json")

    record = parse_merit_timeseries_payload(payload, source="test-demand")

    assert record is not None
    assert record["demand"] == 214403.0
    assert record["timestamp"].isoformat() == "2026-04-04T20:27:32+05:30"
    assert record["raw_labels"]["demand"] == 214403.0


def test_parse_merit_timeseries_payload_extracts_latest_generation_snapshot() -> None:
    payload = load_fixture("demandmet2chartdata.json")

    record = parse_merit_timeseries_payload(payload, source="test-generation")

    assert record is not None
    assert record["generation"] == {
        "coal": 129011.0,
        "gas": 1948.0,
        "solar": 62734.0,
        "wind": 4744.0,
        "hydro": 9175.0,
        "nuclear": 6791.0,
    }
    assert record["timestamp"].isoformat() == "2026-04-04T20:27:32+05:30"


def test_combine_records_builds_plausible_live_snapshot() -> None:
    demand_record = parse_merit_timeseries_payload(load_fixture("demandmet1chartdata.json"), source="demand")
    generation_record = parse_merit_timeseries_payload(load_fixture("demandmet2chartdata.json"), source="generation")

    combined = combine_records([demand_record, generation_record], source="combined")

    assert combined is not None
    assert combined["demand"] == 214403.0
    assert combined["generation"]["coal"] == 129011.0
    assert is_plausible_record(combined)


def test_is_plausible_record_rejects_old_bad_shape() -> None:
    bad_record = {
        "demand": 8.0,
        "generation": {
            "coal": 1031398.0,
            "gas": 6.0,
            "solar": 24.0,
            "wind": 24.0,
            "hydro": 204.0,
            "nuclear": 64.0,
        },
    }

    assert not is_plausible_record(bad_record)
