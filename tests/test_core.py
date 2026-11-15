import numpy as np
import pytest
from astropy.coordinates import SkyCoord
from astropy.time import Time

from spherex_viewer import align, archive, measure, synthetic, targets
from spherex_viewer.frames import sort_chronologically

CENTER = SkyCoord(269.4, 4.75, unit="deg")


def test_parse_coords_formats():
    a = targets.parse_coords("08h55m10.831s", "-7d14m42.53s")
    b = targets.parse_coords("133.795129", "-7.245147")
    c = targets.parse_coords("08:55:10.831", "-07:14:42.53")
    assert a.separation(b).arcsec < 0.1 and a.separation(c).arcsec < 1e-3
    with pytest.raises(ValueError):
        targets.parse_coords("abc", "def")


def test_barnard_moves_north_about_10p4_arcsec_per_year():
    t = targets.TARGETS["Barnard's Star"]
    c0, c1 = t.coord_at(Time(2025.0, format="jyear")), t.coord_at(Time(2026.0, format="jyear"))
    assert 10.2 < c0.separation(c1).arcsec < 10.5
    assert (c1.dec - c0.dec).arcsec > 10.3  # almost purely northward


def test_wise0855_brief_coordinates():
    t = targets.TARGETS["WISE 0855-0714"]
    assert abs(t.ra_deg - 133.7951292) < 1e-6 and abs(t.dec_deg + 7.2451472) < 1e-6
    assert (
        t.coord_at(Time(2026.2, format="jyear")).separation(t.coord_ref()).arcsec > 100
    )  # ~8"/yr * 16 yr


def test_wise0855_matches_the_wiseview_link_position_in_2014():
    """Independent check of the epoch and motion: the challenge's WiseView link points at the 2014 NEOWISE position."""
    t = targets.TARGETS["WISE 0855-0714"]
    wiseview = SkyCoord(133.786245, -7.244372, unit="deg")
    assert t.coord_at(Time(2014.4, format="jyear")).separation(wiseview).arcsec < 2.0
    assert (
        t.coord_ref().separation(wiseview).arcsec > 30
    )  # without the motion it would be 5 pixels off


def test_epsilon_indi_b_has_a_published_position():
    t = targets.TARGETS["Epsilon Indi B (Ba/Bb)"]
    assert t.has_position and t.ref_epoch == 1999.666
    host_2000 = SkyCoord("22h03m21.65s", "-56d47m09.5s")  # Epsilon Indi A, about 6.7 arcmin away
    assert 6.4 < t.coord_at(Time(2000.0, format="jyear")).separation(host_2000).arcmin < 7.0


def test_archive_helpers():
    q = archive.build_query(133.795, -7.245, 0.1, ["SPHEREx-D5", "SPHEREx-D6"])
    assert (
        "spherex.artifact" in q
        and "'SPHEREx-D5', 'SPHEREx-D6'" in q
        and "center=133.79500000,-7.24500000d&size=0.100000" in q
    )
    u1 = "https://irsa.ipac.caltech.edu/ibe/data/spherex/qr2/level2/2025W19_2B/l2b-v20-2025-247/2/level2_2025W19_2B_0346_1D2_spx_l2b-v20-2025-247.fits?center=1,2d&size=0.1"
    u2 = u1.replace("v20-2025-247", "v25-2026-100")
    o = [
        archive.parse_observation(u1, 60000.5, "SPHEREx-D2"),
        archive.parse_observation(u2, 60000.5, "SPHEREx-D2"),
    ]
    assert o[0].obs_key == "2025W19_2B_0346_1D2" and o[0].version == 20
    d = archive.dedupe_versions(o)
    assert len(d) == 1 and d[0].version == 25
    many = [archive.Observation(f"u{i}", 60000 + i, "b", f"k{i}", 1) for i in range(100)]
    th = archive.thin_evenly(many, 10)
    assert len(th) == 10 and th[0].mjd == 60000 and th[-1].mjd == 60099


def test_common_grid_is_centred_and_roundtrips():
    w, shape = align.common_wcs(CENTER, 300, 6.15)
    assert shape[0] % 2 == 1
    x, y = w.world_to_pixel(CENTER)
    assert abs(x - (shape[1] - 1) / 2) < 1e-6 and abs(y - (shape[0] - 1) / 2) < 1e-6
    e, n = align.pix_to_offset(
        x + 3, y + 2, shape, 6.15
    )  # +3 px in x = 3 px WEST; +2 px in y = north
    assert abs(e + 3 * 6.15) < 1e-6 and abs(n - 2 * 6.15) < 1e-6
    px, py = align.offset_to_pix(e, n, shape, 6.15)
    assert abs(px - (x + 3)) < 1e-9 and abs(py - (y + 2)) < 1e-9


def test_chronological_sort():
    frames, _, _ = synthetic.make_synthetic(CENTER, n_frames=12)
    rng = np.random.default_rng(0)
    shuffled = list(rng.permutation(np.array(frames, dtype=object)))
    out = sort_chronologically(shuffled)
    assert [f.mjd for f in out] == sorted(f.mjd for f in frames)


def test_alignment_static_star_stays_put():
    # One isolated source at the field centre, random roll angle in every frame. After reprojection its
    # fitted centroid must sit on the grid-centre pixel to ~0.01 px: this validates the WCS/pixel conventions.
    # (Blending with close neighbours is a separate, real effect: see README "Known limitations".)
    from dataclasses import replace

    frames, truth, _ = synthetic.make_synthetic(CENTER, n_frames=12, pointing_error_pix=0.0)
    out = []
    for f, wt in zip(frames, truth["true_wcs"]):
        yy, xx = np.mgrid[0:41, 0:41]
        x, y = wt.world_to_pixel(CENTER)
        img = 500 * np.exp(-((xx - x) ** 2 + (yy - y) ** 2) / (2 * 1.1**2)) + 40
        out.append(replace(f, data=img.astype(np.float32), wcs=wt))
    wcs_out, shape = align.common_wcs(CENTER, 250, 6.15)
    cube = align.reproject_stack(out, wcs_out, shape)
    c = (shape[0] - 1) / 2
    for im in cube:
        fit = measure.fit_gaussian(im, c, c)
        assert fit and fit["ok"]
        assert np.hypot(fit["x"] - c, fit["y"] - c) < 0.02


def test_a_search_that_never_answers_is_abandoned(monkeypatch):
    import time

    monkeypatch.setattr(archive, "SEARCH_DEADLINE_S", 0.2)
    monkeypatch.setattr(archive, "_query_tap", lambda *a: time.sleep(5))
    monkeypatch.setattr(
        archive,
        "_query_sia",
        lambda *a: (_ for _ in ()).throw(RuntimeError("502 Proxy Error\nlong page")),
    )
    started = time.time()
    with pytest.raises(RuntimeError, match="TAP: no answer after 0 seconds.*SIA: 502 Proxy Error"):
        archive.query_observations(1, 2, 0.1, ["SPHEREx-D2"])
    assert time.time() - started < 2
    one = archive.Observation("u", 60000.0, "SPHEREx-D2", "k", 1)
    monkeypatch.setattr(archive, "_query_sia", lambda *a: [one])
    assert archive.query_observations(1, 2, 0.1, ["SPHEREx-D2"]) == [
        one
    ]  # the second route rescues it
