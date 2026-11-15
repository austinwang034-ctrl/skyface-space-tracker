/*
 * Skyface Space Tracker: page behaviour.
 *
 * What happens, in order:
 *   1. The start screen offers three ways in: a star's name, coordinates, or a challenge object.
 *   2. The page asks the server for a tracker of that patch and shows progress while it is built.
 *      Saved objects open instantly; others are fetched live from NASA's archive.
 *   3. The images are downloaded and played like a short video, with the year on screen.
 *   4. The other tabs (compare epochs, motion, spectrum, ...) load only when opened.
 *
 * Top to bottom: settings, small helpers, the form, building a tracker, the player, the tabs,
 * sharing, the start screen, and finally the button wiring.
 *
 * All data comes from this site's own server under /api (see server.py).
 */

// ===== Settings =====

// Colours drawn on the canvas and charts. Keep these in step with styles.css.
const COLOR = {
  text: "#edf3fa",
  muted: "#a6bad1",
  grid: "#24446b",
  ink: "#081828", // dark ink, used on the light "plate view"
  target: "#ff6b35", // the tracked object and its path (international orange)
  targetFaint: "rgba(255,107,53,.45)",
  cyan: "#7fe3ff", // a spot the visitor clicked
  red: "#ff5d5d", // measured position
  gaia: "#9be3b4", // rings around Gaia stars
  readout: "rgba(8,24,40,.85)", // box behind the year
};
const FONT = '"Atkinson Hyperlegible Next", system-ui, sans-serif';

// ===== Small helpers =====

const $ = (selector) => document.querySelector(selector);
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// Make text safe to insert into HTML.
const esc = (text) =>
  String(text ?? "").replace(
    /[&<>"]/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c],
  );

// Format a number with a fixed number of decimals ("–" when missing).
const fmt = (value, decimals = 2) =>
  value == null || Number.isNaN(Number(value)) ? "–" : Number(value).toFixed(decimals);

// Everything we know about the tracker currently on screen.
const state = {
  cfg: null, // server settings: bands, test objects, saved objects
  lookup: null, // the last object found by name or picked from the list (carries its motion)
  job: null, // id of the tracker on screen
  mv: null, // its description: grid, target, one entry per image
  imgs: [], // the downloaded images
  k: 0, // image now showing
  timer: null, // playback timer
  track: null, // measured positions and brightness (fetched once)
  gaia: null, // Gaia stars to ring
  click: null, // spot the visitor clicked, with its spectrum
  loadId: 0, // counts image loads, so a slow old load cannot overwrite a newer one
  makeId: 0, // counts tracker requests, so a slow old request cannot overwrite a newer one
  lastBody: null, // the request that produced this tracker (for the share link)
};

// Call our server and return the JSON reply. Server error messages become thrown Errors.
async function api(url, options) {
  let reply;
  try {
    reply = await fetch(url, options);
  } catch (e) {
    throw new Error("The site’s server did not answer. It may be waking up: wait a minute and try again.");
  }
  if (!reply.ok) {
    let message = reply.statusText || `error ${reply.status}`;
    try {
      const detail = (await reply.json()).detail;
      if (Array.isArray(detail)) {
        message = detail.map((d) => `${d.loc[d.loc.length - 1]}: ${d.msg}`).join("; ");
      } else if (detail) {
        message = detail;
      }
    } catch (e) {
      /* the reply was not JSON: keep the status text */
    }
    const error = new Error(message);
    error.status = reply.status;
    throw error;
  }
  return reply.json();
}

const postJson = (body) => ({
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

// Shared look for all Plotly charts.
const chartLayout = (extra = {}) => ({
  paper_bgcolor: "rgba(0,0,0,0)",
  plot_bgcolor: "rgba(0,0,0,0)",
  font: { color: COLOR.text, family: FONT },
  margin: { t: 44, r: 10, b: 50, l: 60 },
  ...extra,
  xaxis: { gridcolor: COLOR.grid, ...(extra.xaxis || {}) },
  yaxis: { gridcolor: COLOR.grid, ...(extra.yaxis || {}) },
});

// Draw a chart, or say why not if the chart library has not arrived yet.
function chart(id, traces, layout) {
  const box = $("#" + id);
  if (typeof Plotly === "undefined") {
    box.innerHTML = `<p class="note">The chart library is still loading. Open this tab again in a moment.</p>`;
    return;
  }
  Plotly.newPlot(box, traces, chartLayout(layout), { responsive: true, displaylogo: false });
}

// Build an HTML table. columns = [key, heading, optional formatter].
const table = (columns, rows, rowAttr = () => "") =>
  `<table><tr>${columns.map((c) => `<th>${esc(c[1])}</th>`).join("")}</tr>${rows
    .map(
      (r, i) =>
        `<tr${rowAttr(r, i)}>${columns.map((c) => `<td>${c[2] ? c[2](r[c[0]], r) : esc(r[c[0]])}</td>`).join("")}</tr>`,
    )
    .join("")}</table>`;

const setProgress = (fraction, message) => {
  $("#sbar").style.width = 100 * fraction + "%";
  $("#smsg").textContent = message;
};

const showError = (message) => {
  $("#error").hidden = !message;
  $("#error").textContent = message || "";
};

// "2025-08-14 11:50 · 1.36 µm"
const frameLabel = (f) =>
  f.time_utc.slice(0, 16) + (f.wavelength_um ? ` · ${fmt(f.wavelength_um, 2)} µm` : "");

// Julian year of an image, for moving catalogue positions to its date.
const frameYear = (f) => 2000 + (f.mjd - 51544.5) / 365.25;

// ===== The form =====

// Turn the "simulated demo" option on or off (the detector boxes mean nothing for the demo).
function setDemo(on) {
  $("#s_demo").checked = on;
  document.querySelectorAll("#bands input").forEach((box) => (box.disabled = on));
}

// Tick one detector band and untick the rest.
const setBand = (band) =>
  document.querySelectorAll("#bands input").forEach((box) => (box.checked = box.value === band));

const checkedBands = () =>
  [...document.querySelectorAll("#bands input:checked")].map((box) => box.value);

// Put an object (from a name lookup or the challenge list) into the form. Its motion is kept in
// state.lookup and sent with the request, so the tracker follows a fast-moving star.
function fillForm(object) {
  setDemo(false); // choosing a real object always means real images
  state.lookup = object;
  $("#name").value = object.name || "";
  $("#ra").value = object.ra;
  $("#dec").value = object.dec;
  $("#epoch").value = object.epoch ?? 2000;
  if (object.band) setBand(object.band);
}

// Ask the server where a named object is and put it in the form.
async function lookupName(name) {
  fillForm(await api("/api/resolve?name=" + encodeURIComponent(name)));
}

// One button per challenge object; a green dot marks those with real images saved on the site.
function objectButtons() {
  return state.cfg.test_objects
    .map(
      (o) =>
        `<button type="button" data-obj="${esc(o.id)}" title="${esc(o.notes)}">${o.saved ? '<span class="dot" title="Saved on this site"></span>' : ""}${esc(o.name)}</button>`,
    )
    .join("");
}

// Runs once at start: read settings from the server and fill in the form.
async function init() {
  state.cfg = await api("/api/config");
  $("#bands").innerHTML = Object.entries(state.cfg.bands)
    .map(
      ([band, [lo, hi]]) =>
        `<label class="opt"><input type="checkbox" value="${band}"> ${band.slice(-2)} · ${lo}–${hi} µm</label>`,
    )
    .join("");
  setBand("SPHEREx-D2");
  // A small host may cap how many images one tracker uses; keep the slider within that.
  const slider = $("#maxf");
  slider.max = Math.min(+slider.max, state.cfg.max_frames || 200);
  slider.value = Math.min(+slider.value, +slider.max);
  $("#maxv").textContent = slider.value;
  $("#ex").innerHTML = $("#start_objects").innerHTML = objectButtons();
}

const testObject = (id) => state.cfg.test_objects.find((o) => o.id === id);

// ===== Building a tracker =====

// The request for whatever is in the form.
function formRequest() {
  const demo = $("#s_demo").checked,
    lookup = state.lookup,
    body = {
      source: demo ? "demo" : $("#live").checked ? "live" : "auto",
      name: $("#name").value.trim() || null,
      ra: $("#ra").value.trim(),
      dec: $("#dec").value.trim(),
      epoch: parseFloat($("#epoch").value) || 2000,
      fov_arcmin: +$("#fov").value,
      bands: checkedBands(),
      max_frames: +$("#maxf").value,
    };
  // Send the looked-up motion only while the form still holds that object's coordinates.
  if (lookup && String(lookup.ra) === body.ra && String(lookup.dec) === body.dec) {
    body.pmra = lookup.pmra ?? null;
    body.pmdec = lookup.pmdec ?? null;
    body.parallax = lookup.parallax ?? null;
  }
  return body;
}

// The main action: ask for a tracker, wait for it, then show it.
async function make(body = formRequest()) {
  const id = ++state.makeId;
  stop();
  showError("");
  ["result", "empty"].forEach((name) => ($("#" + name).hidden = true));
  $("#status").hidden = false;
  $("#go").disabled = true;
  setProgress(0, "Starting…");
  try {
    if (body.source !== "demo" && !body.saved_id && !(body.ra && body.dec))
      throw Object.assign(new Error("Enter both a right ascension and a declination."), {
        plain: true,
      });
    let job = await api("/api/jobs", postJson(body));
    while (job.state === "queued" || job.state === "running") {
      if (id !== state.makeId) return; // a newer request has replaced this one
      setProgress(job.progress, job.message);
      await sleep(700);
      job = await api("/api/jobs/" + job.id);
    }
    if (id !== state.makeId) return;
    if (job.state === "error") throw new Error(job.error);
    state.job = job.id;
    state.lastBody = body;
    await loadTracker();
  } catch (e) {
    if (id !== state.makeId) return;
    let message = e.message;
    if (e.status === 404 && /unknown or expired job/i.test(message))
      message =
        "The server restarted while building your tracker (a free host restarts when it runs out of memory or goes to sleep). Press the button again.";
    else if (/IRSA search failed|502|Proxy Error/i.test(message))
      message =
        "NASA’s SPHEREx archive (IRSA) is not answering right now, so new images cannot be fetched. Objects with a green dot are saved on this site and still open. Technical detail: " +
        message.slice(0, 300);
    showError(e.plain ? message : "Could not build the tracker: " + message);
    $("#empty").hidden = false;
  } finally {
    if (id === state.makeId) {
      $("#status").hidden = true;
      $("#go").disabled = false;
    }
  }
}

// Fetch the finished tracker's description and set up the player and summary numbers.
async function loadTracker() {
  const mv = (state.mv = await api(`/api/jobs/${state.job}/tracker`)),
    n = mv.frames.length,
    first = mv.frames[0],
    last = mv.frames[n - 1];
  state.track = null;
  state.gaia = null;
  state.click = null;
  state.k = 0;

  const origin = $("#origin");
  origin.className = mv.origin === "demo" ? "warn" : "ok";
  origin.textContent =
    mv.origin === "demo"
      ? "Simulated demo. These images are made up, not SPHEREx observations."
      : mv.origin === "saved"
        ? `Real SPHEREx images, saved on this site${mv.saved_utc ? ` on ${mv.saved_utc} UTC` : ""}. Tick “Get the newest images from NASA” under More options to refresh them.`
        : `Real SPHEREx images, fetched just now from NASA’s archive: ${n} of ${mv.n_found} available${mv.n_skipped ? `, ${mv.n_skipped} skipped` : ""}.`;

  $("#metrics").innerHTML = [
    [n, "Images"],
    [fmt(last.mjd - first.mjd, 0) + " days", `${first.time_utc.slice(0, 10)} → ${last.time_utc.slice(0, 10)}`],
    [fmt(mv.native_pixel_arcsec) + "″", "SPHEREx pixel"],
    [`${fmt(mv.center.ra, 4)}°, ${fmt(mv.center.dec, 4)}°`, "Patch centre"],
  ]
    .map((m) => `<div><b>${esc(m[0])}</b><span>${esc(m[1])}</span></div>`)
    .join("");
  $("#tsrc").textContent = mv.target.source ? `${mv.target.name}: ${mv.target.source}` : "";
  // Opened from a link or a saved copy: show in the form what is on screen.
  if (mv.origin !== "demo" && !$("#ra").value.trim()) {
    const t = mv.target,
      round = (v) => Math.round(v * 1e6) / 1e6;
    fillForm({ name: t.name, ra: round(t.ra), dec: round(t.dec), epoch: t.epoch, pmra: t.pmra, pmdec: t.pmdec, parallax: t.parallax });
  }
  $("#bandwarn").hidden = mv.bands.length < 2;
  const bandOptions =
    `<option value="">All detectors</option>` +
    mv.bands.map((b) => `<option value="${b}">${b.slice(-2)}</option>`).join("");
  $("#v_band").innerHTML = $("#cmp_band").innerHTML = bandOptions;
  $("#v_band").disabled = $("#cmp_band").disabled = mv.bands.length < 2;
  $("#seek").max = n - 1;
  $("#seek").value = 0;
  $("#gif").href = `/api/jobs/${state.job}/tracker.gif`;
  $("#csv").href = `/api/jobs/${state.job}/track.csv`;
  $("#c_meas").checked = false;
  $("#ov_note").textContent = "";
  $("#logbook").hidden = true;
  showTab("movie");
  $("#result").hidden = false;
  updateShare();
  await loadImages();
  loadLogbook();
  if ($("#c_gaia").checked) toggleGaia();
}

// The observer's log: the server's plain-language summary of what was measured.
async function loadLogbook() {
  const job = state.job;
  try {
    const log = await api(`/api/jobs/${job}/logbook`);
    if (job !== state.job) return;
    $("#log_verdict").textContent = log.verdict;
    $("#log_lines").innerHTML = log.lines
      .map((line) => `<dt>${esc(line.label)}</dt><dd>${esc(line.text)}</dd>`)
      .join("");
    $("#logbook").hidden = false;
  } catch (e) {
    $("#logbook").hidden = true; // the log is a summary; the tabs still hold every number
  }
}

// What the tracker is asked to show: the view, how many neighbours to add, and the stretch.
function viewParams() {
  return new URLSearchParams({
    stretch: $("#c_each").checked ? "frame" : "global",
    view: $("#v_view").value,
    window: $("#v_window").value,
  });
}

// Download every image so playback is smooth.
async function loadImages() {
  const query = viewParams(),
    n = state.mv.frames.length,
    id = ++state.loadId,
    job = state.job;
  let done = 0;
  $("#status").hidden = false;
  const imgs = await Promise.all(
    state.mv.frames.map(
      (f) =>
        new Promise((resolve) => {
          const im = new Image();
          im.onload = im.onerror = () => {
            if (id === state.loadId) setProgress(++done / n, `Loading images ${done}/${n}`);
            resolve(im);
          };
          im.src = `/api/jobs/${job}/frames/${f.k}.png?${query}`;
        }),
    ),
  );
  if (id !== state.loadId) return; // a newer load started meanwhile
  state.imgs = imgs;
  $("#status").hidden = true;
  draw();
}

// ===== The player: draws one image at a time on the canvas =====

// Sky offset (arcsec East, North of the patch centre) -> canvas position.
function canvasPos(east, north) {
  const g = state.mv.grid,
    s = $("#cv").width / g.nx;
  return [
    ((g.nx - 1) / 2 - east / g.scale_arcsec + 0.5) * s,
    (g.ny - 1 - ((g.ny - 1) / 2 + north / g.scale_arcsec) + 0.5) * s,
  ];
}

// Draw the current image, position markers, the year, compass and scale bar.
function draw() {
  const mv = state.mv;
  if (!mv) return;
  const c = $("#cv"),
    x = c.getContext("2d"),
    W = c.width,
    f = mv.frames[state.k],
    im = state.imgs[state.k];
  x.fillStyle = "#000";
  x.fillRect(0, 0, W, W);
  x.imageSmoothingEnabled = $("#c_smooth").checked;
  // Plate view shows a photographic negative: dark stars on a light sky, the way astronomers
  // inspected glass plates. Difference views are already coloured, so they are not inverted.
  const plate = $("#c_neg").checked && $("#v_view").value === "image",
    ink = plate ? COLOR.ink : COLOR.text;
  x.filter =
    `brightness(${$("#v_bright").value}%) contrast(${$("#v_contrast").value}%)` +
    (plate ? " invert(1)" : "");
  if (im && im.naturalWidth) x.drawImage(im, 0, 0, W, W);
  x.filter = "none";

  if (state.gaia && $("#c_gaia").checked) {
    const year = frameYear(f);
    x.strokeStyle = COLOR.gaia;
    x.lineWidth = 1.5;
    for (const star of state.gaia.sources) {
      const dt = year - state.gaia.epoch,
        p = canvasPos(star.e + star.pm_e * dt, star.n + star.pm_n * dt);
      x.beginPath();
      x.arc(p[0], p[1], 9, 0, 6.2832);
      x.stroke();
    }
  }
  if ($("#c_pred").checked) {
    x.strokeStyle = COLOR.targetFaint;
    x.lineWidth = 1.5;
    x.setLineDash([4, 4]);
    x.beginPath();
    mv.frames.forEach((q, i) => {
      const p = canvasPos(...q.pred);
      i ? x.lineTo(...p) : x.moveTo(...p);
    });
    x.stroke();
    x.setLineDash([]);
    const p = canvasPos(...f.pred);
    x.strokeStyle = COLOR.target;
    x.lineWidth = 2;
    x.beginPath();
    x.arc(p[0], p[1], 15, 0, 6.2832);
    x.stroke();
  }
  if ($("#c_meas").checked && state.track) {
    const row = state.track.rows[state.k];
    if (row && row.meas_e != null) {
      const p = canvasPos(row.meas_e, row.meas_n);
      x.strokeStyle = COLOR.red;
      x.lineWidth = 2;
      x.beginPath();
      x.moveTo(p[0] - 8, p[1] - 8);
      x.lineTo(p[0] + 8, p[1] + 8);
      x.moveTo(p[0] + 8, p[1] - 8);
      x.lineTo(p[0] - 8, p[1] + 8);
      x.stroke();
    }
  }
  if (state.click) {
    const p = canvasPos(state.click.east, state.click.north);
    x.strokeStyle = COLOR.cyan;
    x.lineWidth = 2;
    x.strokeRect(p[0] - 10, p[1] - 10, 20, 20);
  }

  x.fillStyle = COLOR.readout;
  x.fillRect(8, 8, 330, 78);
  x.textBaseline = "alphabetic";
  x.textAlign = "left";
  x.fillStyle = "#fff";
  x.font = `700 44px ${FONT}`;
  x.fillText(f.time_utc.slice(0, 4), 18, 56);
  x.fillStyle = COLOR.target;
  x.font = `600 16px ${FONT}`;
  x.fillText(frameLabel(f), 18, 78);
  x.fillStyle = ink;
  x.fillText("N ↑   E ←", W - 104, 26);
  const bar = (30 / mv.grid.scale_arcsec) * (W / mv.grid.nx);
  x.fillRect(12, W - 22, bar, 3);
  x.fillText("30″", 12, W - 30);
  x.textAlign = "right";
  x.fillText(`image ${state.k + 1} of ${mv.frames.length}`, W - 12, W - 16);
  x.textAlign = "left";

  $("#seek").value = state.k;
  // Put the image into words for screen-reader users. The live region speaks only when the
  // visitor steps by hand, not during playback.
  const words = `Image ${state.k + 1} of ${mv.frames.length}, taken ${frameLabel(f)}.`;
  c.setAttribute("aria-label", "Tracker image. " + words);
  if (!state.timer) $("#frame_text").textContent = words;
}

// May image k be shown, given the detector chosen under "Show"?
const playable = (k) => !$("#v_band").value || state.mv.frames[k].band === $("#v_band").value;

// Move d images forward (+1) or back (-1), skipping detectors that are filtered out.
function advance(d) {
  const n = state.mv.frames.length;
  for (let tries = 0; tries < n; tries++) {
    state.k = (state.k + d + n) % n;
    if (playable(state.k)) break;
  }
  draw();
}

function play() {
  if (!state.mv) return;
  stop();
  state.timer = setInterval(() => advance(1), 1000 / +$("#fps").value);
  $("#play").textContent = "❚❚ Pause";
}

function stop() {
  clearInterval(state.timer);
  state.timer = null;
  $("#play").textContent = "▶ Play";
}

// Step by hand; this pauses playback.
function nudge(d) {
  if (!state.mv) return;
  stop();
  advance(d);
}

// Keyboard control of the tracker: Space plays or pauses, arrow keys step one image.
function trackerKey(e) {
  if (!state.mv) return;
  if (e.key === " ") state.timer ? stop() : play();
  else if (e.key === "ArrowRight") nudge(1);
  else if (e.key === "ArrowLeft") nudge(-1);
  else return;
  e.preventDefault();
}

// Fetch the Gaia stars the first time the box is ticked, then redraw.
async function toggleGaia() {
  const box = $("#c_gaia"),
    note = $("#ov_note"),
    job = state.job;
  if (box.checked && !state.gaia && job) {
    note.textContent = "Fetching Gaia stars…";
    try {
      const stars = await api(`/api/jobs/${job}/overlay/gaia`);
      if (job !== state.job) return;
      state.gaia = stars;
      note.textContent = `${stars.sources.length} Gaia stars in this patch.`;
    } catch (e) {
      box.checked = false;
      note.textContent = "Gaia stars could not be fetched right now. Try again in a minute.";
    }
  }
  draw();
  updateShare();
}

// ===== Result tabs: each one loads its data when opened =====

const TABS = ["movie", "cmp", "track", "spec", "dq", "more"];

function showTab(name) {
  // Only the open tab is in the keyboard's Tab order; arrow keys move between tabs.
  document.querySelectorAll(".tabs button[data-tab]").forEach((b) => {
    b.setAttribute("aria-selected", b.dataset.tab === name);
    b.tabIndex = b.dataset.tab === name ? 0 : -1;
  });
  TABS.forEach((t) => ($("#tab-" + t).hidden = t !== name));
  if (name !== "movie") stop();
  if (name !== "cmp") blinkStop();
  const loader = { cmp: loadCompare, track: loadTrack, spec: loadSpectrum, dq: loadQuality, more: loadMore }[name];
  if (loader && state.job) loader().catch((e) => showError(e.message));
}

// Arrow keys move along the row of tabs, as screen-reader users expect.
function tabKey(e) {
  const tabs = [...document.querySelectorAll(".tabs button[data-tab]")],
    i = tabs.indexOf(document.activeElement);
  if (i < 0) return;
  const j = { ArrowRight: (i + 1) % tabs.length, ArrowLeft: (i - 1 + tabs.length) % tabs.length, Home: 0, End: tabs.length - 1 }[e.key];
  if (j === undefined) return;
  e.preventDefault();
  showTab(tabs[j].dataset.tab);
  tabs[j].focus();
}

// Measured positions and brightness for every image (fetched once per tracker).
async function getTrack() {
  const job = state.job;
  if (!state.track) {
    const track = await api(`/api/jobs/${job}/track`);
    if (job === state.job) state.track = track;
    return track;
  }
  return state.track;
}

// ---- Compare epochs: early stack, late stack, their difference, and the blink comparator ----

const blink = { sides: null, side: "early", timer: null };

function blinkShow(side) {
  if (!blink.sides) return;
  blink.side = side;
  $("#blink_img").src = blink.sides[side].src;
  $("#blink_tag").textContent = blink.sides[side].tag;
}

function blinkStop() {
  clearInterval(blink.timer);
  blink.timer = null;
  $("#blink_go").textContent = "Start blinking";
}

function blinkToggle() {
  if (blink.timer) return blinkStop();
  if (!blink.sides) return;
  $("#blink_go").textContent = "Stop blinking";
  blink.timer = setInterval(
    () => blinkShow(blink.side === "early" ? "late" : "early"),
    +$("#blink_speed").value,
  );
}

async function loadCompare() {
  const split = +$("#cmp_split").value / 100,
    band = $("#cmp_band").value,
    query = `split=${split}` + (band ? `&band=${encodeURIComponent(band)}` : ""),
    base = `/api/jobs/${state.job}/compare`;
  try {
    const m = await api(`${base}/meta?${query}`),
      day = (text) => text.slice(0, 10);
    $("#cmp_info").textContent =
      `Early stack: ${m.n_early} images (${day(m.early_from)} to ${day(m.early_to)}). ` +
      `Late stack: ${m.n_late} images (${day(m.late_from)} to ${day(m.late_to)}).`;
    ["early", "late", "diff"].forEach((part) => ($("#cmp_" + part).src = `${base}/${part}.png?${query}`));
    blink.sides = {
      early: { src: `${base}/early.png?${query}`, tag: `Early: ${day(m.early_from)} to ${day(m.early_to)}` },
      late: { src: `${base}/late.png?${query}`, tag: `Late: ${day(m.late_from)} to ${day(m.late_to)}` },
    };
    blinkShow("early");
  } catch (e) {
    $("#cmp_info").textContent = e.message;
  }
}

// ---- Motion: where the object was found in each image, and how bright ----

async function loadTrack() {
  const T = await getTrack(),
    found = T.rows.filter((r) => r.ok),
    target = state.mv.target,
    moving = !!(target.pmra || target.pmdec),
    line = $("#pmline");
  if (!found.length) {
    line.className = "warn";
    line.textContent =
      "The object was not detected clearly in any image. Try a longer-wavelength detector or a larger field, and check the coordinates.";
    $("#plot1").innerHTML = $("#plot2").innerHTML = "";
    return;
  }
  line.className = "ok";
  line.textContent = T.fit
    ? `Measured motion: ${fmt(T.fit.pmra_masyr, 0)} ± ${fmt(T.fit.pmra_err, 0)} mas/yr East, ${fmt(T.fit.pmdec_masyr, 0)} ± ${fmt(T.fit.pmdec_err, 0)} mas/yr North` +
      (moving ? ` (catalogue: ${fmt(T.catalogue_pm.pmra, 0)}, ${fmt(T.catalogue_pm.pmdec, 0)})` : "") +
      `. Used ${T.fit.n_used} images.`
    : "Too few detections or too short a time span to measure motion (needs 5 or more detections over 60 days).";
  const marker = {
    color: found.map((r) => r.wavelength_um),
    colorscale: "Turbo",
    colorbar: { title: "µm" },
    size: 7,
  };
  chart(
    "plot1",
    [{ x: found.map((r) => r.time_utc), y: found.map((r) => r.offset_arcsec), mode: "markers", marker }],
    {
      title: moving ? "Does the object sit where the catalogue says?" : "Does the object stay where you pointed?",
      yaxis: { title: "Measured − expected (arcsec)", rangemode: "tozero" },
      shapes: [
        {
          type: "line",
          xref: "paper",
          x0: 0,
          x1: 1,
          y0: T.native_pixel_arcsec,
          y1: T.native_pixel_arcsec,
          line: { dash: "dot", color: COLOR.muted },
        },
      ],
      annotations: [
        {
          xref: "paper",
          x: 1,
          y: T.native_pixel_arcsec,
          text: "one SPHEREx pixel",
          showarrow: false,
          yanchor: "bottom",
          xanchor: "right",
          font: { size: 12, color: COLOR.muted },
        },
      ],
    },
  );
  chart(
    "plot2",
    [
      {
        x: found.map((r) => r.time_utc),
        y: found.map((r) => r.flux_mjy),
        error_y: { type: "data", array: found.map((r) => r.flux_err_mjy), color: "#6d7f96" },
        mode: "markers",
        marker,
      },
    ],
    { title: "Brightness over time", yaxis: { title: "Brightness (mJy, approximate)" } },
  );
}

// ---- Spectrum: brightness against wavelength, for the tracked object or a clicked spot ----

async function loadSpectrum() {
  const rows = (state.click ? state.click.rows : (await getTrack()).rows).filter(
      (r) => r.wavelength_um != null && r.flux_mjy != null,
    ),
    note = $("#spnote");
  note.innerHTML = state.click
    ? `Spectrum at the spot you clicked (${fmt(state.click.east, 1)}″ East, ${fmt(state.click.north, 1)}″ North; RA ${fmt(state.click.ra, 5)}°, Dec ${fmt(state.click.dec, 5)}°). <button class="quiet" id="sp_reset">Show the tracked object instead</button>`
    : `Spectrum of ${esc(state.mv.target.name)}.`;
  const reset = $("#sp_reset");
  if (reset)
    reset.onclick = () => {
      state.click = null;
      draw();
      loadSpectrum();
    };
  if (rows.length < 3) {
    $("#sp1").innerHTML = $("#sp2").innerHTML = "";
    note.textContent += " Too few usable measurements here for a spectrum.";
    return;
  }
  // Shade SPHEREx's six detector bands behind the points.
  const bands = Object.entries(state.cfg.bands),
    shapes = bands.map(([, [lo, hi]], i) => ({
      type: "rect",
      xref: "x",
      yref: "paper",
      x0: lo,
      x1: hi,
      y0: 0,
      y1: 1,
      fillcolor: i % 2 ? "rgba(255,225,163,.06)" : "rgba(168,216,255,.07)",
      line: { width: 0 },
      layer: "below",
    })),
    annotations = bands.map(([band, [lo, hi]]) => ({
      x: (lo + hi) / 2,
      y: 1,
      yref: "paper",
      text: band.slice(-2),
      showarrow: false,
      yanchor: "bottom",
      font: { size: 11, color: COLOR.muted },
    }));
  chart(
    "sp1",
    [
      {
        x: rows.map((r) => r.wavelength_um),
        y: rows.map((r) => r.flux_mjy),
        error_y: { type: "data", array: rows.map((r) => r.flux_err_mjy), color: "#6d7f96" },
        mode: "markers",
        marker: {
          size: 8,
          color: rows.map((r, i) => i),
          colorscale: "Plasma",
          colorbar: { title: "Image<br>(time order)" },
          symbol: rows.map((r) => (r.ok ? "circle" : "circle-open")),
        },
        text: rows.map((r) => r.time_utc.slice(0, 16) + " UTC"),
        hoverinfo: "text+x+y",
      },
    ],
    {
      title: "Measured SPHEREx spectrum (open symbols: no clear detection)",
      height: 380,
      shapes,
      annotations,
      xaxis: { title: "Wavelength (µm)", range: [0.7, 5.1] },
      yaxis: { title: "Brightness (mJy, approximate)" },
    },
  );
  chart(
    "sp2",
    [
      {
        x: rows.map((r) => r.time_utc),
        y: rows.map((r) => r.wavelength_um),
        mode: "markers",
        marker: {
          symbol: "square",
          size: 11,
          color: rows.map((r) => r.flux_mjy),
          colorscale: "Viridis",
          colorbar: { title: "mJy" },
        },
        text: rows.map(
          (r) => `${r.time_utc.slice(0, 16)} UTC<br>${fmt(r.wavelength_um, 2)} µm<br>${fmt(r.flux_mjy, 3)} mJy`,
        ),
        hoverinfo: "text",
      },
    ],
    {
      title: "When each wavelength was observed, coloured by brightness",
      height: 340,
      xaxis: { title: "Observation time (UTC)" },
      yaxis: { title: "Wavelength (µm)" },
    },
  );
}

// ---- Data quality: one row of details and flags per image ----

async function loadQuality() {
  const mv = state.mv;
  $("#dq_table").innerHTML = table(
    [
      ["k", "#", (k) => k + 1],
      ["time_utc", "Time (UTC)", (t) => esc(t.slice(0, 19))],
      ["band", "Detector", (b) => esc(b.slice(-2))],
      ["wavelength_um", "λ (µm)", (v) => fmt(v, 2)],
      ["release", "Release", (v) => esc(String(v || "").toUpperCase())],
      ["valid_pct", "Valid %", (v) => fmt(v, 0)],
      ["masked_pct", "Masked %", (v) => fmt(v, 1)],
      ["flags", "Flags at target", (v) => esc((v || []).join(", ") || "none")],
    ],
    mv.frames,
    (row) => ` data-k="${row.k}"`,
  );
  $("#dq_skipped").textContent = mv.n_skipped
    ? `${mv.n_skipped} image${mv.n_skipped > 1 ? "s were" : " was"} skipped: ${[...new Set(mv.skipped)].slice(0, 3).join("; ")}.`
    : "";
}

// ---- Other tools: links for the same patch, and data credits ----

async function loadMore() {
  const c = state.mv.center,
    L = await api(`/api/links?ra=${c.ra}&dec=${c.dec}&fov_arcmin=${state.mv.fov_arcmin || 5}`),
    groups = [...new Set(L.links.map((l) => l.group))];
  $("#xlinks").innerHTML = groups
    .map(
      (group) =>
        `<div class="xl"><h4>${esc(group)}</h4>${L.links
          .filter((l) => l.group === group)
          .map(
            (l) =>
              `<a href="${esc(l.url)}" target="_blank" rel="noopener">${esc(l.name)}</a><p>${esc(l.why)}</p>`,
          )
          .join("")}</div>`,
    )
    .join("");
  $("#xcredits").innerHTML = L.credits.map((text) => `<li>${esc(text)}</li>`).join("");
}

// ===== Sharing: the address reopens the same tracker, displayed the same way =====

// On/off display choices that travel in the address: [name in the address, checkbox].
const DISPLAY_FLAGS = [
  ["plate", "#c_neg"],
  ["gaia", "#c_gaia"],
];

function shareUrl() {
  const b = state.lastBody;
  if (!b) return "";
  const p = new URLSearchParams();
  if (b.source === "demo") p.set("demo", "1");
  else if (b.saved_id) p.set("saved", b.saved_id);
  else {
    p.set("ra", b.ra);
    p.set("dec", b.dec);
    p.set("epoch", b.epoch);
    p.set("fov", b.fov_arcmin);
    if (b.name) p.set("name", b.name);
    if (b.pmra || b.pmdec) {
      p.set("pmra", b.pmra || 0);
      p.set("pmdec", b.pmdec || 0);
    }
    if (b.bands.length) p.set("bands", b.bands.map((band) => band.slice(-2)).join(","));
    if (b.source === "live") p.set("live", "1");
  }
  if ($("#v_view").value !== "image") p.set("view", $("#v_view").value);
  if ($("#v_window").value !== "1") p.set("window", $("#v_window").value);
  if ($("#fps").value !== "4") p.set("speed", $("#fps").value);
  DISPLAY_FLAGS.forEach(([name, id]) => $(id).checked && p.set(name, "1"));
  return location.origin + location.pathname + "?" + p;
}

function updateShare() {
  const url = shareUrl();
  $("#share").hidden = !url || !state.mv;
  if (!url || !state.mv) return;
  $("#share_url").value = url;
  $("#share_embed").value =
    `<iframe src="${url}&embed=1" width="100%" height="900" style="border:0" loading="lazy" title="Skyface Space Tracker"></iframe>`;
  history.replaceState(null, "", url);
}

// If the address names a patch (?ra=..&dec=.., ?saved=.., ?obj=.., ?demo=1), open it at once.
function bootFromUrl() {
  const q = new URLSearchParams(location.search);
  if (q.get("embed")) document.body.classList.add("embed");
  const pick = (id, value) => {
    if (value && [...$(id).options].some((o) => o.value === value)) $(id).value = value;
  };
  pick("#v_view", q.get("view"));
  pick("#v_window", q.get("window"));
  pick("#fps", q.get("speed"));
  DISPLAY_FLAGS.forEach(([name, id]) => ($(id).checked = q.get(name) === "1"));
  document.body.classList.toggle("plate-view", $("#c_neg").checked);

  const object = q.get("obj") && testObject(q.get("obj"));
  if (object) return leaveStart(), trackObject(object);
  if (q.get("saved")) return leaveStart(), make({ source: "auto", saved_id: q.get("saved") });
  if (q.get("demo")) return leaveStart(), make({ source: "demo" });
  if (!(q.get("ra") && q.get("dec"))) return;
  leaveStart();
  const number = (key) => (q.get(key) == null || q.get(key) === "" ? null : Number(q.get(key)));
  fillForm({
    name: q.get("name") || "",
    ra: q.get("ra"),
    dec: q.get("dec"),
    epoch: number("epoch") ?? 2000,
    pmra: number("pmra"),
    pmdec: number("pmdec"),
  });
  if (q.get("fov")) {
    $("#fov").value = q.get("fov");
    $("#fovv").textContent = $("#fov").value;
  }
  if (q.get("bands")) {
    const want = q
      .get("bands")
      .split(",")
      .map((b) => "SPHEREx-" + b.trim().toUpperCase());
    document.querySelectorAll("#bands input").forEach((box) => (box.checked = want.includes(box.value)));
  }
  $("#live").checked = q.get("live") === "1";
  if (q.get("autorun") !== "0") make();
}

// ===== Start screen: three ways in =====

const leaveStart = () => $("#welcome").classList.add("gone");

const startMessage = (text) => {
  $("#start_msg").hidden = !text;
  $("#start_msg").textContent = text || "";
};

// Open one of the challenge objects: its saved images if the site has them, else live.
function trackObject(object) {
  fillForm(object);
  if (object.saved && !$("#live").checked) make({ source: "auto", saved_id: object.id });
  else make();
}

// ===== Wiring: connect buttons and sliders to the functions above, then start =====

$("#start_name_form").onsubmit = async (e) => {
  e.preventDefault();
  const name = $("#start_name").value.trim(),
    button = e.target.querySelector("button");
  if (!name) return startMessage("Type a star’s name first, for example Barnard’s Star.");
  startMessage("");
  button.disabled = true;
  try {
    await lookupName(name);
    leaveStart();
    make();
  } catch (x) {
    startMessage(`Could not find “${name}”: ${x.message}`);
  } finally {
    button.disabled = false;
  }
};
$("#start_coords_form").onsubmit = (e) => {
  e.preventDefault();
  const ra = $("#start_ra").value.trim(),
    dec = $("#start_dec").value.trim();
  if (!(ra && dec)) return startMessage("Enter both a right ascension and a declination.");
  startMessage("");
  state.lookup = null;
  $("#name").value = "";
  $("#ra").value = ra;
  $("#dec").value = dec;
  setDemo(false);
  leaveStart();
  make();
};
$("#start_objects").onclick = (e) => {
  const button = e.target.closest("button[data-obj]");
  if (!button) return;
  leaveStart();
  trackObject(testObject(button.dataset.obj));
};
$("#ex").onclick = (e) => {
  const button = e.target.closest("button[data-obj]");
  if (button) trackObject(testObject(button.dataset.obj));
};
$("#demo").onclick = () => {
  leaveStart();
  make({ source: "demo" });
};
$("#home").onclick = () => {
  stop();
  startMessage("");
  $("#welcome").classList.remove("gone");
  $("#start_name").focus();
};
$("#name_go").onclick = async () => {
  const name = $("#name").value.trim();
  if (!name) return;
  showError("");
  $("#name_go").disabled = true;
  try {
    await lookupName(name);
  } catch (x) {
    showError(`Could not find “${name}”: ${x.message}`);
  } finally {
    $("#name_go").disabled = false;
  }
};
$("#name").onkeydown = (e) => e.key === "Enter" && $("#name_go").click();
$("#go").onclick = () => make();
$("#s_demo").onchange = (e) => setDemo(e.target.checked);
// Typing coordinates by hand also means real images, not the demo.
$("#ra").oninput = $("#dec").oninput = () => setDemo(false);
$("#fov").oninput = (e) => ($("#fovv").textContent = e.target.value);
$("#maxf").oninput = (e) => ($("#maxv").textContent = e.target.value);

$("#play").onclick = () => (state.timer ? stop() : play());
$("#prev").onclick = () => nudge(-1);
$("#next").onclick = () => nudge(1);
$("#seek").oninput = (e) => {
  stop();
  state.k = +e.target.value;
  draw();
};
$("#fps").onchange = () => {
  if (state.timer) play();
  updateShare();
};
$("#cv").addEventListener("keydown", trackerKey);
["c_pred", "c_smooth"].forEach((id) => ($("#" + id).onchange = draw));
["v_bright", "v_contrast"].forEach((id) => ($("#" + id).oninput = draw));
["c_each", "v_view", "v_window"].forEach(
  (id) =>
    ($("#" + id).onchange = () => {
      if (!state.mv) return;
      updateShare();
      loadImages();
    }),
);
$("#v_band").onchange = () => {
  if (state.mv && !playable(state.k)) advance(1);
};
$("#c_neg").onchange = () => {
  document.body.classList.toggle("plate-view", $("#c_neg").checked);
  draw();
  updateShare();
};
$("#c_gaia").onchange = toggleGaia;
$("#c_meas").onchange = async (e) => {
  if (e.target.checked) {
    try {
      await getTrack();
    } catch (x) {
      showError(x.message);
      e.target.checked = false;
    }
  }
  draw();
};
// Click the image: extract the spectrum at that spot.
$("#cv").onclick = async (e) => {
  if (!state.mv) return;
  const g = state.mv.grid,
    box = e.target.getBoundingClientRect(),
    s = box.width / g.nx,
    px = (e.clientX - box.left) / s - 0.5,
    py = g.ny - 1 - ((e.clientY - box.top) / s - 0.5),
    east = ((g.nx - 1) / 2 - px) * g.scale_arcsec,
    north = (py - (g.ny - 1) / 2) * g.scale_arcsec,
    job = state.job;
  try {
    const R = await api(`/api/jobs/${job}/spectrum?east=${east.toFixed(2)}&north=${north.toFixed(2)}`);
    if (job !== state.job) return;
    state.click = { east, north, ra: R.ra, dec: R.dec, rows: R.rows };
    draw();
    showTab("spec");
  } catch (x) {
    showError("Could not extract a spectrum there: " + x.message);
  }
};

document.querySelectorAll(".tabs button[data-tab]").forEach((b) => (b.onclick = () => showTab(b.dataset.tab)));
document.querySelector("#result .tabs").addEventListener("keydown", tabKey);
$("#cmp_split").oninput = (e) => ($("#cmp_sv").textContent = e.target.value);
$("#cmp_split").onchange = $("#cmp_band").onchange = () => state.job && loadCompare();
$("#blink_go").onclick = blinkToggle;
$("#blink_speed").onchange = () => {
  if (blink.timer) {
    blinkStop();
    blinkToggle();
  }
};
$("#blink_img").onclick = () => blinkShow(blink.side === "early" ? "late" : "early");
$("#blink_img").onkeydown = (e) => {
  if (e.key === "Enter" || e.key === " ") {
    e.preventDefault();
    $("#blink_img").click();
  }
};
$("#dq_table").onclick = (e) => {
  const row = e.target.closest("tr[data-k]");
  if (!row) return;
  state.k = +row.dataset.k;
  $("#v_band").value = "";
  showTab("movie");
  draw();
};
["share_url", "share_embed"].forEach((id) => ($("#" + id).onfocus = (e) => e.target.select()));

init()
  .then(bootFromUrl)
  .catch((e) => {
    startMessage("Could not reach the site’s server: " + e.message);
    showError("Could not reach the site’s server: " + e.message);
  });
