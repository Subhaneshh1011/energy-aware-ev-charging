from __future__ import annotations

from collections.abc import Mapping


EMISSION_FACTORS = {
    "coal": 820.0,
    "gas": 490.0,
    "solar": 50.0,
    "wind": 20.0,
    "hydro": 10.0,
    "nuclear": 12.0,
}

CANONICAL_SOURCES = tuple(EMISSION_FACTORS.keys())


def harmonize_generation_mix(generation: Mapping[str, float] | None) -> dict[str, float]:
    mix = {source: 0.0 for source in CANONICAL_SOURCES}
    if not generation:
        return mix

    lower_generation = {str(key).strip().lower(): float(value or 0.0) for key, value in generation.items()}

    coal = lower_generation.get("coal", 0.0)
    gas = lower_generation.get("gas", 0.0)
    thermal = lower_generation.get("thermal", 0.0)

    if coal == 0.0 and thermal > 0.0:
        coal = thermal

    mix["coal"] = coal
    mix["gas"] = gas
    mix["solar"] = lower_generation.get("solar", 0.0)
    mix["wind"] = lower_generation.get("wind", 0.0)
    mix["hydro"] = lower_generation.get("hydro", 0.0)
    mix["nuclear"] = lower_generation.get("nuclear", 0.0)
    return mix


def total_generation(generation: Mapping[str, float] | None) -> float:
    return sum(harmonize_generation_mix(generation).values())


def calculate_carbon_intensity(generation: Mapping[str, float] | None) -> float:
    mix = harmonize_generation_mix(generation)
    total = sum(mix.values())
    if total <= 0:
        raise ValueError("Cannot calculate carbon intensity without generation data.")

    weighted_total = sum(mix[source] * EMISSION_FACTORS[source] for source in CANONICAL_SOURCES)
    return weighted_total / total
