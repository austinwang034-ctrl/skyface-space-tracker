# Skyface Space Tracker

A public web tool for the NASA Space Apps challenge *Planet X and SPHEREx*. Enter a right
ascension and declination and it plays every SPHEREx image of that patch of sky in time order,
like a short video, so anything that moves or changes brightness stands out.

Version 1.3.0. Python (FastAPI) back end, plain HTML/CSS/JavaScript front end, no build step.

## Run it

```bash
python -m venv .venv
```

```bash
source .venv/bin/activate
```

```bash
pip install -r requirements-server.txt
```

```bash
uvicorn server:app --port 8000
```

Then open <http://localhost:8000>. With no internet, press **"or watch a simulated demo first"**
on the welcome screen (or tick *Use made-up demo data* under *More options*).

To check that every archive is reachable from your machine:

```bash
python check_live.py
```

To run the tests (they need no network):

```bash
pip install -r requirements-dev.txt
```

```bash
python -m pytest
```

## What it does

| Challenge item | Where |
| --- | --- |
| Enter RA/Dec, see the patch as a video | **Tracker** tab: play, step, scrub, speed, keyboard control |
| Easy to update with new data releases | `RELEASES` in `spherex_viewer/config.py` (or the `SPHEREX_RELEASES` variable); the archive is queried live on every request |
| Use the 102 bands | Pick detectors; **Wavelengths** tab stacks images inside a molecule's band; per-detector stacks and an infrared colour picture in **Compare epochs** |
| Spectra on the fly | Click anywhere in the tracker: **Spectrum** tab |
| Add or subtract images in time | **Compare epochs** (early stack, late stack, difference, blink comparator); tracker views *Only what changed* and *Change since the image before*; *Add neighbouring images together* |
| Choose the field of view | 2 to 15 arcminutes under *More options* |
| Overlay Gaia and WISE | Rings on the tracker itself (Gaia DR3, CatWISE2020/AllWISE) that follow each star's motion; also on the sky map |
| Coordinates in the URL | `/?ra=133.795&dec=-7.245&fov=5&bands=D6`; add `&embed=1` for an iframe |
| Metadata and contamination flags | **Data quality** tab |
| Astropy, Aladin Lite | Astropy, reproject and photutils on the server; Aladin Lite for the sky map |

### URL parameters

`ra`, `dec` (degrees or sexagesimal), `epoch` (year the coordinates are valid), `fov` (arcmin),
`bands` (`D1`..`D6`, comma-separated), `view` (`image`, `minus_static`, `minus_previous`),
`window` (1, 3, 5, 9), `speed` (images per second), `plate=1`, `gaia=1`, `wise=1`, `embed=1`,
`demo=1`, `autorun=0`, and `obj=<id>` to open a featured object on the sky map.

## How it is organised

```
server.py                 FastAPI app: jobs, images, measurements (all under /api)
check_live.py             one-shot check of every external archive
spherex_viewer/
  config.py               every archive address, release list and constant
  archive.py              IRSA search (TAP, then SIA) and cutout download with a disk cache
  frames.py               FITS cutout -> Frame (image, WCS, time, wavelength, flags)
  align.py                common North-up grid and reprojection
  astrometry.py           plate solving against Gaia
  measure.py              centroids, photometry, proper-motion fit, spectra
  render.py               stretches, difference colours, tracker views, GIF
  overlays.py             Gaia and WISE sources to draw over the tracker
  signatures.py           molecular bands and colour temperature
  logbook.py              the plain-language observer's log
  identify.py targets.py  SIMBAD lookups, coordinate parsing, proper-motion propagation
  neighborhood.py         Gaia cone search (ESA, MAST as backup)
  neossat.py              NEOSSat images from CADC
  smallbodies.py          known asteroids and comets from NASA JPL
  crosscheck.py           links to WiseView, IRSA tools, SIMBAD; data credits
  objects.py              profiles of the five test objects
  synthetic.py            simulated frames for the offline demo and the tests
web/                      index.html, styles.css, app.js, bundled font, Plotly, Aladin Lite
tests/                    offline test suite
```

## Adopting a new SPHEREx data release

Put its folder name first in `RELEASES` in `spherex_viewer/config.py`, or set
`SPHEREX_RELEASES="dr2,dr1,qr3"` in the environment. When the same exposure exists in several
releases the first one listed wins; within a release the newest processing version wins.

## Start screen and logo

The site opens on a start screen with three ways in: type a star's name (looked up in SIMBAD),
type coordinates, or open the sky map. Click the logo in the header to come back to it. The logo
(`web/logo.svg`, `web/favicon.svg`) is a reticle around a star that has jumped from the faint dot
to the bright one: the blink-comparator idea in one picture.

## Protecting the project

No website can hide its HTML, CSS or JavaScript: the browser needs them to show the page. What
this project does instead:

- **Licence.** `LICENSE` (GNU AGPL v3) and `NOTICE` state who owns the work. Anyone may reuse it,
  but copies and modified versions, including ones only run as a website, must keep the notice,
  credit the authors and publish their source. The notice is in the page footer and at `/license`.
  Put your team's name in `APP_OWNER` (`spherex_viewer/config.py`), `NOTICE` and the footer.
- **No free back end for copycats.** The API refuses requests made by pages on other websites
  (HTTP 403). To let a partner site call it, list its origin in `SKYFACE_ALLOWED_ORIGINS`.
- **No passing the site off in a frame.** Only the deliberate embed view (`?embed=1`) may be
  shown inside another page; the full site may not.
- **Rate limit.** One visitor may start `SKYFACE_JOBS_PER_MINUTE` trackers per minute (default 12).
- **Credit travels with exports.** Downloaded GIFs carry the project's name.

The strongest proof of authorship is outside the code: a public Git history with your commits,
and the site running on a domain you control.

## Known limitations

- SPHEREx pixels are 6.15 arcseconds. Close pairs (Epsilon Indi Ba/Bb, RW Aurigae A/B) are one
  blended source, and a neighbour within a pixel or two pulls a fitted position toward itself.
- Every exposure of a patch is at a different wavelength, so brightness changes between frames
  mix real variability with colour. *Only what changed* therefore leaves faint residue on
  strongly coloured stars; compare frames of similar wavelength before trusting a variation.
- Photometry is quick-look aperture photometry. Use IRSA's Spectrophotometry Tool for science.
- Known small bodies are computed for an observer on the ground, not for SPHEREx's orbit.
- Jobs live in memory in one server process: run a single worker.

## Data credits

This publication makes use of data products from the Spectro-Photometer for the History of the
Universe, Epoch of Reionization and Ices Explorer (SPHEREx), which is a joint project of the Jet
Propulsion Laboratory and the California Institute of Technology, and is funded by the National
Aeronautics and Space Administration. Data are served by the NASA/IPAC Infrared Science Archive.

Also used: Gaia (ESA/DPAC; mirror at MAST), SIMBAD, VizieR and Aladin Lite (CDS, Strasbourg),
WISE/NEOWISE and CatWISE (NASA/JPL-Caltech), NEOSSat (Canadian Space Agency, archived at CADC),
and the NASA JPL Small-Body Identification service. This is an independent student project, not
an official NASA product.

The bundled typeface is Atkinson Hyperlegible Next (Braille Institute, SIL Open Font License).
