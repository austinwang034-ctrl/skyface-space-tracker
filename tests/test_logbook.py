"""Offline tests for the observer's log."""

import pytest

from spherex_viewer import logbook

BARNARD_FIT = {"pmra_masyr": -790.0, "pmra_err": 40.0, "pmdec_masyr": 10340.0, "pmdec_err": 45.0}
COMMON = dict(
    name="Barnard's Star",
    n_frames=40,
    first_date="2025-05-01",
    last_date="2026-05-01",
    span_days=365.0,
    bands=["SPHEREx-D2"],
    n_solved=38,
    median_residual_arcsec=0.6,
    n_detected=37,
)


def test_motion_direction_and_size():
    north = logbook.Motion(0.0, 1000.0, 10.0, 10.0)
    assert north.heading == "north" and north.position_angle == 0.0 and north.total == 1000.0
    assert (
        logbook.Motion(-500.0, 0.0).heading == "west"
        and logbook.Motion(700, -700).heading == "south-east"
    )
    assert north.significance == pytest.approx(100.0)


def test_tangential_velocity_of_barnards_star():
    # 10.39 arcsec/yr at 546.98 mas parallax is the textbook 90 km/s.
    assert logbook.tangential_velocity(10393.0, 546.98) == pytest.approx(90.1, abs=0.2)
    assert logbook.tangential_velocity(100.0, 0.0) is None


def test_log_for_a_fast_mover_that_matches_its_catalogue_motion():
    log = logbook.write_log(
        **COMMON, fit=BARNARD_FIT, catalogue=logbook.Motion(-802.8, 10362.4), parallax_mas=546.98
    )
    text = " ".join(line["text"] for line in log["lines"])
    assert log["kind"] == "moving" and "toward the north" in log["verdict"]
    assert "The two agree" in text and "light-years" in text and "km/s" in text
    assert log["numbers"]["travelled_arcsec"] == pytest.approx(10.36, abs=0.1)
    assert [line["label"] for line in log["lines"]][:4] == [
        "Observed",
        "Alignment",
        "Detection",
        "Motion",
    ]


def test_log_flags_an_uncatalogued_mover_and_a_disagreement():
    log = logbook.write_log(**COMMON, fit=BARNARD_FIT, catalogue=None)
    assert "second look" in log["lines"][-1]["text"]
    log = logbook.write_log(**COMMON, fit=BARNARD_FIT, catalogue=logbook.Motion(-802.8, 5000.0))
    assert "differ" in " ".join(line["text"] for line in log["lines"])


def test_log_for_a_star_that_stays_put_gives_an_upper_limit():
    fit = {"pmra_masyr": 30.0, "pmra_err": 60.0, "pmdec_masyr": -20.0, "pmdec_err": 60.0}
    log = logbook.write_log(**{**COMMON, "name": "R Draconis"}, fit=fit, catalogue=None)
    assert log["kind"] == "still" and "stayed put" in log["verdict"]
    assert log["numbers"]["upper_limit_masyr"] == pytest.approx(180.0, rel=0.05)


def test_log_when_nothing_is_seen_or_the_record_is_too_short():
    unseen = logbook.write_log(**{**COMMON, "n_detected": 0}, fit=None, catalogue=None)
    assert unseen["kind"] == "unseen" and "not seen" in unseen["verdict"]
    short = logbook.write_log(**COMMON, fit=None, catalogue=None)
    assert short["kind"] == "short"
    demo = logbook.write_log(**COMMON, fit=None, catalogue=None, synthetic=True)
    assert "simulated demo images" in demo["lines"][0]["text"]
