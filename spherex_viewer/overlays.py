"""Catalogue overlay for the tracker: Gaia stars drawn on top of the SPHEREx images.

An overlay answers the first question a mover-hunter asks: "is that dot already known?" A source in
the image with no ring around it is not in Gaia. Positions are sent as offsets from the patch
centre at a reference year plus a yearly motion, so the browser can place each ring correctly in every
frame, including for fast-moving stars.
"""

from __future__ import annotations

import numpy as np
from astropy.coordinates import SkyCoord
from astropy.table import Table
from astropy.time import Time

from .gaia import propagate

MAX_SOURCES = 600


def _number(value) -> float | None:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if np.isfinite(x) else None


def _offsets(center: SkyCoord, coords: SkyCoord) -> tuple[np.ndarray, np.ndarray]:
    east, north = center.spherical_offsets_to(coords)
    return east.arcsec, north.arcsec


def gaia_overlay(tbl: Table, center: SkyCoord, year: float, radius_arcsec: float) -> dict:
    """Gaia stars inside the patch: offsets at `year` and yearly motion, both in arcsec."""
    east, north = _offsets(center, propagate(tbl, Time(year, format="jyear")))
    pm_e = np.nan_to_num(np.asarray(tbl["pmra"], dtype=float)) / 1000.0
    pm_n = np.nan_to_num(np.asarray(tbl["pmdec"], dtype=float)) / 1000.0
    mag = np.asarray(tbl["phot_g_mean_mag"], dtype=float)
    keep = np.flatnonzero(np.hypot(east, north) <= radius_arcsec)
    keep = keep[np.argsort(np.nan_to_num(mag[keep], nan=99.0))][:MAX_SOURCES]
    return {
        "catalogue": "Gaia DR3",
        "epoch": year,
        "sources": [
            {
                "id": str(tbl["source_id"][i]),
                "e": float(east[i]),
                "n": float(north[i]),
                "pm_e": float(pm_e[i]),
                "pm_n": float(pm_n[i]),
                "mag": _number(mag[i]),
            }
            for i in keep
        ],
    }
