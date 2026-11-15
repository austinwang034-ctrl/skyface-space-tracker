"""Skyface Space Tracker: the web server.   Run:  uvicorn server:app --port 8000   then open http://localhost:8000

A visitor asks for a patch of sky. The server answers with a *tracker*: every SPHEREx image of
that patch, lined up and in time order. Trackers come from the saved/ folder when the object was
saved ahead of time (instant, and independent of NASA's archive being up), otherwise live from
NASA/IPAC IRSA. Building one can take a while, so it runs as a background job the page polls.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import numpy as np
from astropy.time import Time
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from spherex_viewer import crosscheck, diagnose, gaia, logbook, measure, overlays, protect, render
from spherex_viewer import tracker as trackers
from spherex_viewer.config import (
    ALLOWED_ORIGINS,
    APP_NAME,
    APP_OWNER,
    APP_VERSION,
    BANDS,
    FRAME_ANCESTORS,
    JOBS_PER_MINUTE,
    MAX_FRAMES,
    OWN_HOSTS,
    RELEASES,
    SPHEREX_ACKNOWLEDGEMENT,
)
from spherex_viewer.targets import (
    TARGETS,
    TEST_IDS,
    Target,
    _simbad_lookup,
    builtin_by_name,
    builtin_near,
    parse_coords,
)

ROOT = Path(__file__).parent
WEB = ROOT / "web"
MAX_JOBS, JOB_TTL_S = 12, 3600
STORE = trackers.Store()
JOB_LIMITER = protect.RateLimiter(JOBS_PER_MINUTE)
app = FastAPI(title=f"{APP_NAME} API", version=APP_VERSION)


@app.middleware("http")
async def guard(request: Request, call_next):
    """Refuse to serve as another website's back end, and forbid framing of the full page."""
    path = request.url.path
    ours = protect.own_hosts(request.headers, OWN_HOSTS)
    if path.startswith("/api/") and protect.is_cross_site(request.headers, ours, ALLOWED_ORIGINS):
        return JSONResponse(
            {"detail": f"This API serves {APP_NAME} only. Other sites may embed it with ?embed=1."},
            status_code=403,
        )
    response = await call_next(request)
    response.headers.update(protect.security_headers(path, request.url.query, FRAME_ANCESTORS))
    if path.startswith("/assets/vendor/") or path.startswith("/assets/fonts/"):
        response.headers["Cache-Control"] = "public, max-age=604800"
    return response


# Compress text on the way out: the chart library alone shrinks from 4.8 MB to about 1.4 MB.
app.add_middleware(GZipMiddleware, minimum_size=1024)


# ------------------------------------------------------------------------------------------ jobs
class JobRequest(BaseModel):
    # "auto": the saved copy if there is one, else live from NASA. "live": always ask NASA.
    source: Literal["auto", "live", "demo"] = "auto"
    saved_id: str | None = Field(None, max_length=40)  # open this saved object directly
    name: str | None = Field(None, max_length=80)
    ra: str | None = Field(None, max_length=40)  # degrees or sexagesimal
    dec: str | None = Field(None, max_length=40)
    epoch: float = Field(2000.0, ge=1900, le=2100)  # year the coordinates are valid
    pmra: float | None = Field(None, ge=-20000, le=20000)  # mas/yr, from a name lookup
    pmdec: float | None = Field(None, ge=-20000, le=20000)
    parallax: float | None = Field(None, ge=0, le=1000)
    fov_arcmin: float = Field(5.0, ge=2.0, le=15.0)
    bands: list[str] = []
    max_frames: int = Field(60, ge=10, le=300)


@dataclass
class Job:
    id: str
    key: str
    req: JobRequest
    state: str = "queued"  # queued | running | done | error
    progress: float = 0.0
    message: str = "Queued"
    error: str | None = None
    tracker: trackers.Tracker | None = None
    created: float = field(default_factory=time.time)
    cache: dict = field(default_factory=dict)  # display ranges, static-sky images, measurements
    # Re-entrant: computing one cached value may need another (the GIF needs the static sky).
    lock: threading.RLock = field(default_factory=threading.RLock)

    def public(self) -> dict:
        return {
            "id": self.id,
            "state": self.state,
            "progress": round(self.progress, 3),
            "message": self.message,
            "error": self.error,
        }

    def once(self, key, compute):
        """Compute-once storage for anything derived from the tracker."""
        with self.lock:
            if key not in self.cache:
                self.cache[key] = compute()
            return self.cache[key]


JOBS: "OrderedDict[str, Job]" = OrderedDict()
KEYS: dict[str, str] = {}
GLOCK = threading.Lock()
POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="job")


def requested_target(req: JobRequest) -> Target:
    """The object a request is about: a built-in test object if the coordinates are one of them,
    otherwise the coordinates as given (with the motion a name lookup supplied, if any)."""
    entered = parse_coords(req.ra or "", req.dec or "")
    known = builtin_near(entered, req.epoch)
    if known is not None:
        return known
    moving = bool(req.pmra or req.pmdec)
    return Target(
        (req.name or "").strip() or "Your patch of sky",
        float(entered.ra.deg),
        float(entered.dec.deg),
        pmra=req.pmra or 0.0,
        pmdec=req.pmdec or 0.0,
        parallax=req.parallax or 0.0,
        ref_epoch=req.epoch,
        kind="mover" if moving else "custom",
        source="Catalogue lookup by name."
        if moving
        else "Coordinates as entered (no motion assumed).",
    )


def run_job(job: Job) -> None:
    job.state = "running"

    def say(message: str, fraction: float) -> None:
        job.message, job.progress = message, float(fraction)

    try:
        req = job.req
        if req.source == "demo":
            say("Making simulated images…", 0.3)
            job.tracker = trackers.build_demo()
        elif req.saved_id:
            say("Opening the saved images…", 0.5)
            job.tracker = STORE.load(req.saved_id)
        else:
            target = requested_target(req)
            entered = parse_coords(req.ra or "", req.dec or "")
            saved_id = STORE.match(entered, req.epoch) if req.source == "auto" else None
            if saved_id:
                say("Opening the saved images…", 0.5)
                job.tracker = STORE.load(saved_id)
            else:
                bands = req.bands or [target.band]
                frames = min(req.max_frames, MAX_FRAMES)
                job.tracker = trackers.build_live(target, req.fov_arcmin, bands, frames, say)
        job.state, job.progress, job.message = "done", 1.0, "Done"
    except KeyError:
        job.state, job.error = "error", "That saved object is not on this site."
    except (ValueError, RuntimeError) as exc:
        job.state, job.error = "error", str(exc)
    except Exception as exc:  # noqa: BLE001  (network errors etc.: report, never crash the worker)
        job.state, job.error = "error", f"{type(exc).__name__}: {exc}"


def done_job(job_id: str) -> Job:
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "Unknown or expired job.")
    if job.state == "error":
        raise HTTPException(409, job.error or "Job failed.")
    if job.state != "done":
        raise HTTPException(409, "Job is still running.")
    return job


def records(df) -> list[dict]:
    return json.loads(df.to_json(orient="records"))  # NaN -> null, numpy types -> plain JSON


def get_track(job: Job):
    """Measured positions and brightness in every image, plus a straight-line motion fit."""

    def compute():
        table = measure.measure_track(job.tracker)
        fit = measure.fit_linear_motion(table)
        return table, ({k: float(v) for k, v in fit.items()} if fit else None)

    return job.once("track", compute)


# ------------------------------------------------------------------------------------------ set-up endpoints
def test_objects() -> list[dict]:
    saved = STORE.index()
    out = []
    for short, key in TEST_IDS.items():
        t = TARGETS[key]
        out.append(
            {
                "id": short,
                "name": t.name,
                "ra": round(t.ra_deg, 6),
                "dec": round(t.dec_deg, 6),
                "epoch": t.ref_epoch,
                "pmra": t.pmra,
                "pmdec": t.pmdec,
                "parallax": t.parallax,
                "band": t.band,
                "notes": t.notes,
                "saved": short in saved,
            }
        )
    return out


@app.get("/api/config")
def config():
    return {
        "name": APP_NAME,
        "version": APP_VERSION,
        "owner": APP_OWNER,
        "max_frames": MAX_FRAMES,
        "bands": {k: list(v) for k, v in BANDS.items()},
        "releases": RELEASES,
        "acknowledgement": SPHEREX_ACKNOWLEDGEMENT,
        "test_objects": test_objects(),
        "saved": STORE.summaries(),
    }


@app.get("/api/resolve")
def resolve(name: str = Query(..., min_length=2, max_length=80)):
    """Star name -> coordinates and motion. The built-in test objects answer with no network;
    everything else is asked of SIMBAD (its main server, then the Harvard mirror)."""
    known = builtin_by_name(name)
    if known is not None:
        return {
            "name": known.name,
            "ra": round(known.ra_deg, 6),
            "dec": round(known.dec_deg, 6),
            "epoch": known.ref_epoch,
            "pmra": known.pmra,
            "pmdec": known.pmdec,
            "parallax": known.parallax,
            "band": known.band,
            "source": "built-in catalogue",
        }

    def number(value):
        return float(value) if value is not None and np.isfinite(value) else None

    try:
        r = _simbad_lookup(name)
    except LookupError as exc:
        raise HTTPException(404, f"SIMBAD has no object with that name. ({exc})") from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            502,
            "The SIMBAD name catalogue cannot be reached right now, so only the five test objects "
            f"can be found by name. Enter coordinates instead. ({exc})",
        ) from exc
    return {
        "name": name.strip(),
        "ra": round(float(r["ra_deg"]), 6),
        "dec": round(float(r["dec_deg"]), 6),
        "epoch": r.get("ref_epoch", 2000.0),
        "pmra": number(r.get("pmra")),
        "pmdec": number(r.get("pmdec")),
        "parallax": number(r.get("parallax")),
        "band": None,
        "source": "SIMBAD",
    }


@app.post("/api/jobs", status_code=202)
def create_job(req: JobRequest, request: Request):
    visitor = request.client.host if request.client else "unknown"
    if not JOB_LIMITER.allow(visitor):
        raise HTTPException(429, "Too many trackers started at once. Wait a minute and try again.")
    if req.source != "demo":
        bad = [b for b in req.bands if b not in BANDS]
        if bad:
            raise HTTPException(422, f"Unknown detector bands: {', '.join(bad)}")
        if req.saved_id:
            if req.saved_id not in STORE.index():
                raise HTTPException(404, "That saved object is not on this site.")
        else:
            if not (req.ra and req.dec):
                raise HTTPException(422, "Enter both a right ascension and a declination.")
            try:
                parse_coords(req.ra, req.dec)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
    key = hashlib.sha1(req.model_dump_json().encode()).hexdigest()
    with GLOCK:
        old = JOBS.get(KEYS.get(key, ""))
        fresh = old and old.state != "error" and time.time() - old.created < JOB_TTL_S
        if fresh and req.source != "live":
            return old.public()  # identical request: reuse the result
        job = Job(id=uuid.uuid4().hex[:12], key=key, req=req)
        JOBS[job.id], KEYS[key] = job, job.id
        now = time.time()
        for jid in list(JOBS):  # bound memory: drop expired jobs, then the oldest finished ones
            other = JOBS[jid]
            finished = other.state in ("done", "error") and jid != job.id
            if finished and (now - other.created > JOB_TTL_S or len(JOBS) > MAX_JOBS):
                if KEYS.get(other.key) == jid:
                    del KEYS[other.key]
                del JOBS[jid]
    POOL.submit(run_job, job)
    return job.public()


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "Unknown or expired job.")
    return job.public()


# ------------------------------------------------------------------------------------------ tracker endpoints
@app.get("/api/jobs/{job_id}/tracker")
def tracker_info(job_id: str):
    """Everything the page needs to play the tracker (the images themselves come as PNGs)."""
    t = done_job(job_id).tracker
    meta = t.meta
    return {
        "name": meta["name"],
        "origin": meta["origin"],  # saved | live | demo
        "saved_utc": meta.get("saved_utc"),
        "center": meta["center"],
        "grid": {"nx": t.shape[1], "ny": t.shape[0], "scale_arcsec": t.scale},
        "native_pixel_arcsec": meta["native_pixel_arcsec"],
        "fov_arcmin": meta.get("fov_arcmin"),
        "bands": meta.get("bands", []),
        "n_found": meta.get("n_found"),
        "n_skipped": meta.get("n_skipped", 0),
        "skipped": meta.get("skipped", []),
        "has_wavelengths": t.wave is not None,
        "target": meta["target"],
        "frames": [{"k": k, **f} for k, f in enumerate(t.frames)],
    }


def frame_rgba(job: Job, k: int, stretch: str, view: str, window: int) -> np.ndarray:
    """One tracker image as RGBA.

    view "image": the image itself (grey). "minus_static": the image minus the median of every
    image from its detector, so only what changed is left. "minus_previous": the image minus the
    one before it. Differences are drawn red (brighter) and blue (fainter).
    """
    t = job.tracker
    group = t.band_group(k)
    image = render.windowed(t.cube, group, k, window)
    band = t.frames[k]["band"]
    static = job.once(("static", band), lambda: render.static_sky(t.cube, group))
    # Pixels the archive flagged as bad are blank in the data. For display only, they are filled
    # with that pixel's usual value so the picture has no holes; measurements never use them.
    blank = ~np.isfinite(image) & np.isfinite(static)
    if view == "image":
        image = np.where(blank, static, image)
        if stretch == "frame":
            lo, hi = render.limits(image)
        else:
            lo, hi = job.once("limits", lambda: render.limits(t.cube))
        # A star too bright for the detector has its core blanked in every exposure: draw it white.
        return render.gray_rgba(np.where(render.enclosed_blanks(image), hi, image), lo, hi)
    if view == "minus_static":
        diff = image - static
    else:
        at = group.index(k)
        previous = render.windowed(t.cube, group, group[at - 1], window) if at else image
        diff = image - previous
    limit = job.once(
        ("diff_limit", band), lambda: render.limits(np.abs(t.cube[group] - static), 0.0, 99.8)[1]
    )
    return render.diverging_rgba(np.where(blank | render.enclosed_blanks(diff), 0.0, diff), limit)


@app.get("/api/jobs/{job_id}/frames/{k}.png")
def frame_png(
    job_id: str,
    k: int,
    stretch: Literal["global", "frame"] = "global",
    view: Literal["image", "minus_static", "minus_previous"] = "image",
    window: int = Query(1, ge=1, le=15),
):
    job = done_job(job_id)
    if not 0 <= k < len(job.tracker.frames):
        raise HTTPException(404, "No such image.")
    return Response(
        render.png_bytes(frame_rgba(job, k, stretch, view, window)),
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=3600"},
    )


def filled_cube(job: Job) -> np.ndarray:
    """The whole cube with blank pixels filled by each detector's usual sky (for the GIF)."""
    t = job.tracker
    out = t.cube.copy()
    for band in t.meta.get("bands", []):
        group = [i for i, f in enumerate(t.frames) if f["band"] == band]
        static = job.once(("static", band), lambda g=group: render.static_sky(t.cube, g))
        out[group] = np.where(np.isfinite(out[group]), out[group], static)
    top = job.once("limits", lambda: render.limits(t.cube))[1]
    for k in range(len(out)):
        out[k][render.enclosed_blanks(out[k])] = top  # saturated star cores: white
    return out


def frame_label(f: dict) -> str:
    wave = f" · {f['wavelength_um']:.2f} µm" if f.get("wavelength_um") else ""
    return f"{f['time_utc'][:16]} UTC{wave}"


@app.get("/api/jobs/{job_id}/tracker.gif")
def tracker_gif(job_id: str):
    job = done_job(job_id)
    t = job.tracker
    credit = {"demo": "SIMULATED DEMO"}.get(t.meta["origin"], "SPHEREx / NASA-IPAC IRSA")
    data = render.gif_bytes(
        job.once("cube01", lambda: render.stretch(filled_cube(job))),
        [frame_label(f) for f in t.frames],
        zoom=max(2, round(480 / t.shape[0])),
        watermark=f"{APP_NAME} · {credit}",
    )
    return Response(
        data,
        media_type="image/gif",
        headers={"Content-Disposition": 'attachment; filename="skyface_tracker.gif"'},
    )


@app.get("/api/jobs/{job_id}/track")
def track_data(job_id: str):
    job = done_job(job_id)
    table, fit = get_track(job)
    target = job.tracker.target
    return {
        "rows": records(table),
        "fit": fit,
        "catalogue_pm": {"pmra": target.pmra, "pmdec": target.pmdec},
        "native_pixel_arcsec": job.tracker.meta["native_pixel_arcsec"],
    }


@app.get("/api/jobs/{job_id}/track.csv")
def track_csv(job_id: str):
    table, _ = get_track(done_job(job_id))
    return Response(
        table.to_csv(index=False),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="spherex_track.csv"'},
    )


@app.get("/api/jobs/{job_id}/logbook")
def job_logbook(job_id: str):
    """The observer's log: what was observed and whether anything moved, in plain sentences."""
    job = done_job(job_id)
    t = job.tracker
    table, fit = get_track(job)
    frames, target = t.frames, t.target
    return logbook.write_log(
        name=target.name,
        n_frames=len(frames),
        first_date=frames[0]["time_utc"][:10],
        last_date=frames[-1]["time_utc"][:10],
        span_days=float(frames[-1]["mjd"] - frames[0]["mjd"]),
        bands=t.meta.get("bands", []),
        n_solved=None,
        median_residual_arcsec=None,
        n_detected=int(table.ok.astype(bool).sum()),
        fit=fit,
        catalogue=logbook.Motion(target.pmra, target.pmdec) if target.has_motion else None,
        parallax_mas=target.parallax,
        pixel_arcsec=float(t.meta["native_pixel_arcsec"]),
        synthetic=t.meta["origin"] == "demo",
    )


@app.get("/api/jobs/{job_id}/spectrum")
def spectrum_here(
    job_id: str,
    east: float = Query(..., ge=-3000, le=3000),
    north: float = Query(..., ge=-3000, le=3000),
):
    """Spectrum at a spot picked in the tracker (arcsec East and North of the patch centre)."""
    t = done_job(job_id).tracker
    from spherex_viewer.align import offset_to_pix

    x, y = (float(v) for v in offset_to_pix(east, north, t.shape, t.scale))
    sky = t.wcs.pixel_to_world(x, y)
    return {
        "east": east,
        "north": north,
        "ra": float(sky.ra.deg),
        "dec": float(sky.dec.deg),
        "rows": records(measure.spectrum_at(t, east, north)),
    }


def compare_arrays(job: Job, split: float, band: str | None):
    """Early stack, late stack and late-minus-early (images are in time order)."""
    t = job.tracker
    idx = [i for i, f in enumerate(t.frames) if band in (None, "", f["band"])]
    if len(idx) < 2:
        raise HTTPException(422, "Need at least two images (try another detector).")
    s = int(min(max(round(len(idx) * split), 1), len(idx) - 1))
    early, late = render.mean_image(t.cube[idx[:s]]), render.mean_image(t.cube[idx[s:]])
    return early, late, late - early, idx, s


@app.get("/api/jobs/{job_id}/compare/meta")
def compare_meta(job_id: str, split: float = Query(0.5, ge=0.1, le=0.9), band: str | None = None):
    job = done_job(job_id)
    _, _, _, idx, s = compare_arrays(job, split, band)
    f = job.tracker.frames
    return {
        "n_early": s,
        "n_late": len(idx) - s,
        "early_from": f[idx[0]]["time_utc"],
        "early_to": f[idx[s - 1]]["time_utc"],
        "late_from": f[idx[s]]["time_utc"],
        "late_to": f[idx[-1]]["time_utc"],
    }


@app.get("/api/jobs/{job_id}/compare/{part}.png")
def compare_png(
    job_id: str,
    part: Literal["early", "late", "diff"],
    split: float = Query(0.5, ge=0.1, le=0.9),
    band: str | None = None,
):
    """Adding images lowers the noise; subtracting the early stack from the late one shows
    anything that moved as a blue (earlier) and red (later) pair."""
    early, late, diff, _, _ = compare_arrays(done_job(job_id), split, band)
    if part == "diff":
        rgba = render.diverging_rgba(diff, render.limits(np.abs(diff), 0.0, 99.5)[1])
    else:
        lo, hi = render.limits(np.stack([early, late]))
        rgba = render.gray_rgba(early if part == "early" else late, lo, hi)
    return Response(
        render.png_bytes(rgba), media_type="image/png", headers={"Cache-Control": "no-store"}
    )


@app.get("/api/jobs/{job_id}/overlay/gaia")
def gaia_stars(job_id: str):
    """Gaia stars to ring on the tracker: offsets from the patch centre (arcsec) at a reference
    year, plus yearly motion, so each ring follows its star from image to image."""
    job = done_job(job_id)
    t = job.tracker
    if t.meta.get("gaia"):
        return t.meta["gaia"]

    def fetch():
        radius = trackers.overlay_radius(t)
        mid_year = float(Time(t.frames[len(t.frames) // 2]["mjd"], format="mjd").jyear)
        stars = gaia.gaia_cone(t.center, radius / 60.0 + 1.0, 18.0)
        return overlays.gaia_overlay(stars, t.center, mid_year, radius)

    try:
        return job.once("gaia", fetch)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"The Gaia catalogue could not be reached: {exc}") from exc


# ------------------------------------------------------------------------------------------ site endpoints
@app.get("/api/links")
def cross_links(
    ra: float = Query(..., ge=0, lt=360),
    dec: float = Query(..., ge=-90, le=90),
    fov_arcmin: float = Query(6.0, ge=1, le=60),
):
    """Links to other tools for the same patch (WiseView, IRSA, CADC, SIMBAD, MAST) and credits."""
    return {"links": crosscheck.links(ra, dec, fov_arcmin), "credits": crosscheck.CREDITS}


@app.get("/api/diagnose")
def self_check():
    """Which archives this server can reach right now, and how much memory it is using."""
    report = diagnose.run()
    report["saved_objects"] = [s["id"] for s in STORE.summaries()]
    return report


@app.get("/api/health")
def health():
    return {"ok": True, "jobs": len(JOBS), "version": APP_VERSION, "saved": len(STORE.index())}


@app.get("/license", response_class=PlainTextResponse)
def licence():
    """The copyright notice and the full licence text."""
    return (ROOT / "NOTICE").read_text() + "\n" + "=" * 78 + "\n\n" + (ROOT / "LICENSE").read_text()


@app.get("/robots.txt", response_class=PlainTextResponse)
def robots():
    return "User-agent: *\nAllow: /\nDisallow: /api/\n"


# The page's stylesheet, script, fonts and logo are served from /assets.
app.mount("/assets", StaticFiles(directory=WEB), name="assets")


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host="127.0.0.1", port=8000)
