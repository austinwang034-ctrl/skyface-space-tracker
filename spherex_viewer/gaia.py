"""Gaia stars: cone search (ESA, then the MAST mirror; cached on disk) and moving them to a date."""

from __future__ import annotations

import hashlib

import astropy.units as u
import numpy as np
from astropy.coordinates import SkyCoord
from astropy.table import Table
from astropy.time import Time

from .config import CATALOG_DIR

GAIA_REF_EPOCH = 2016.0


def gaia_cone(center: SkyCoord, radius_arcmin: float, mag_limit: float = 18.0) -> Table:
    """Gaia DR3 sources in a cone (needs network the first time, then served from the disk cache)."""
    key = f"{center.ra.deg:.5f}_{center.dec.deg:.5f}_{radius_arcmin:.2f}_{mag_limit:.1f}"
    path = CATALOG_DIR / f"gaia_{hashlib.sha1(key.encode()).hexdigest()[:16]}.ecsv"
    if path.exists():
        return Table.read(path)
    errors = []
    tbl = None
    for name, fn in (("ESA Gaia archive", _gaia_esa), ("MAST Gaia mirror", _gaia_mast)):
        try:
            tbl = fn(center, radius_arcmin, mag_limit)
            tbl.meta["source"] = name
            break
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: {exc}")
    if tbl is None:
        raise RuntimeError(" | ".join(errors))
    CATALOG_DIR.mkdir(parents=True, exist_ok=True)
    tbl.write(path, format="ascii.ecsv", overwrite=True)
    return tbl


GAIA_COLS = ("source_id", "ra", "dec", "parallax", "pmra", "pmdec", "phot_g_mean_mag", "ref_epoch")


def _gaia_esa(center: SkyCoord, radius_arcmin: float, mag_limit: float) -> Table:
    from astroquery.gaia import Gaia

    q = (
        f"SELECT source_id, ra, dec, parallax, pmra, pmdec, phot_g_mean_mag, ref_epoch "
        f"FROM gaiadr3.gaia_source WHERE 1=CONTAINS(POINT('ICRS', ra, dec), "
        f"CIRCLE('ICRS', {center.ra.deg:.8f}, {center.dec.deg:.8f}, {radius_arcmin / 60.0:.6f})) "
        f"AND phot_g_mean_mag < {mag_limit}"
    )
    return Gaia.launch_job_async(q).get_results()


def normalise_gaia(raw: Table, mag_limit: float) -> Table:
    """Reduce any Gaia DR3 table (whatever the column capitalisation) to the columns this package uses."""
    low = {c.lower(): c for c in raw.colnames}
    missing = [c for c in GAIA_COLS[:7] if c not in low]
    if missing:
        raise ValueError(f"Gaia table lacks columns {missing}")
    out = Table(
        {
            c: (
                np.ma.filled(np.ma.asarray(raw[low[c]], dtype=float), np.nan)
                if c != "source_id"
                else [str(v) for v in raw[low[c]]]
            )
            for c in GAIA_COLS[:7]
        }
    )
    out["ref_epoch"] = (
        np.ma.filled(np.ma.asarray(raw[low["ref_epoch"]], dtype=float), GAIA_REF_EPOCH)
        if "ref_epoch" in low
        else np.full(len(out), GAIA_REF_EPOCH)
    )
    return out[np.asarray(out["phot_g_mean_mag"]) < mag_limit]


def _gaia_mast(center: SkyCoord, radius_arcmin: float, mag_limit: float) -> Table:
    """Gaia DR3 from the copy hosted at MAST (STScI), through its VO cone-search service."""
    from io import BytesIO

    import requests

    from .config import MAST_GAIA_CONE

    r = requests.get(
        MAST_GAIA_CONE,
        params={
            "format": "votable",
            "RA": f"{center.ra.deg:.8f}",
            "DEC": f"{center.dec.deg:.8f}",
            "SR": f"{radius_arcmin / 60.0:.6f}",
        },
        timeout=60,
    )
    r.raise_for_status()
    return normalise_gaia(Table.read(BytesIO(r.content), format="votable"), mag_limit)


def propagate(tbl: Table, when: Time) -> SkyCoord:
    """Positions of all rows at `when` using each star's proper motion (missing PM -> 0)."""
    pmra = np.nan_to_num(np.asarray(tbl["pmra"], dtype=float))
    pmdec = np.nan_to_num(np.asarray(tbl["pmdec"], dtype=float))
    c = SkyCoord(
        ra=np.asarray(tbl["ra"], dtype=float) * u.deg,
        dec=np.asarray(tbl["dec"], dtype=float) * u.deg,
        pm_ra_cosdec=pmra * u.mas / u.yr,
        pm_dec=pmdec * u.mas / u.yr,
        obstime=Time(GAIA_REF_EPOCH, format="jyear"),
        frame="icrs",
    )
    moved = c.apply_space_motion(new_obstime=when)
    return SkyCoord(
        moved.ra, moved.dec, frame="icrs"
    )  # plain ICRS: drop obstime so it mixes with other coords
