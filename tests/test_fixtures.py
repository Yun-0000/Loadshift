from pathlib import Path

from loadshift.fixtures import HORIZON_STEPS, load_forecast_day, load_provenance
from loadshift.paths import FIXTURES_DIR, REPO_ROOT


def test_provenance_names_public_sources_and_no_secrets():
    provenance = load_provenance()
    sources = provenance["sources"]
    assert "open-meteo" in sources["weather"]["url"].lower()
    snapshots = sources["weather"].get("snapshots") or [sources["weather"]["snapshot"]]
    assert snapshots
    for snap in snapshots:
        assert Path(REPO_ROOT / snap).is_file()
    assert sources["weather"]["live_secrets"] == "none — public archive, no API key"
    assert "pvwatts" in sources["pv"]["reference"].lower()
    assert "openei" in sources["load"]["references"][1]
    assert "pge.com" in sources["prices"]["reference"]
    blob = (FIXTURES_DIR / "provenance.json").read_text().lower()
    for banned in ("api_key", "token", "password", "secret="):
        assert banned not in blob


def test_forecast_day_has_48_steps_and_midday_pv():
    day = load_forecast_day()
    frame = day.frame
    assert len(frame) == HORIZON_STEPS
    assert frame["pv_w"].max() > 3000
    noon = frame.between_time("11:00", "14:00")["pv_w"].mean()
    evening = frame.between_time("18:00", "21:00")["pv_w"].mean()
    assert noon > evening
    assert frame["load_w"].between_time("17:00", "20:00").mean() > frame["load_w"].between_time(
        "02:00", "05:00"
    ).mean()
    assert set(day.provenance["sources"]) == {"weather", "pv", "load", "prices"}


def test_derived_csv_matches_generator(tmp_path):
    from loadshift.fixtures import write_derived_csvs
    import pandas as pd

    # Write next to committed fixtures, then compare in-memory series to CSV.
    csv_path = FIXTURES_DIR / "day_ahead.csv"
    if not csv_path.exists():
        write_derived_csvs()
    written = pd.read_csv(csv_path, index_col="timestamp")
    generated = load_forecast_day().frame
    assert len(written) == len(generated)
    assert abs(written["pv_w"].max() - generated["pv_w"].max()) < 1e-6
