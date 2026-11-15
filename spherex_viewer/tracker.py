"""A tracker: every SPHEREx image of one patch of sky, lined up on a common grid, ready to play.

A tracker is plain data, so it can be built three ways and then used identically:

  live   searched and downloaded from NASA's IRSA archive when a visitor asks (`build_live`);
  saved  built once by save_images.py and kept as one file per object in the saved/ folder, so the
         site shows real images even while the archive is down (`save`, `load`, `Store`);
  demo   simulated images for trying the site with no internet (`build_demo`).

On disk a tracker is a single .npz file: the image cube, the wavelength of every pixel of every
image, and a small JSON description.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Callable

import astropy.units as u
import numpy as np
from astropy.coordinates import SkyCoord
from astropy.time import Time
from astropy.wcs import WCS
from reproject import reproject_interp

from . import archive
from .align import clean, common_wcs, grid_wcs, pix_to_offset
from .config import APP_VERSION, SAVED_DIR
from .frames import Frame, decode_flags, load_frame, sort_chronologically, usable
from .synthetic import make_synthetic
from .targets import Target

NOMINAL_EPOCH = (
    2026.2  # middle of the SPHEREx data now public: where a moving target's patch is centred
)
OVERSAMPLE = 2  # grid pixels per native SPHEREx pixel, per axis
MATCH_ARCSEC = 30.0  # a request this close to a saved object is answered from the saved copy


@dataclass
class Tracker:
    meta: dict  # JSON description: name, centre, grid scale, target, one entry per image
    cube: (
        np.ndarray
    )  # (N, ny, nx) float32, background-subtracted MJy/sr, NaN where there is no data
    wave: np.ndarray | None  # (N, ny, nx) wavelength in micron at every pixel, or None if unknown

    @property
    def frames(self) -> list[dict]:
        return self.meta["frames"]

    @property
    def shape(self) -> tuple[int, int]:
        return self.cube.shape[1:]

    @property
    def scale(self) -> float:
        return float(self.meta["scale_arcsec"])

    @property
    def psf_scale(self) -> float:
        """Grid pixels per native SPHEREx pixel."""
        return float(self.meta["native_pixel_arcsec"]) / self.scale

    @cached_property
    def center(self) -> SkyCoord:
        c = self.meta["center"]
        return SkyCoord(c["ra"] * u.deg, c["dec"] * u.deg, frame="icrs")

    @cached_property
    def wcs(self) -> WCS:
        return grid_wcs(self.center, self.scale, self.shape[0])

    @cached_property
    def target(self) -> Target:
        return target_from_dict(self.meta["target"])

    def band_group(self, k: int) -> list[int]:
        """Images from the same detector as image k, in time order (only these may be combined)."""
        band = self.frames[k]["band"]
        return [i for i, f in enumerate(self.frames) if f["band"] == band]

    def wavelength_at(self, k: int, x: float, y: float) -> float | None:
        """Wavelength (micron) image k saw at grid pixel (x, y)."""
        if self.wave is not None:
            i, j = int(round(y)), int(round(x))
            if 0 <= i < self.shape[0] and 0 <= j < self.shape[1]:
                value = float(self.wave[k, i, j])
                if np.isfinite(value) and value > 0:
                    return value
        return self.frames[k].get("wavelength_um")


# ------------------------------------------------------------------------------------ targets
def target_to_dict(t: Target) -> dict:
    return {
        "name": t.name,
        "ra": t.ra_deg,
        "dec": t.dec_deg,
        "pmra": t.pmra,
        "pmdec": t.pmdec,
        "parallax": t.parallax,
        "epoch": t.ref_epoch,
        "kind": t.kind,
        "source": t.source,
        "notes": t.notes,
    }


def target_from_dict(d: dict) -> Target:
    return Target(
        d["name"],
        d["ra"],
        d["dec"],
        pmra=d.get("pmra") or 0.0,
        pmdec=d.get("pmdec") or 0.0,
        parallax=d.get("parallax") or 0.0,
        ref_epoch=d.get("epoch") or 2000.0,
        kind=d.get("kind") or "custom",
        source=d.get("source") or "",
        notes=d.get("notes") or "",
    )


def position_at(t: Target, when: Time) -> SkyCoord:
    """Plain ICRS position of the target at a date (moved by its proper motion, if it has one)."""
    c = t.coord_at(when) if t.has_motion else t.coord_ref()
    return SkyCoord(c.ra, c.dec, frame="icrs")


def patch_center(t: Target) -> SkyCoord:
    """Where to centre the patch: for movers, where the target is in the middle of the SPHEREx data."""
    return position_at(t, Time(NOMINAL_EPOCH, format="jyear"))


# ------------------------------------------------------------------------------------ building
def _wavelength_image(frame: Frame) -> np.ndarray | None:
    """Wavelength at every native pixel. SPHEREx filters vary across the detector, so it is an image."""
    if frame.wave_wcs is None:
        return None
    ny, nx = frame.data.shape
    yy, xx = np.mgrid[0:ny, 0:nx]
    try:
        wave, _ = frame.wave_wcs.pixel_to_world(xx, yy)
        out = np.asarray(wave.to(u.um).value, dtype=np.float32)
        return out if out.shape == frame.data.shape and np.isfinite(out).any() else None
    except Exception:  # noqa: BLE001  (wavelength is a bonus: the image is still usable without it)
        return None


def from_frames(
    frames: list[Frame],
    target: Target,
    center: SkyCoord,
    fov_arcmin: float,
    origin: str,
    n_found: int | None = None,
    skipped: list[str] | None = None,
) -> Tracker:
    """Line a set of native images up on one North-up grid and describe each of them."""
    if not frames:
        raise RuntimeError("No usable images were retrieved.")
    frames = sort_chronologically(frames)
    native = float(np.mean([f.pixscale_arcsec for f in frames]))
    scale = native / OVERSAMPLE
    wcs_out, shape = common_wcs(center, fov_arcmin * 60.0, scale)
    cube = np.full((len(frames),) + shape, np.nan, dtype=np.float32)
    wave = np.full_like(cube, np.nan)
    entries = []
    for k, f in enumerate(frames):
        image, _ = clean(f)
        cube[k] = reproject_interp((image, f.wcs), wcs_out, shape_out=shape, return_footprint=False)
        wave_image = _wavelength_image(f)
        if wave_image is not None:
            wave[k] = reproject_interp(
                (wave_image, f.wcs), wcs_out, shape_out=shape, return_footprint=False
            )
        elif np.isfinite(f.wavelength_um):
            wave[k] = f.wavelength_um
        where = position_at(target, f.time)
        gx, gy = (float(v) for v in wcs_out.world_to_pixel(where))
        east, north = pix_to_offset(gx, gy, shape, scale)
        nx_, ny_ = (float(v) for v in f.wcs.world_to_pixel(where))
        flag = f.flag_at(nx_, ny_)
        middle = float(wave[k, shape[0] // 2, shape[1] // 2])
        if not np.isfinite(middle):
            middle = float(f.wavelength_um)
        flagged = f.flagged_fraction()
        entries.append(
            {
                "mjd": float(f.mjd),
                "time_utc": f.time.iso,
                "band": f.band,
                "wavelength_um": round(middle, 4) if np.isfinite(middle) else None,
                "obs_key": f.obs.obs_key,
                "release": f.obs.release,
                "version": f.obs.version,
                "valid_pct": round(float(100 * np.isfinite(cube[k]).mean()), 1),
                "masked_pct": None if flagged is None else round(100 * flagged, 2),
                "flags": decode_flags(flag) if flag else [],
                "pred": [round(float(east), 3), round(float(north), 3)],
            }
        )
    meta = {
        "name": target.name,
        "origin": origin,
        "app_version": APP_VERSION,
        "center": {"ra": float(center.ra.deg), "dec": float(center.dec.deg)},
        "scale_arcsec": float(scale),
        "native_pixel_arcsec": native,
        "fov_arcmin": float(fov_arcmin),
        "bands": sorted({f.band for f in frames}),
        "target": target_to_dict(target),
        "frames": entries,
        "n_found": n_found if n_found is not None else len(frames),
        "skipped": (skipped or [])[:20],
        "n_skipped": len(skipped or []),
        "gaia": None,
    }
    return Tracker(meta, cube, wave if np.isfinite(wave).any() else None)


def build_live(
    target: Target,
    fov_arcmin: float,
    bands: list[str],
    max_frames: int = 60,
    progress: Callable[[str, float], None] | None = None,
) -> Tracker:
    """Search IRSA for every SPHEREx image of the target's patch, download cutouts, line them up."""
    say = progress or (lambda message, fraction: None)
    center = patch_center(target)
    say("Searching NASA's SPHEREx archive…", 0.05)
    # Ask for 1.5x the displayed field so rotated cutouts still fill the whole square grid.
    found = archive.query_observations(
        center.ra.deg, center.dec.deg, 1.5 * fov_arcmin / 60.0, bands
    )
    if not found:
        raise RuntimeError("No SPHEREx images cover this position in the selected bands (yet).")
    wanted = archive.thin_evenly(found, max_frames)
    say(f"Downloading {len(wanted)} of {len(found)} images…", 0.15)
    ok, failed = archive.fetch_all(
        wanted, progress=lambda i, n: say(f"Downloading images… {i}/{n}", 0.15 + 0.6 * i / n)
    )
    frames, skipped = [], [message for _, message in failed]
    for obs, path in ok:
        try:
            frame = load_frame(obs, path, center)
        except Exception as exc:  # noqa: BLE001
            path.unlink(missing_ok=True)  # corrupt download: drop it so a retry fetches it again
            skipped.append(f"unreadable file: {exc}")
            continue
        if usable(frame):
            frames.append(frame)
        else:
            skipped.append("mostly empty image (target near the detector edge)")
    say("Lining the images up…", 0.85)
    return from_frames(frames, target, center, fov_arcmin, "live", len(found), skipped)


def build_demo(n_frames: int = 40) -> Tracker:
    """Simulated images of a Barnard-like star drifting north. Clearly labelled as not real."""
    center = SkyCoord(269.4486 * u.deg, 4.7399 * u.deg, frame="icrs")
    frames, truth, gaia = make_synthetic(center, n_frames=n_frames, pointing_error_pix=0.0)
    target = Target(
        "Simulated star (Barnard-like)",
        truth["mover_ra"][0],
        truth["mover_dec"][0],
        pmdec=10300.0,
        ref_epoch=frames[0].time.jyear,
        kind="mover",
        source="SIMULATED. Not SPHEREx data.",
    )
    tracker = from_frames(frames, target, center, 4.0, "demo")
    from .overlays import gaia_overlay

    mid_year = float(Time(tracker.frames[len(frames) // 2]["mjd"], format="mjd").jyear)
    tracker.meta["gaia"] = gaia_overlay(gaia, center, mid_year, overlay_radius(tracker))
    return tracker


def overlay_radius(tracker: Tracker) -> float:
    """Radius (arcsec) that reaches the corners of the square patch."""
    return 0.75 * tracker.shape[0] * tracker.scale


# ------------------------------------------------------------------------------------ saving
def save(tracker: Tracker, path: Path) -> Path:
    """Write a tracker as one compressed file. Wavelengths are stored at half precision
    (about 0.002 micron), far finer than SPHEREx's spectral resolution."""
    path.parent.mkdir(parents=True, exist_ok=True)
    arrays = {"cube": tracker.cube.astype(np.float32), "meta": np.array(json.dumps(tracker.meta))}
    if tracker.wave is not None:
        arrays["wave"] = tracker.wave.astype(np.float16)
    tmp = path.with_suffix(".part.npz")
    np.savez_compressed(tmp, **arrays)
    tmp.replace(path)
    return path


def read_meta(path: Path) -> dict:
    with np.load(path, allow_pickle=False) as data:
        return json.loads(str(data["meta"]))


def load(path: Path) -> Tracker:
    with np.load(path, allow_pickle=False) as data:
        meta = json.loads(str(data["meta"]))
        cube = np.asarray(data["cube"], dtype=np.float32)
        wave = np.asarray(data["wave"], dtype=np.float32) if "wave" in data.files else None
    if cube.ndim != 3 or len(meta["frames"]) != len(cube):
        raise ValueError(f"{path.name} is not a valid saved tracker.")
    meta["origin"] = "saved"
    return Tracker(meta, cube, wave)


class Store:
    """The saved/ folder: one .npz per object, found by id or by sky position."""

    def __init__(self, folder: Path = SAVED_DIR):
        self.folder = Path(folder)
        self._index: dict[str, tuple[float, dict]] = {}  # id -> (file time, description)
        self._lock = threading.Lock()

    def path(self, saved_id: str) -> Path:
        if not saved_id or not all(ch.isalnum() or ch in "-_" for ch in saved_id):
            raise KeyError(saved_id)
        return self.folder / f"{saved_id}.npz"

    def index(self) -> dict[str, dict]:
        """Description of every saved object (re-read only when a file changes)."""
        with self._lock:
            seen = {}
            for file in sorted(self.folder.glob("*.npz")) if self.folder.is_dir() else []:
                if file.name.endswith(".part.npz"):
                    continue
                stamp = file.stat().st_mtime
                cached = self._index.get(file.stem)
                if cached is None or cached[0] != stamp:
                    try:
                        cached = (stamp, read_meta(file))
                    except Exception:  # noqa: BLE001  (a damaged file is skipped, not fatal)
                        continue
                seen[file.stem] = cached
            self._index = seen
            return {saved_id: meta for saved_id, (_, meta) in seen.items()}

    def summaries(self) -> list[dict]:
        out = []
        for saved_id, meta in self.index().items():
            frames = meta["frames"]
            out.append(
                {
                    "id": saved_id,
                    "name": meta["name"],
                    "n_frames": len(frames),
                    "bands": meta.get("bands", []),
                    "first": frames[0]["time_utc"][:10],
                    "last": frames[-1]["time_utc"][:10],
                    "saved_utc": meta.get("saved_utc"),
                }
            )
        return out

    def match(self, coord: SkyCoord, epoch: float) -> str | None:
        """Id of the saved object that sits at `coord` in year `epoch`, allowing for its motion."""
        when = Time(epoch, format="jyear")
        best, best_sep = None, MATCH_ARCSEC
        for saved_id, meta in self.index().items():
            sep = position_at(target_from_dict(meta["target"]), when).separation(coord).arcsec
            if sep <= best_sep:
                best, best_sep = saved_id, sep
        return best

    def load(self, saved_id: str) -> Tracker:
        path = self.path(saved_id)
        if not path.exists():
            raise KeyError(saved_id)
        return load(path)
