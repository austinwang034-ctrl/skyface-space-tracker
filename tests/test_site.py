"""The page, its script and stylesheet, and the image-saving script."""

import re

import pytest
from fastapi.testclient import TestClient

import save_images
import server
from spherex_viewer import tracker as trackers

c = TestClient(server.app)


def site():
    page, css, js = c.get("/"), c.get("/assets/styles.css"), c.get("/assets/app.js")
    assert page.status_code == css.status_code == js.status_code == 200
    return page.text, css.text, js.text


def test_every_element_the_script_uses_exists_in_the_page():
    html, _, js = site()
    ids = set(re.findall(r'id="([^"]+)"', html))
    used = set(re.findall(r'\$\("#([A-Za-z_][\w-]*)"\)', js))
    used -= {"sp_reset"}  # created by the script itself, inside the spectrum note
    assert used - ids == set(), f"app.js looks for ids the page lacks: {sorted(used - ids)}"
    assert len(ids) == len(re.findall(r'id="([^"]+)"', html)), "duplicate ids in the page"
    for prefix, names in (("tab-", server_tabs(js)), ("tabbtn-", server_tabs(js))):
        assert all(prefix + n in ids for n in names)


def server_tabs(js):
    return (
        re.search(r"const TABS = \[(.*?)\]", js)
        .group(1)
        .replace('"', "")
        .replace(" ", "")
        .split(",")
    )


def test_page_has_the_three_ways_in_and_the_tracker_controls():
    html, css, js = site()
    for needle in (
        "Welcome to Skyface Space Tracker", 'id="start_name"', 'id="start_ra"', 'id="start_dec"',
        'id="start_objects"', 'id="demo"', 'id="home"', "Track this patch", "Compare epochs",
        "Data quality", "Spectrum", "Observer’s log", 'id="v_view"', 'id="v_window"', 'id="c_gaia"',
        'id="blink_go"', 'id="c_neg"', "/assets/logo.svg", 'href="/license"', '<main id="main"',
        'class="skip-link"', 'role="alert"', 'role="tabpanel"', 'lang="en"',
    ):  # fmt: skip
        assert needle in html, needle
    for needle in (
        "/api/resolve",
        "saved_id",
        "minus_static",
        "/overlay/gaia",
        "trackerKey",
        "embed",
    ):
        assert needle in js or needle in html, needle
    assert "style=" not in html and "fonts.googleapis.com" not in html
    assert "aladin" not in html.lower()  # nothing on the page waits for an outside server
    assert not re.search(r"(^|[},\s])canvas\s*[{,]", css)
    for asset in ("fonts/atkinson-hyperlegible-next-latin-wght-normal.woff2", "vendor/plotly.min.js",
                  "logo.svg", "favicon.svg"):  # fmt: skip
        assert c.get(f"/assets/{asset}").status_code == 200, asset


def test_save_script_arguments():
    assert save_images.parse_bands(None) is None
    assert save_images.parse_bands("d2, D6") == ["SPHEREx-D2", "SPHEREx-D6"]
    assert len(save_images.parse_bands("all")) == 6
    with pytest.raises(SystemExit):
        save_images.parse_bands("D9")
    assert (
        save_images.slug("Proxima Centauri!") == "proxima-centauri"
        and save_images.slug("***") == "object"
    )

    class Args:
        name = ra = dec = only = id = None
        epoch = 2000.0

    assert [i for i, _ in save_images.wanted(Args())] == list(save_images.TEST_IDS)
    Args.only = "wise0855,rdra"
    picked = save_images.wanted(Args())
    assert [i for i, _ in picked] == ["wise0855", "rdra"] and picked[0][1].band == "SPHEREx-D6"
    Args.only = "pluto"
    with pytest.raises(SystemExit):
        save_images.wanted(Args())
    Args.only, Args.name = None, "Barnard's Star"
    assert save_images.wanted(Args())[0][0] == "barnard-s-star"
    Args.name, Args.ra, Args.dec, Args.id = None, "10.5", "-20.25", "My Patch"
    ((saved_id, target),) = save_images.wanted(Args())
    assert saved_id == "my-patch" and target.ra_deg == 10.5
    Args.dec = None
    with pytest.raises(SystemExit):
        save_images.wanted(Args())


def test_save_script_writes_a_file_the_site_can_open(monkeypatch, tmp_path, demo_tracker):
    monkeypatch.setattr(save_images, "SAVED_DIR", tmp_path)
    monkeypatch.setattr(save_images, "ATTEMPTS", 2)
    monkeypatch.setattr(save_images.time, "sleep", lambda s: None)
    calls = []

    def flaky_build(target, fov, bands, frames, progress=None):
        calls.append(bands)
        if len(calls) == 1:
            raise RuntimeError("IRSA search failed (502)")
        fresh = dict(demo_tracker.meta, gaia=None)
        return trackers.Tracker(fresh, demo_tracker.cube, demo_tracker.wave)

    monkeypatch.setattr(trackers, "build_live", flaky_build)
    monkeypatch.setattr(
        save_images.gaia, "gaia_cone", lambda *a: (_ for _ in ()).throw(OSError("offline"))
    )
    detail = save_images.save_one("barnard", demo_tracker.target, ["SPHEREx-D2"], 5.0, 40)
    assert len(calls) == 2 and "40 images" in detail and "no Gaia stars saved" in detail
    store = trackers.Store(tmp_path)
    saved = store.load("barnard")
    assert saved.meta["origin"] == "saved" and saved.meta["saved_utc"] and len(saved.frames) == 40

    monkeypatch.setattr(
        trackers, "build_live", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
    )
    with pytest.raises(RuntimeError, match="gave up after 2 attempts"):
        save_images.save_one("rdra", demo_tracker.target, ["SPHEREx-D2"], 5.0, 40)
