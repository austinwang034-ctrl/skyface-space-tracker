"""Offline tests of the web API: the demo, saved objects, live requests and their failures."""

import time

import pytest
from fastapi.testclient import TestClient

import server
from spherex_viewer import tracker as trackers

c = TestClient(server.app)
PNG, GIF = b"\x89PNG\r\n\x1a\n", b"GIF"


def run(body, tries=400):
    """Start a job and wait for it to finish; returns its final status."""
    r = c.post("/api/jobs", json=body)
    assert r.status_code == 202, r.text
    job = r.json()
    for _ in range(tries):
        if job["state"] in ("done", "error"):
            return job
        time.sleep(0.1)
        job = c.get(f"/api/jobs/{job['id']}").json()
    raise AssertionError("job timed out")


@pytest.fixture(scope="module")
def demo_job():
    return run({"source": "demo"})["id"]


def test_config_lists_bands_and_the_five_test_objects():
    cfg = c.get("/api/config").json()
    assert cfg["version"] == server.APP_VERSION and "SPHEREx-D6" in cfg["bands"]
    assert [o["id"] for o in cfg["test_objects"]] == [
        "barnard",
        "wise0855",
        "epsindi",
        "rwaur",
        "rdra",
    ]
    assert all(o["ra"] is not None and o["saved"] is False for o in cfg["test_objects"])
    assert cfg["saved"] == [] and cfg["max_frames"] == server.MAX_FRAMES


def test_requests_are_validated():
    assert c.post("/api/jobs", json={"source": "auto"}).status_code == 422  # no coordinates
    assert c.post("/api/jobs", json={"ra": "abc", "dec": "def"}).status_code == 422
    assert c.post("/api/jobs", json={"ra": "1", "dec": "2", "bands": ["NOPE"]}).status_code == 422
    assert c.post("/api/jobs", json={"ra": "1", "dec": "2", "fov_arcmin": 99}).status_code == 422
    assert c.post("/api/jobs", json={"source": "bogus"}).status_code == 422
    assert c.post("/api/jobs", json={"saved_id": "nothing-here"}).status_code == 404
    assert c.get("/api/jobs/doesnotexist").status_code == 404
    assert c.get("/api/jobs/doesnotexist/tracker").status_code == 404


def test_demo_tracker_description(demo_job):
    mv = c.get(f"/api/jobs/{demo_job}/tracker").json()
    assert mv["origin"] == "demo" and mv["grid"]["nx"] == mv["grid"]["ny"] and mv["has_wavelengths"]
    assert [f["k"] for f in mv["frames"]] == list(range(len(mv["frames"])))
    assert mv["frames"][0]["mjd"] < mv["frames"][-1]["mjd"] and mv["bands"] == ["SPHEREx-D2"]
    assert mv["target"]["pmdec"] == 10300.0


def test_every_image_view_renders(demo_job):
    seen = set()
    for view in ("image", "minus_static", "minus_previous"):
        for window in (1, 3):
            r = c.get(f"/api/jobs/{demo_job}/frames/5.png", params={"view": view, "window": window})
            assert r.status_code == 200 and r.content[:8] == PNG
            seen.add(r.content)
    assert len(seen) == 6  # every combination really is a different picture
    assert (
        c.get(f"/api/jobs/{demo_job}/frames/0.png?view=minus_previous").content[:8] == PNG
    )  # first image
    assert c.get(f"/api/jobs/{demo_job}/frames/0.png?stretch=frame").status_code == 200
    assert c.get(f"/api/jobs/{demo_job}/frames/999.png").status_code == 404
    assert c.get(f"/api/jobs/{demo_job}/frames/-1.png").status_code == 404
    assert c.get(f"/api/jobs/{demo_job}/frames/5.png?view=nope").status_code == 422
    assert c.get(f"/api/jobs/{demo_job}/frames/5.png?window=99").status_code == 422
    assert c.get(f"/api/jobs/{demo_job}/tracker.gif").content[:3] == GIF


def test_measurements_log_and_spectrum(demo_job):
    track = c.get(f"/api/jobs/{demo_job}/track").json()
    assert track["fit"] and abs(track["fit"]["pmdec_masyr"] - 10300) < 300
    assert sum(1 for row in track["rows"] if row["ok"]) > 35
    assert "time_utc" in c.get(f"/api/jobs/{demo_job}/track.csv").text
    log = c.get(f"/api/jobs/{demo_job}/logbook").json()
    assert log["kind"] == "moving" and "north" in log["verdict"]
    assert {"Observed", "Alignment", "Detection", "Motion", "Catalogue"} <= {
        x["label"] for x in log["lines"]
    }
    spectrum = c.get(f"/api/jobs/{demo_job}/spectrum?east=0&north=0").json()
    assert len(spectrum["rows"]) == len(track["rows"]) and abs(spectrum["dec"] - 4.74) < 0.01
    assert c.get(f"/api/jobs/{demo_job}/spectrum?east=99999&north=0").status_code == 422
    assert c.get(f"/api/jobs/{demo_job}/spectrum?east=0").status_code == 422


def test_compare_epochs(demo_job):
    meta = c.get(f"/api/jobs/{demo_job}/compare/meta?split=0.5").json()
    assert meta["n_early"] + meta["n_late"] == 40 and meta["early_to"] <= meta["late_from"]
    for part in ("early", "late", "diff"):
        assert c.get(f"/api/jobs/{demo_job}/compare/{part}.png?split=0.3").content[:8] == PNG
    assert c.get(f"/api/jobs/{demo_job}/compare/diff.png?band=SPHEREx-D2").content[:8] == PNG
    assert c.get(f"/api/jobs/{demo_job}/compare/diff.png?band=SPHEREx-D6").status_code == 422
    assert c.get(f"/api/jobs/{demo_job}/compare/meta?split=5").status_code == 422


def test_gaia_overlay_links_health_and_pages(demo_job):
    stars = c.get(f"/api/jobs/{demo_job}/overlay/gaia").json()
    assert stars["catalogue"] == "Gaia DR3" and len(stars["sources"]) > 5
    links = c.get("/api/links", params={"ra": 133.786245, "dec": -7.244372}).json()
    assert any("wiseview" in link["url"] for link in links["links"]) and len(links["credits"]) >= 4
    assert c.get("/api/health").json()["ok"]
    assert c.get("/").status_code == 200 and "Disallow: /api/" in c.get("/robots.txt").text


def test_identical_requests_share_one_job():
    first = c.post("/api/jobs", json={"source": "demo", "max_frames": 30}).json()
    assert (
        c.post("/api/jobs", json={"source": "demo", "max_frames": 30}).json()["id"] == first["id"]
    )


def test_saved_objects_open_by_id_by_position_and_mark_the_test_object(demo_tracker):
    meta = dict(demo_tracker.meta, saved_utc="2026-11-15 12:00")
    trackers.save(
        trackers.Tracker(meta, demo_tracker.cube, demo_tracker.wave), server.STORE.path("barnard")
    )
    cfg = c.get("/api/config").json()
    assert cfg["saved"][0]["id"] == "barnard" and cfg["test_objects"][0]["saved"] is True

    job = run({"saved_id": "barnard"})
    assert job["state"] == "done"
    mv = c.get(f"/api/jobs/{job['id']}/tracker").json()
    assert mv["origin"] == "saved" and mv["saved_utc"] == "2026-11-15 12:00"
    assert c.get(f"/api/jobs/{job['id']}/frames/0.png").content[:8] == PNG
    assert c.get(f"/api/jobs/{job['id']}/overlay/gaia").json()["sources"]  # stored with the images

    # Typing coordinates that land on the saved object opens the saved copy, with no archive call.
    target = demo_tracker.target
    by_position = run(
        {"ra": str(target.ra_deg), "dec": str(target.dec_deg), "epoch": target.ref_epoch}
    )
    assert by_position["state"] == "done"
    assert c.get(f"/api/jobs/{by_position['id']}/tracker").json()["origin"] == "saved"


def test_live_request_builds_from_the_archive_and_follows_looked_up_motion(
    monkeypatch, demo_tracker
):
    seen = {}

    def fake_build(target, fov, bands, frames, say):
        seen.update(target=target, fov=fov, bands=bands, frames=frames)
        say("Downloading images… 1/1", 0.5)
        return demo_tracker

    monkeypatch.setattr(trackers, "build_live", fake_build)
    job = run({"name": "Vega", "ra": "279.2347", "dec": "38.7837", "pmra": 200.9, "pmdec": 286.2,
               "fov_arcmin": 6, "bands": ["SPHEREx-D3"], "max_frames": 300})  # fmt: skip
    assert job["state"] == "done"
    assert (
        seen["target"].name == "Vega" and seen["target"].pmra == 200.9 and seen["target"].has_motion
    )
    assert (
        seen["bands"] == ["SPHEREx-D3"] and seen["fov"] == 6 and seen["frames"] == server.MAX_FRAMES
    )

    run({"ra": "269.452075", "dec": "4.693361"})  # Barnard's Star at its J2000 place
    assert seen["target"].name == "Barnard's Star" and seen["target"].pmdec > 10000
    assert seen["bands"] == ["SPHEREx-D2"]  # the object's usual detector when none is chosen

    run({"ra": "10.5", "dec": "-20.25"})
    assert seen["target"].name == "Your patch of sky" and not seen["target"].has_motion


def test_archive_failures_become_a_readable_job_error(monkeypatch):
    def down(*a, **k):
        raise RuntimeError("IRSA search failed (TAP: 502 Server Error | SIA: 502 Proxy Error)")

    monkeypatch.setattr(trackers, "build_live", down)
    job = run({"ra": "50.1", "dec": "12.3"})
    assert job["state"] == "error" and "IRSA search failed" in job["error"]
    assert c.get(f"/api/jobs/{job['id']}/tracker").status_code == 409

    def crash(*a, **k):
        raise ConnectionResetError("peer reset")

    monkeypatch.setattr(trackers, "build_live", crash)
    job = run({"ra": "50.2", "dec": "12.3"})
    assert job["state"] == "error" and "ConnectionResetError" in job["error"]


def test_live_ignores_the_saved_copy_when_asked(monkeypatch, demo_tracker):
    trackers.save(demo_tracker, server.STORE.path("barnard"))
    called = []
    monkeypatch.setattr(trackers, "build_live", lambda *a: called.append(1) or demo_tracker)
    target = demo_tracker.target
    body = {"ra": str(target.ra_deg), "dec": str(target.dec_deg), "epoch": target.ref_epoch}
    run({**body, "source": "auto"})
    assert called == []  # answered from the saved copy
    run({**body, "source": "live"})
    assert called == [1]


def test_blank_pixels_are_filled_for_display_only(demo_tracker):
    import io

    import numpy as np
    from PIL import Image

    holed = demo_tracker.cube.copy()
    holed[5, 30:34, 30:34] = np.nan  # a patch of flagged pixels in one image
    trackers.save(
        trackers.Tracker(demo_tracker.meta, holed, demo_tracker.wave), server.STORE.path("holed")
    )
    job = run({"saved_id": "holed"})["id"]
    for view in ("image", "minus_static", "minus_previous"):
        png = c.get(f"/api/jobs/{job}/frames/5.png", params={"view": view}).content
        rgba = np.array(Image.open(io.BytesIO(png)))[::-1]  # back to North-down array order
        assert (rgba[30:34, 30:34, 3] == 255).all(), view  # no transparent hole in the picture
    assert c.get(f"/api/jobs/{job}/tracker.gif").content[:3] == GIF
    assert np.isnan(server.JOBS[job].tracker.cube[5, 31, 31])  # the data itself is untouched


def test_a_saturated_star_core_is_drawn_white_not_as_a_hole(demo_tracker):
    import io

    import numpy as np
    from PIL import Image

    cored = demo_tracker.cube.copy()
    cored[:, 38:41, 38:41] = np.nan  # blank in every image, like the core of a too-bright star
    trackers.save(
        trackers.Tracker(demo_tracker.meta, cored, demo_tracker.wave), server.STORE.path("cored")
    )
    job = run({"saved_id": "cored"})["id"]
    rgba = np.array(Image.open(io.BytesIO(c.get(f"/api/jobs/{job}/frames/2.png").content)))[::-1]
    assert (rgba[38:41, 38:41, :3] == 255).all() and (rgba[38:41, 38:41, 3] == 255).all()
    diff = np.array(
        Image.open(io.BytesIO(c.get(f"/api/jobs/{job}/frames/2.png?view=minus_static").content))
    )[::-1]
    assert (diff[38:41, 38:41, 3] == 255).all()
    assert c.get(f"/api/jobs/{job}/tracker.gif").content[:3] == GIF
