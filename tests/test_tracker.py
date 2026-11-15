"""Offline tests for trackers: building, measuring, saving, loading and finding them."""

import numpy as np
import pytest
from astropy.coordinates import SkyCoord
from astropy.time import Time

from spherex_viewer import measure, overlays, render, synthetic, targets
from spherex_viewer import tracker as trackers

CENTER = SkyCoord(133.7862, -7.2444, unit="deg")


def test_demo_tracker_is_complete_and_in_time_order(demo_tracker):
    t = demo_tracker
    n = len(t.frames)
    assert t.cube.shape == (n, *t.shape) and t.shape[0] == t.shape[1] and t.shape[0] % 2 == 1
    assert t.meta["origin"] == "demo" and t.psf_scale == pytest.approx(2.0)
    mjd = [f["mjd"] for f in t.frames]
    assert mjd == sorted(mjd)
    for f in t.frames:
        assert {
            "time_utc",
            "band",
            "wavelength_um",
            "release",
            "valid_pct",
            "flags",
            "pred",
        } <= set(f)
        assert len(f["pred"]) == 2 and 1.0 < f["wavelength_um"] < 1.7
    assert t.wave is not None and t.wave.shape == t.cube.shape
    assert t.meta["gaia"]["sources"]  # the demo carries its own star catalogue


def test_the_moving_star_is_found_and_its_motion_recovered(demo_tracker):
    track = measure.measure_track(demo_tracker)
    assert track.ok.mean() > 0.9
    found = track[track.ok]
    assert found.offset_arcsec.median() < 1.0  # found where predicted, well under a 6.15" pixel
    fit = measure.fit_linear_motion(track)
    assert fit and abs(fit["pmdec_masyr"] - 10300) / 10300 < 0.03 and abs(fit["pmra_masyr"]) < 300
    # the predicted path runs north by about 10.3" per year over ~1.1 years
    north = [f["pred"][1] for f in demo_tracker.frames]
    assert 10 < north[-1] - north[0] < 13


def test_spectrum_at_a_spot_and_off_the_edge(demo_tracker):
    centre = measure.spectrum_at(demo_tracker, 0.0, 0.0)
    assert len(centre) == len(demo_tracker.frames) and centre.ok.sum() > 30
    assert centre.wavelength_um.between(1.0, 1.7).all()
    off = measure.spectrum_at(demo_tracker, 5000.0, 0.0)
    assert not off.ok.any() and off.flux_mjy.isna().all()


def test_a_static_star_keeps_its_place_and_brightness_through_the_grid():
    """One isolated star at the centre, a random roll in every image: after lining up, its
    measured place must stay on the centre and its brightness must stay constant."""
    from dataclasses import replace

    frames, truth, _ = synthetic.make_synthetic(CENTER, n_frames=10, pointing_error_pix=0.0)
    yy, xx = np.mgrid[0:41, 0:41]
    made = []
    for f, wcs in zip(frames, truth["true_wcs"], strict=True):
        x, y = wcs.world_to_pixel(CENTER)
        image = 500 * np.exp(-((xx - x) ** 2 + (yy - y) ** 2) / (2 * 1.1**2)) + 40
        made.append(replace(f, data=image.astype(np.float32), wcs=wcs))
    star = targets.Target("still", float(CENTER.ra.deg), float(CENTER.dec.deg))
    t = trackers.from_frames(made, star, CENTER, 3.0, "demo")
    track = measure.measure_track(t)
    assert track.ok.all() and track.offset_arcsec.max() < 0.3
    assert track.flux_mjy.std() / track.flux_mjy.mean() < 0.03


def test_save_and_load_round_trip(demo_tracker, tmp_path):
    path = trackers.save(demo_tracker, tmp_path / "saved" / "demo.npz")
    assert path.exists() and not list(path.parent.glob("*.part*"))
    back = trackers.load(path)
    assert back.meta["origin"] == "saved" and back.frames == demo_tracker.frames
    assert np.array_equal(back.cube, demo_tracker.cube, equal_nan=True)
    assert np.nanmax(np.abs(back.wave - demo_tracker.wave)) < 0.003  # half-precision wavelengths
    again = measure.fit_linear_motion(measure.measure_track(back))
    assert abs(again["pmdec_masyr"] - 10300) / 10300 < 0.03
    assert trackers.read_meta(path)["name"] == demo_tracker.meta["name"]


def test_store_lists_finds_and_guards(demo_tracker, tmp_path):
    store = trackers.Store(tmp_path / "saved")
    assert store.index() == {} and store.summaries() == []  # a missing folder is simply empty
    trackers.save(demo_tracker, store.path("demo"))
    (store.folder / "broken.npz").write_bytes(b"not a real file")
    summary = store.summaries()
    assert [s["id"] for s in summary] == ["demo"] and summary[0]["n_frames"] == len(
        demo_tracker.frames
    )

    target = demo_tracker.target
    now = trackers.position_at(target, Time(2026.4, format="jyear"))
    assert store.match(now, 2026.4) == "demo"
    assert store.match(now, 1990.0) is None  # the star was 6 arcminutes away back then
    assert store.match(SkyCoord(10, 10, unit="deg"), 2026.4) is None
    assert store.load("demo").meta["origin"] == "saved"
    for bad in ("../etc/passwd", "", "a b", "missing"):
        with pytest.raises(KeyError):
            store.load(bad)


def test_images_without_wavelength_information_still_build():
    frames, _, _ = synthetic.make_synthetic(CENTER, n_frames=4, pointing_error_pix=0.0)
    from dataclasses import replace

    frames = [replace(f, wavelength_um=float("nan")) for f in frames]
    star = targets.Target("x", float(CENTER.ra.deg), float(CENTER.dec.deg))
    t = trackers.from_frames(frames, star, CENTER, 3.0, "demo")
    assert t.wave is None and t.frames[0]["wavelength_um"] is None
    assert t.wavelength_at(0, 5, 5) is None
    assert len(measure.spectrum_at(t, 0, 0)) == 4
    with pytest.raises(RuntimeError):
        trackers.from_frames([], star, CENTER, 3.0, "demo")


def test_live_build_uses_the_archive_and_reports_what_it_skipped(monkeypatch, tmp_path):
    from spherex_viewer import archive

    frames, _, _ = synthetic.make_synthetic(CENTER, n_frames=6, pointing_error_pix=0.0)
    by_url = {f.obs.url: f for f in frames}
    monkeypatch.setattr(archive, "query_observations", lambda *a: [f.obs for f in frames])
    monkeypatch.setattr(
        archive,
        "fetch_all",
        lambda obs, progress=None: (
            [(o, tmp_path / "x") for o in obs[:-1]],
            [(obs[-1], "timed out")],
        ),
    )

    def fake_load(obs, path, center):
        if obs is frames[0].obs:
            raise OSError("truncated file")
        return by_url[obs.url]

    monkeypatch.setattr(trackers, "load_frame", fake_load)
    said = []
    star = targets.Target("x", float(CENTER.ra.deg), float(CENTER.dec.deg))
    t = trackers.build_live(star, 3.0, ["SPHEREx-D2"], 60, lambda m, f: said.append(f))
    assert len(t.frames) == 4 and t.meta["origin"] == "live" and t.meta["n_found"] == 6
    assert t.meta["n_skipped"] == 2 and said == sorted(said) and said[-1] < 1

    monkeypatch.setattr(archive, "query_observations", lambda *a: [])
    with pytest.raises(RuntimeError, match="No SPHEREx images"):
        trackers.build_live(star, 3.0, ["SPHEREx-D2"])


def test_window_and_static_sky():
    cube = np.arange(6, dtype=float)[:, None, None] * np.ones((6, 2, 2))
    group = [0, 2, 4]  # images 1, 3, 5 belong to another detector
    assert render.windowed(cube, group, 2, 1)[0, 0] == 2
    assert render.windowed(cube, group, 2, 3)[0, 0] == pytest.approx(2.0)  # mean of 0, 2, 4
    assert render.windowed(cube, group, 0, 3)[0, 0] == pytest.approx(1.0)  # edge: mean of 0, 2
    sky = np.random.default_rng(3).normal(10, 1, (9, 9))
    stack = np.repeat(sky[None], 7, axis=0)
    stack[3, 4, 4] += 500  # a visitor in one image
    assert (stack[3] - render.static_sky(stack, list(range(7))))[4, 4] == pytest.approx(500)
    assert render.limits(np.full((3, 3), np.nan)) == (0.0, 1.0)


def test_gaia_overlay_gives_offsets_and_yearly_motion():
    _, _, stars = synthetic.make_synthetic(CENTER, n_frames=3)
    out = overlays.gaia_overlay(stars, CENTER, 2026.0, radius_arcsec=200.0)
    assert out["sources"] and all(np.hypot(s["e"], s["n"]) <= 200 for s in out["sources"])
    mover = next(s for s in out["sources"] if s["id"] == "999")
    assert mover["pm_n"] == pytest.approx(10.3) and mover["pm_e"] == 0.0
