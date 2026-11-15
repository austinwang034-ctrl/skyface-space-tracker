"""Save real SPHEREx images into the site, so they load even when NASA's archive is down.

Run it from the project folder, with the virtual environment active, on a computer where the
archive answers (it needs the internet):

    python save_images.py

That saves the five challenge test objects. Each becomes one file in the saved/ folder
(saved/barnard.npz and so on). Upload the saved/ folder to GitHub with the rest of the site and
those objects open instantly from then on, whatever state the archive is in.

More examples:

    python save_images.py --only barnard,wise0855      just these test objects
    python save_images.py --bands D2,D6                 these detectors for every object
    python save_images.py --name "Proxima Centauri"     any object SIMBAD knows, saved as proxima-centauri
    python save_images.py --ra 133.7951 --dec -7.2451 --epoch 2010.34 --id mypatch --bands D6

An object that fails (the archive may be slow or down) is reported and skipped; the others are
still saved. Run the script again later to pick up the ones that failed, or to refresh all of
them with newer images.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
import traceback
from datetime import datetime, timezone

from astropy.time import Time

from spherex_viewer import gaia, overlays
from spherex_viewer import tracker as trackers
from spherex_viewer.config import BANDS, SAVED_DIR
from spherex_viewer.targets import (
    TARGETS,
    TEST_IDS,
    Target,
    _simbad_lookup,
    builtin_by_name,
    parse_coords,
)

ATTEMPTS = 3


def parse_bands(text: str | None) -> list[str] | None:
    """'D2,D6' or 'all' -> ['SPHEREx-D2', 'SPHEREx-D6']. None means each object's usual detector."""
    if not text:
        return None
    if text.strip().lower() == "all":
        return list(BANDS)
    bands = [f"SPHEREx-{part.strip().upper().replace('SPHEREX-', '')}" for part in text.split(",")]
    unknown = [b for b in bands if b not in BANDS]
    if unknown:
        raise SystemExit(f"Unknown detector(s): {', '.join(unknown)}. Use D1 to D6, or 'all'.")
    return bands


def slug(name: str) -> str:
    """A file-name-safe id: 'Proxima Centauri' -> 'proxima-centauri'."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "object"


def named_target(name: str) -> Target:
    """A built-in test object, or any other name looked up in SIMBAD."""
    known = builtin_by_name(name)
    if known is not None:
        return known
    r = _simbad_lookup(name)

    def number(key):
        value = r.get(key)
        return float(value) if value is not None and value == value else 0.0  # NaN -> 0

    return Target(
        name,
        r["ra_deg"],
        r["dec_deg"],
        pmra=number("pmra"),
        pmdec=number("pmdec"),
        parallax=number("parallax"),
        ref_epoch=2000.0,
        source="SIMBAD",
    )


def wanted(args) -> list[tuple[str, Target]]:
    """(id, target) for everything this run should save."""
    if args.name:
        target = named_target(args.name)
        return [(args.id or slug(args.name), target)]
    if args.ra or args.dec:
        if not (args.ra and args.dec):
            raise SystemExit("Give both --ra and --dec.")
        c = parse_coords(args.ra, args.dec)
        name = args.id or "patch"
        target = Target(
            name, float(c.ra.deg), float(c.dec.deg), ref_epoch=args.epoch, kind="custom"
        )
        return [(slug(name), target)]
    ids = [part.strip() for part in args.only.split(",")] if args.only else list(TEST_IDS)
    unknown = [i for i in ids if i not in TEST_IDS]
    if unknown:
        raise SystemExit(
            f"Unknown test object(s): {', '.join(unknown)}. Choose from: {', '.join(TEST_IDS)}."
        )
    return [(i, TARGETS[TEST_IDS[i]]) for i in ids]


def add_gaia(tracker: trackers.Tracker) -> str:
    """Store the Gaia stars of the patch with the images, so the overlay needs no network later."""
    try:
        radius = trackers.overlay_radius(tracker)
        mid = tracker.frames[len(tracker.frames) // 2]["mjd"]
        stars = gaia.gaia_cone(tracker.center, radius / 60.0 + 1.0, 18.0)
        tracker.meta["gaia"] = overlays.gaia_overlay(
            stars, tracker.center, float(Time(mid, format="mjd").jyear), radius
        )
        return f"{len(tracker.meta['gaia']['sources'])} Gaia stars"
    except Exception as exc:  # noqa: BLE001  (the images are what matters; Gaia can be fetched live)
        return f"no Gaia stars saved ({type(exc).__name__})"


def save_one(saved_id: str, target: Target, bands: list[str], fov: float, max_frames: int) -> str:
    """Build one tracker from the live archive (a few attempts) and write it to saved/."""
    last: Exception | None = None
    for attempt in range(1, ATTEMPTS + 1):
        try:
            tracker = trackers.build_live(
                target,
                fov,
                bands,
                max_frames,
                progress=lambda message, fraction: print(f"    {message}", end="\r", flush=True),
            )
            break
        except Exception as exc:  # noqa: BLE001
            last = exc
            print(f"    attempt {attempt} of {ATTEMPTS} failed: {str(exc)[:160]}")
            if attempt < ATTEMPTS:
                time.sleep(20 * attempt)
    else:
        raise RuntimeError(f"gave up after {ATTEMPTS} attempts: {last}")
    stars = add_gaia(tracker)
    tracker.meta["saved_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    path = trackers.save(tracker, SAVED_DIR / f"{saved_id}.npz")
    frames = tracker.frames
    return (
        f"{len(frames)} images, {frames[0]['time_utc'][:10]} to {frames[-1]['time_utc'][:10]}, "
        f"{stars}, {path.stat().st_size / 1e6:.1f} MB"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Save real SPHEREx images into the saved/ folder.")
    parser.add_argument("--only", help="comma-separated test objects: " + ", ".join(TEST_IDS))
    parser.add_argument("--name", help="any object name SIMBAD knows")
    parser.add_argument("--ra", help="right ascension (degrees or 08h55m10.8s)")
    parser.add_argument("--dec", help="declination (degrees or -07d14m42.5s)")
    parser.add_argument(
        "--epoch", type=float, default=2000.0, help="year the coordinates are valid"
    )
    parser.add_argument("--id", help="file name to save under (letters, digits, - and _)")
    parser.add_argument(
        "--bands", help="detectors, e.g. D2,D6 or all (default: each object's usual one)"
    )
    parser.add_argument(
        "--fov", type=float, default=5.0, help="field of view in arcminutes (2 to 15)"
    )
    parser.add_argument("--max-frames", type=int, default=60, help="most images per object")
    args = parser.parse_args()
    if not 2 <= args.fov <= 15:
        raise SystemExit("--fov must be between 2 and 15 arcminutes.")
    bands = parse_bands(args.bands)
    results = []
    for saved_id, target in wanted(args):
        print(f"\n{target.name}  ->  saved/{saved_id}.npz")
        try:
            detail = save_one(saved_id, target, bands or [target.band], args.fov, args.max_frames)
            results.append((saved_id, True, detail))
            print(f"    SAVED: {detail}" + " " * 20)
        except Exception as exc:  # noqa: BLE001
            results.append((saved_id, False, str(exc)[:200]))
            print(f"    FAILED: {str(exc)[:200]}")
            traceback.print_exc(limit=1)
    good = [r for r in results if r[1]]
    print("\n" + "=" * 60)
    print(f"{len(good)} of {len(results)} saved into {SAVED_DIR}")
    for saved_id, ok, detail in results:
        print(f"  [{'OK' if ok else 'FAILED':6}] {saved_id}: {detail}")
    if good:
        print("\nNext: upload the saved/ folder to GitHub with the rest of the site.")
    return 0 if len(good) == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
