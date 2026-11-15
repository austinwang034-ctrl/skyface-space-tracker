"""Offline tests for SIMBAD failover: the Harvard mirror, then the built-in test objects."""

import pytest
from astropy.coordinates import SkyCoord
from astropy.time import Time
from fastapi.testclient import TestClient

import server
from spherex_viewer import config, targets

c = TestClient(server.app)


@pytest.fixture
def servers(monkeypatch):
    monkeypatch.setattr(config, "SIMBAD_SERVERS", ["main.example", "mirror.example"])
    monkeypatch.setattr(targets, "SIMBAD_SERVERS", config.SIMBAD_SERVERS)
    targets._FAILED.clear()
    return config.SIMBAD_SERVERS


def test_the_mirror_is_used_when_the_main_server_is_down_and_then_preferred(servers):
    asked = []

    def ask(server_name):
        asked.append(server_name)
        if server_name == "main.example":
            raise RuntimeError("404 Not Found\nlong explanation")
        return "answer"

    assert targets.simbad_try(ask) == "answer" and asked == ["main.example", "mirror.example"]
    assert servers[0] == "mirror.example"  # next time the working server is asked first
    asked.clear()
    assert targets.simbad_try(ask) == "answer" and asked == ["mirror.example"]


def test_unknown_names_are_an_answer_and_total_failure_names_every_server(servers):
    def unknown(server_name):
        raise LookupError("no such object")

    with pytest.raises(LookupError):
        targets.simbad_try(unknown)

    def down(server_name):
        raise RuntimeError("404 Not Found\nmore text")

    with pytest.raises(ConnectionError, match="main.example: 404 Not Found.*mirror.example"):
        targets.simbad_try(down)


@pytest.mark.parametrize(
    "typed,key",
    [
        ("Barnard's Star", "Barnard's Star"),
        ("barnard’s star", "Barnard's Star"),
        ("GJ 699", "Barnard's Star"),
        ("WISE 0855-0714", "WISE 0855-0714"),
        ("eps Ind Ba", "Epsilon Indi B (Ba/Bb)"),
        ("RW Aur", "RW Aurigae"),
        ("r draconis", "R Draconis"),
    ],
)
def test_test_objects_are_known_without_a_network(typed, key):
    assert targets.builtin_by_name(typed) is targets.TARGETS[key]
    assert targets.builtin_by_name("Vega") is None


def test_builtin_objects_are_recognised_by_position_allowing_for_motion():
    barnard = targets.TARGETS["Barnard's Star"]
    now = barnard.coord_at(Time(2026.5, format="jyear"))
    here = SkyCoord(now.ra, now.dec, frame="icrs")
    assert targets.builtin_near(here, 2026.5) is barnard
    assert targets.builtin_near(here, 2000.0) is None  # 270 arcsec from where it was in 2000
    assert targets.builtin_near(barnard.coord_ref(), 2000.0) is barnard
    assert targets.builtin_near(SkyCoord(10, 10, unit="deg"), 2000.0) is None


def test_name_lookup_survives_a_simbad_outage(monkeypatch):
    def down(name):
        raise ConnectionError("no SIMBAD server answered")

    monkeypatch.setattr(server, "_simbad_lookup", down)
    r = c.get("/api/resolve", params={"name": "Barnard's Star"}).json()
    assert abs(r["ra"] - 269.452075) < 1e-6 and r["pmdec"] > 10000 and "built-in" in r["source"]
    w = c.get("/api/resolve", params={"name": "WISE 0855-0714"}).json()
    assert w["epoch"] == 2010.34 and w["band"] == "SPHEREx-D6"  # the year its position is valid
    failed = c.get("/api/resolve", params={"name": "Vega"})
    assert failed.status_code == 502 and "five test objects" in failed.json()["detail"]


def test_other_names_come_from_simbad_and_unknown_names_say_so(monkeypatch):
    monkeypatch.setattr(
        server,
        "_simbad_lookup",
        lambda name: {"ra_deg": 279.23, "dec_deg": 38.78, "ref_epoch": 2000.0,
                      "pmra": 200.9, "pmdec": float("nan"), "parallax": 130.2},
    )  # fmt: skip
    r = c.get("/api/resolve", params={"name": "Vega"}).json()
    assert r["source"] == "SIMBAD" and r["pmra"] == 200.9 and r["pmdec"] is None

    def unknown(name):
        raise LookupError("no object named that")

    monkeypatch.setattr(server, "_simbad_lookup", unknown)
    assert c.get("/api/resolve", params={"name": "Not A Star"}).status_code == 404


def test_a_server_that_just_failed_is_not_asked_first_again(servers):
    asked = []

    def ask(server_name):
        asked.append(server_name)
        if server_name == "main.example":
            raise RuntimeError("down")
        return "ok"

    targets.simbad_try(ask)
    asked.clear()
    servers.insert(0, "main.example")  # even if it were back at the front of the list
    targets.simbad_try(ask)
    assert asked[0] == "mirror.example"


def test_self_check_reports_every_archive_even_when_they_fail():
    from spherex_viewer import diagnose

    class Reply:
        def __init__(self, ok):
            self.status_code, self.text = (
                (200, "capabilities uri hips VOTABLE Ceres") if ok else (404, "gone")
            )

    def get(url, params=None, timeout=0):
        if "simbad.cds" in url:
            raise TimeoutError("slow")
        return Reply("harvard" in url or "simbad" not in url)

    report = diagnose.run(get)
    assert (
        len(report["archives"]) == len(diagnose.CHECKS) and report["version"] == config.APP_VERSION
    )
    assert report["names_work"] is True and any("simbad.cds" in n for n in report["failed"])
    assert all({"name", "ok", "seconds", "detail"} <= set(r) for r in report["archives"])
