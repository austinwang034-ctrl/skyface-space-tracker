"""A self-check the site runs on itself: can THIS server reach each archive it depends on?

Most "it won't load" problems on a small free host are one of three things: an archive that is
down or slow, a server that ran out of memory and restarted, or a blocked connection. Opening
/api/diagnose on the live site (not on your own computer) shows which, in a few seconds.
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from .config import APP_VERSION, IRSA_TAP_SYNC, SIMBAD_SERVERS

START = time.time()
TIMEOUT = 20.0

IRSA_QUERY = (
    "SELECT TOP 1 a.uri FROM spherex.artifact a JOIN spherex.plane p ON a.planeid = p.planeid "
    "WHERE 1 = CONTAINS(POINT('ICRS', 269.452, 4.6934), p.poly) AND p.energy_bandpassname = 'SPHEREx-D2'"
)

# (what it is used for, address, query parameters, text that must appear in a good reply)
CHECKS = [
    ("SPHEREx image search (IRSA)", IRSA_TAP_SYNC, {"QUERY": IRSA_QUERY, "FORMAT": "CSV"}, "uri"),
    (
        "Gaia stars, ESA",
        "https://gea.esac.esa.int/tap-server/tap/capabilities",
        None,
        "capabilities",
    ),
    (
        "Gaia stars, MAST backup",
        "https://mast.stsci.edu/vo-conesearch/api/v0.1/gaiadr3",
        {"RA": "269.45", "DEC": "4.69", "SR": "0.01", "format": "votable"},
        "VOTABLE",
    ),
] + [
    (f"SIMBAD names, {host}", f"https://{host}/simbad/sim-tap/capabilities", None, "capabilities")
    for host in SIMBAD_SERVERS
]


def _memory_mb() -> float | None:
    """Memory this server process is using, in MB (Linux only)."""
    try:
        with open("/proc/self/status") as status:
            for line in status:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024, 1)
    except OSError:
        pass
    return None


def _check(item, get) -> dict:
    name, url, params, must_contain = item
    started = time.time()
    try:
        reply = get(url, params=params, timeout=TIMEOUT)
        seconds = round(time.time() - started, 1)
        good = reply.status_code < 400 and must_contain.lower() in reply.text[:20000].lower()
        detail = "ok" if good else f"HTTP {reply.status_code}: {reply.text[:120].strip()!r}"
        return {"name": name, "ok": good, "seconds": seconds, "detail": detail}
    except Exception as exc:  # noqa: BLE001  (a failed check is the answer, not an error)
        return {
            "name": name,
            "ok": False,
            "seconds": round(time.time() - started, 1),
            "detail": f"{type(exc).__name__}: {str(exc)[:140]}",
        }


def run(get=requests.get) -> dict:
    """Test every archive at once (so the whole check takes no longer than the slowest one)."""
    with ThreadPoolExecutor(max_workers=len(CHECKS)) as pool:
        results = list(pool.map(lambda item: _check(item, get), CHECKS))
    failed = [r["name"] for r in results if not r["ok"]]
    simbad = [r for r in results if r["name"].startswith("SIMBAD")]
    return {
        "version": APP_VERSION,
        "uptime_minutes": round((time.time() - START) / 60, 1),
        "memory_mb": _memory_mb(),
        "cpu_count": os.cpu_count(),
        "all_ok": not failed,
        "failed": failed,
        "names_work": any(r["ok"] for r in simbad),
        "archives": results,
    }
