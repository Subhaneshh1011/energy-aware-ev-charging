from backend.carbon_model import calculate_carbon_intensity, harmonize_generation_mix


def test_harmonize_generation_mix_maps_thermal_to_coal() -> None:
    result = harmonize_generation_mix({"thermal": 100.0, "hydro": 10.0})

    assert result["coal"] == 100.0
    assert result["hydro"] == 10.0
    assert result["gas"] == 0.0


def test_calculate_carbon_intensity_uses_weighted_average() -> None:
    generation = {
        "coal": 100.0,
        "gas": 50.0,
        "solar": 25.0,
        "wind": 25.0,
        "hydro": 25.0,
        "nuclear": 25.0,
    }

    result = calculate_carbon_intensity(generation)

    assert round(result, 2) == 395.45
