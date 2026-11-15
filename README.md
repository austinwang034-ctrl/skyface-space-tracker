# Skyface Space Tracker

A public web tool for the NASA Space Apps challenge *Planet X and SPHEREx*. Give it a star name or
sky coordinates and it plays every SPHEREx image of that patch in time order, like a short video,
so anything that moves or changes brightness stands out.

Version 2.0.0. Python (FastAPI) back end, plain HTML/CSS/JavaScript front end, no build step.

## How it stays working when NASA's archive is down

SPHEREx images come from NASA's IRSA archive, which is sometimes slow or offline. So the site
keeps **saved copies of real SPHEREx images** in the `saved/` folder, one file per object. Saved
objects open instantly and never touch the archive; they show a green dot on the start screen.
Anything else is fetched live from IRSA, and if IRSA does not answer the site says so plainly
instead of hanging.

To save (or refresh) images, run this on a computer with internet while IRSA is up:

```bash
python save_images.py
```

That saves the five challenge test objects. It retries, skips anything that fails, and tells you
what it saved. More options:

```bash
python save_images.py --only rwaur
```

```bash
python save_images.py --name "Proxima Centauri" --bands D2,D6
```

Then upload the `saved/` folder to GitHub with the rest of the site.

## Run it on your computer

```bash
python -m venv .venv
```

```bash
source .venv/bin/activate
```

(On Windows: `.venv\Scripts\activate`.)

```bash
pip install -r requirements-server.txt
```

```bash
uvicorn server:app --port 8000
```

Then open <http://localhost:8000>. With no internet, the saved objects and **Watch a simulated
demo** still work.

To see which archives are reachable from your machine:

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

## Put it online for free (Render free plan)

1. Put the contents of this folder at the top level of a public GitHub repository. Folder names
   must arrive unchanged: `spherex_viewer`, `web`, `saved`, `tests`.
2. At render.com choose **New > Blueprint**, pick the repository and click **Apply**.
3. When the status turns **Live**, the address at the top of the page is your website.

Later commits redeploy the site automatically. The build checks that every needed file is
present and that the server starts, and stops with a clear message if an upload lost a folder.

The free plan gives 512 MB of memory and a tenth of a CPU core, and sleeps after 15 minutes
without visitors (the next visit waits about a minute). Saved objects are quick even so; live
requests are capped at 60 images by `render.yaml` and take a few minutes.

If something will not load on the live site, open `/api/diagnose` there: it lists each archive
as reachable or not, and how much memory the server is using.

## What it does

| Challenge item | Where |
| --- | --- |
| Enter RA/Dec, see the patch as a video | **Tracker**: play, step, scrub, speed, keyboard control |
| Easy to update with new data | `python save_images.py` refreshes saved objects; live requests always query the archive; release order is one line in `spherex_viewer/config.py` |
| Use the 102 bands | Pick detectors; every image carries the wavelength it saw at each pixel |
| Spectra on the fly | Click anywhere in the tracker: **Spectrum** tab |
| Add or subtract images in time | **Compare epochs** (early stack, late stack, difference, blink comparator); tracker views *Only what changed* and *Change since the image before*; *Add neighbouring images together* |
| Choose the field of view | 2 to 15 arcminutes under *More options* |
| Overlay other catalogues | Gaia stars ringed on the tracker, following each star's motion; links to WiseView (WISE) and NEOSSat for the same patch |
| Coordinates in the URL | `/?ra=133.795&dec=-7.245&epoch=2010.34&bands=D6`; add `&embed=1` for an iframe |
| Metadata and contamination flags | **Data quality** tab |
| Astropy and friends | Astropy, reproject and photutils on the server |

### URL parameters

`ra`, `dec` (degrees or sexagesimal), `epoch` (year the coordinates are valid), `pmra`, `pmdec`
(mas/yr), `name`, `fov` (arcmin), `bands` (`D1`..`D6`, comma-separated), `live=1` (skip the saved
copy), `saved=<id>` or `obj=<test object id>` (`barnard`, `wise0855`, `epsindi`, `rwaur`, `rdra`),
`demo=1`, `view` (`image`, `minus_static`, `minus_previous`), `window` (1, 3, 5, 9), `speed`,
`plate=1`, `gaia=1`, `embed=1`, `autorun=0`.

## How it is organised

```
server.py                 the web server: jobs, images, measurements (all under /api)
save_images.py            saves real SPHEREx images into saved/
check_live.py             one-shot check of every outside archive
saved/                    one .npz per saved object (real images, wavelengths, description)
spherex_viewer/
  config.py               every archive address, release list and setting
  tracker.py              a tracker: build it live, save it, load it, find it
  archive.py              IRSA search (TAP, then SIA, each with a deadline) and cutout download
  frames.py               FITS cutout -> image, WCS, time, wavelength, flags
  align.py                the common North-up grid
  measure.py              centroids, photometry, proper-motion fit, spectra
  render.py               stretches, difference colours, stacking, GIF
  gaia.py overlays.py     Gaia stars (ESA, MAST as backup) and their rings
  targets.py              test objects, coordinate parsing, SIMBAD with a Harvard fallback
  logbook.py              the plain-language observer's log
  crosscheck.py           links to WiseView, IRSA tools, CADC, SIMBAD; data credits
  protect.py diagnose.py  cross-site guard, rate limit, self-check
  synthetic.py            simulated images for the demo and the tests
web/                      index.html, styles.css, app.js, logo, bundled font and chart library
tests/                    offline test suite
```

## Settings (environment variables)

| Variable | Meaning | Default |
| --- | --- | --- |
| `SKYFACE_MAX_FRAMES` | most images one live tracker may use | 200 (60 on Render) |
| `SKYFACE_JOBS_PER_MINUTE` | trackers one visitor may start per minute | 12 |
| `SKYFACE_ALLOWED_ORIGINS` | other websites allowed to call the API | none |
| `SKYFACE_SAVED_DIR` | where saved objects live | `saved/` |
| `SPHEREX_RELEASES` | data releases, best first | `dr1,qr3,qr2` |

## Protecting the project

No website can hide its HTML, CSS or JavaScript: the browser needs them to show the page. What
this project does instead:

- **Licence.** `LICENSE` (GNU AGPL v3) and `NOTICE` state who owns the work. Anyone may reuse it,
  but copies and modified versions must keep the notice, credit the authors and publish their
  source. Put your team's name in `APP_OWNER` (`spherex_viewer/config.py`), `NOTICE` and the
  page footer.
- **No free back end for copycats.** The API refuses requests made by pages on other websites.
- **No passing the site off in a frame.** Only the embed view (`?embed=1`) may be framed.
- **Rate limit** on starting trackers, and the project's name on exported GIFs.

## Known limitations

- SPHEREx pixels are 6.15 arcseconds. Close pairs (Epsilon Indi Ba/Bb, RW Aurigae A/B) are one
  blended source, and a neighbour within a pixel or two pulls a measured position toward itself.
- Every exposure of a patch is at a different wavelength, so brightness changes between images
  mix real variability with colour. Compare images of similar wavelength before trusting a change.
- Photometry is quick-look aperture photometry. Use IRSA's Spectrophotometry Tool for science.
- Images are placed using the archive's own pointing, which real data shows is good to about half
  an arcsecond; there is no extra plate-solving step.
- Star names other than the five test objects need the SIMBAD catalogue (CDS or its Harvard
  mirror) to be reachable.
- Trackers being built live in memory in one server process: run a single worker.

## Data credits

This publication makes use of data products from the Spectro-Photometer for the History of the
Universe, Epoch of Reionization and Ices Explorer (SPHEREx), which is a joint project of the Jet
Propulsion Laboratory and the California Institute of Technology, and is funded by the National
Aeronautics and Space Administration. Data are served by the NASA/IPAC Infrared Science Archive.

Also used: Gaia (ESA/DPAC; mirror at MAST), SIMBAD (CDS, Strasbourg), WISE/NEOWISE through
WiseView (Backyard Worlds), and NEOSSat (Canadian Space Agency, archived at CADC). This is an
independent student project, not an official NASA product.

The bundled typeface is Atkinson Hyperlegible Next (Braille Institute, SIL Open Font License).
