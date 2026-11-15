"""Targets, coordinate parsing, SIMBAD resolution and proper-motion propagation."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace

import astropy.units as u
import numpy as np
from astropy.coordinates import SkyCoord
from astropy.time import Time

from .config import CACHE_DIR


class TargetUnavailable(RuntimeError):
    """Raised when a target has no coordinates and they could not be resolved."""


@dataclass(frozen=True)
class Target:
    name: str
    ra_deg: float | None = None
    dec_deg: float | None = None
    pmra: float = 0.0  # mas/yr, already multiplied by cos(dec)
    pmdec: float = 0.0  # mas/yr
    parallax: float = 0.0  # mas
    ref_epoch: float = 2000.0  # Julian year at which ra/dec are valid
    simbad_names: tuple = ()
    band: str = "SPHEREx-D2"
    kind: str = "mover"  # "mover" | "variable" | "custom"
    notes: str = ""
    source: str = ""  # provenance of the numbers above

    @property
    def has_position(self) -> bool:
        return self.ra_deg is not None and self.dec_deg is not None

    @property
    def has_motion(self) -> bool:
        return bool(self.pmra or self.pmdec)

    def coord_at(self, when: Time) -> SkyCoord:
        """ICRS position at `when`, propagated with proper motion (and parallax-derived distance)."""
        if not self.has_position:
            raise TargetUnavailable(f"{self.name}: no coordinates; resolve the target first.")
        kw = {}
        if self.parallax and self.parallax > 0:
            kw["distance"] = (1000.0 / self.parallax) * u.pc
        c = SkyCoord(
            ra=self.ra_deg * u.deg,
            dec=self.dec_deg * u.deg,
            pm_ra_cosdec=self.pmra * u.mas / u.yr,
            pm_dec=self.pmdec * u.mas / u.yr,
            obstime=Time(self.ref_epoch, format="jyear"),
            frame="icrs",
            **kw,
        )
        return c.apply_space_motion(new_obstime=when)

    def coord_ref(self) -> SkyCoord:
        return SkyCoord(self.ra_deg * u.deg, self.dec_deg * u.deg, frame="icrs")


def parse_coords(ra_text: str, dec_text: str) -> SkyCoord:
    """Accept decimal degrees ('133.7951', '-7.2451') or sexagesimal ('08h55m10.831s', '-7d14m42.53s')."""
    ra_text, dec_text = ra_text.strip(), dec_text.strip()
    try:
        return SkyCoord(float(ra_text) * u.deg, float(dec_text) * u.deg, frame="icrs")
    except ValueError:
        pass
    try:
        return SkyCoord(ra_text, dec_text, unit=(u.hourangle, u.deg), frame="icrs")
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Could not parse RA/Dec ({ra_text!r}, {dec_text!r}): {exc}") from exc


def _sexa(ra: str, dec: str) -> tuple[float, float]:
    c = SkyCoord(ra, dec, unit=(u.hourangle, u.deg))
    return float(c.ra.deg), float(c.dec.deg)


_W0855_RA, _W0855_DEC = _sexa("08h55m10.831s", "-07d14m42.53s")
_EPS_IND_B = _sexa("22h04m10.392s", "-56d46m57.29s")  # epoch 1999.666, McCaughrean et al. 2004

TARGETS: dict[str, Target] = {
    "Barnard's Star": Target(
        "Barnard's Star",
        269.452075,
        4.693361,
        pmra=-802.803,
        pmdec=10362.394,
        parallax=546.976,
        ref_epoch=2000.0,
        simbad_names=("GJ 699",),
        band="SPHEREx-D2",
        kind="mover",
        notes="Highest proper motion of any star (~10.4 arcsec/yr, mostly north).",
        source="Offline fallback: J2000 position, Gaia DR3 proper motion. Live SIMBAD lookup overrides.",
    ),
    "Epsilon Indi B (Ba/Bb)": Target(
        "Epsilon Indi B (Ba/Bb)",
        *_EPS_IND_B,
        pmra=3981.977,
        pmdec=-2466.832,
        parallax=270.658,
        ref_epoch=1999.666,
        simbad_names=("eps Ind Ba", "eps Ind B"),
        band="SPHEREx-D2",
        kind="mover",
        notes="T1+T6 brown-dwarf pair, only ~0.6 arcsec apart: unresolved in SPHEREx (6.2 arcsec pixels). "
        "Epsilon Indi A is ~6.7 arcmin away and shares the motion.",
        source="Position: 22h04m10.392s -56d46m57.29s measured on a 1999.666 UK Schmidt plate (McCaughrean et al. "
        "2004, table 2). Proper motion and parallax: Gaia DR3. A live SIMBAD lookup overrides the position.",
    ),
    "WISE 0855-0714": Target(
        "WISE 0855-0714",
        _W0855_RA,
        _W0855_DEC,
        pmra=-8123.7,
        pmdec=673.2,
        parallax=439.0,
        ref_epoch=2010.34,
        band="SPHEREx-D6",
        kind="mover",
        notes="Coldest known brown dwarf (~250 K): expect it only in the longest-wavelength bands (D5/D6).",
        source="Position: challenge brief, which is the WISE discovery position (first imaged 4 May 2010, so epoch "
        "2010.34). Proper motion and parallax: Kirkpatrick et al. 2021. Cross-check: moved forward to mid-2014 it "
        "lands within 2 arcsec of the NEOWISE position used in the challenge's WiseView link.",
    ),
    "RW Aurigae": Target(
        "RW Aurigae",
        simbad_names=("RW Aur",),
        band="SPHEREx-D2",
        kind="variable",
        notes="Accreting T Tauri binary with deep irregular dimmings. Watch brightness, not position.",
        source="Resolved live via SIMBAD.",
    ),
    "R Draconis": Target(
        "R Draconis",
        simbad_names=("R Dra",),
        band="SPHEREx-D2",
        kind="variable",
        notes="Mira-type pulsating variable (~245 d period). Watch brightness, not position.",
        source="Resolved live via SIMBAD.",
    ),
}


# ----------------------------------------------------------------------------- SIMBAD resolution
def _num(row, cols: dict, key: str) -> float:
    if key not in cols:
        return float("nan")
    v = row[cols[key]]
    try:
        return float(np.ma.filled(v, np.nan)) if np.ma.is_masked(v) else float(v)
    except (TypeError, ValueError):
        return float("nan")


def _simbad_lookup(name: str) -> dict:
    from astroquery.simbad import Simbad

    s = Simbad()
    s.TIMEOUT = 10
    try:
        s.add_votable_fields("pmra", "pmdec", "plx_value")
    except Exception:  # older astroquery field names
        s = Simbad()
        s.add_votable_fields("pmra", "pmdec", "plx")
    t = s.query_object(name)
    if t is None or len(t) == 0:
        raise LookupError(f"SIMBAD has no object named {name!r}")
    cols = {c.lower(): c for c in t.colnames}
    row = t[0]
    ra, dec = row[cols["ra"]], row[cols["dec"]]
    if isinstance(ra, (str, bytes)):
        c = SkyCoord(str(ra), str(dec), unit=(u.hourangle, u.deg))
        ra, dec = c.ra.deg, c.dec.deg
    out = {
        "ra_deg": float(ra),
        "dec_deg": float(dec),
        "ref_epoch": 2000.0,  # SIMBAD: ICRS, epoch J2000
        "pmra": _num(row, cols, "pmra"),
        "pmdec": _num(row, cols, "pmdec"),
        "parallax": _num(row, cols, "plx_value") if "plx_value" in cols else _num(row, cols, "plx"),
    }
    return out


def resolve_target(t: Target, online: bool = True) -> Target:
    """Fill in / refresh coordinates and motion from SIMBAD (cached on disk). Never raises if the target
    already has fallback coordinates; raises TargetUnavailable otherwise."""
    cache_file = CACHE_DIR / "resolved_targets.json"
    cache = json.loads(cache_file.read_text()) if cache_file.exists() else {}
    rec = cache.get(t.name)
    note = ""
    if rec is None and online and t.simbad_names:
        for nm in t.simbad_names:
            try:
                rec = _simbad_lookup(nm)
                rec["resolved_as"] = nm
                cache[t.name] = rec
                CACHE_DIR.mkdir(parents=True, exist_ok=True)
                cache_file.write_text(json.dumps(cache, indent=1))
                break
            except Exception as exc:  # noqa: BLE001
                note = f"SIMBAD lookup failed for {nm!r}: {exc}"
                rec = None
    if rec:
        upd = {"ra_deg": rec["ra_deg"], "dec_deg": rec["dec_deg"], "ref_epoch": rec["ref_epoch"]}
        for k in ("pmra", "pmdec", "parallax"):  # keep fallback where SIMBAD has no value
            if rec.get(k) is not None and np.isfinite(rec[k]):
                upd[k] = rec[k]
        return replace(t, source=f"SIMBAD ({rec.get('resolved_as', 'cached')})", **upd)
    if not t.has_position:
        raise TargetUnavailable(
            f"{t.name}: could not resolve coordinates. {note or 'Network needed once.'}"
        )
    return replace(t, notes=(t.notes + " " + note).strip())


def custom_target(center: SkyCoord, name: str = "Custom patch") -> Target:
    return Target(
        name,
        float(center.ra.deg),
        float(center.dec.deg),
        kind="custom",
        source="User-entered coordinates (no proper motion assumed).",
    )
