#!/usr/bin/env node
/* Headless-Chrome smoke test of the Tracks UI: every analysed session page
   must render without JS errors, and the map, charts, tables and tiles must
   agree with the API.

     BASE=http://127.0.0.1:8731 SHOTS=/tmp/shots node scripts/e2e_pages.js

   Needs `npm i puppeteer-core` somewhere on NODE_PATH and a Chrome binary
   (CHROME=/usr/bin/google-chrome by default). Credentials come from .env. */
const fs = require("fs");
const path = require("path");
const puppeteer = require("puppeteer-core");

const ROOT = path.resolve(__dirname, "..");
const BASE = process.env.BASE || "http://127.0.0.1:8731";
const SHOTS = process.env.SHOTS || "";
const env = Object.fromEntries(
  fs.readFileSync(path.join(ROOT, ".env"), "utf8").split("\n")
    .filter((l) => l.includes("=") && !l.trim().startsWith("#"))
    .map((l) => [l.slice(0, l.indexOf("=")).trim(), l.slice(l.indexOf("=") + 1).trim()]));
const AUTH = { username: env.APP_USERNAME, password: env.APP_PASSWORD };
const authHeader = "Basic " + Buffer.from(AUTH.username + ":" + AUTH.password).toString("base64");

async function api(p) {
  const r = await fetch(BASE + p, { headers: { Authorization: authHeader } });
  if (!r.ok) throw new Error(p + " -> " + r.status);
  return r.json();
}

function watch(page, errors) {
  page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
  page.on("console", (m) => { if (m.type() === "error" && !/Failed to load resource/.test(m.text())) errors.push("console: " + m.text()); });
  page.on("response", (r) => { if (r.url().startsWith(BASE) && r.status() >= 400) errors.push("HTTP " + r.status() + " " + r.url()); });
}

(async () => {
  const browser = await puppeteer.launch({ executablePath: process.env.CHROME || "/usr/bin/google-chrome", headless: true, args: ["--no-sandbox"] });
  const tracks = await api("/api/tracks");
  let failures = 0;
  const report = (name, errs, extra) => {
    const ok = errs.length === 0;
    if (!ok) failures++;
    console.log((ok ? "PASS " : "FAIL ") + name + (extra ? "  " + extra : ""));
    errs.slice(0, 8).forEach((e) => console.log("      " + e));
  };

  for (const t of tracks) {
    const page = await browser.newPage();
    await page.setViewport({ width: 1440, height: 1000 });
    await page.authenticate(AUTH);
    const errors = [];
    watch(page, errors);
    await page.goto(BASE + "/#track/" + t.id, { waitUntil: "networkidle2", timeout: 60000 });
    await page.waitForSelector(".tk-tiles", { timeout: 60000 }).catch(() => errors.push("no tiles rendered"));
    await new Promise((r) => setTimeout(r, 600));
    const a = await api("/api/tracks/" + t.id + "/analysis");
    const dom = await page.evaluate(() => {
      const tiles = {};
      document.querySelectorAll(".tk-tile").forEach((x) => { tiles[x.querySelector(".l").textContent] = x.querySelector(".v").textContent; });
      const panels = {};
      document.querySelectorAll(".panel").forEach((p) => {
        const h = p.querySelector("h2, summary");
        if (h) panels[h.textContent] = p.querySelectorAll("tbody tr").length;
      });
      return {
        tiles, panels,
        uplots: document.querySelectorAll(".tk-charts .uplot").length,
        manMarkers: document.querySelectorAll(".leaflet-marker-pane .tk-man-icon").length,
        canvases: document.querySelectorAll(".leaflet-overlay-pane canvas").length,
        polar: !!document.querySelector('.tk-svg[aria-label="Session polar diagram"] path[stroke]'),
        err: Array.from(document.querySelectorAll(".tk-msg.err")).map((e) => e.textContent),
      };
    });
    const s = a.summary;
    if (dom.err.length) errors.push("error message on page: " + dom.err.join("; "));
    if (dom.uplots < 4) errors.push("expected >=4 timeline charts, got " + dom.uplots);
    if (!dom.canvases) errors.push("map track not drawn");
    if (!dom.polar) errors.push("polar not drawn");
    if (dom.tiles["Tacks"] !== String(s.tacks)) errors.push("tile tacks " + dom.tiles["Tacks"] + " != api " + s.tacks);
    if (dom.tiles["Gybes"] !== String(s.gybes)) errors.push("tile gybes " + dom.tiles["Gybes"] + " != api " + s.gybes);
    const manRows = dom.panels["Maneuvers"];
    if (manRows !== Math.max(1, a.maneuvers.length)) errors.push("maneuver table rows " + manRows + " != " + a.maneuvers.length);
    if (dom.manMarkers !== a.maneuvers.length) errors.push("map maneuver markers " + dom.manMarkers + " != " + a.maneuvers.length);
    if (dom.panels["Legs"] !== Math.max(1, a.legs.length)) errors.push("legs rows " + dom.panels["Legs"] + " != " + a.legs.length);
    if (s.tacks + s.gybes !== a.maneuvers.length) errors.push("summary counts disagree with maneuver list");

    // interactions: click a maneuver row -> charts zoom + map highlight; play the replay briefly
    if (a.maneuvers.length) {
      await page.evaluate(() => {
        const p = Array.from(document.querySelectorAll(".panel")).find((x) => x.querySelector("h2") && x.querySelector("h2").textContent === "Maneuvers");
        p.querySelector("tbody tr.clickable").click();
      });
      await new Promise((r) => setTimeout(r, 400));
      const zoomed = await page.evaluate(() => {
        const sel = document.querySelector('.tk-readout b');
        return { readout: sel ? sel.textContent : null, slider: +document.querySelector(".tk-scrub input[type=range]").value };
      });
      if (!zoomed.slider) errors.push("maneuver click did not move the replay");
    }
    await page.click(".tk-scrub .tk-btn.primary");
    await new Promise((r) => setTimeout(r, 700));
    await page.click(".tk-scrub .tk-btn.primary");
    for (const mode of ["VMG", "Tack"]) {
      await page.evaluate((m) => { Array.from(document.querySelectorAll(".tk-seg button")).find((b) => b.textContent === m).click(); }, mode);
    }
    if (SHOTS) {
      fs.mkdirSync(SHOTS, { recursive: true });
      await page.screenshot({ path: path.join(SHOTS, "track-" + t.id + ".png"), fullPage: true });
    }
    report("session #" + t.id + " " + t.start_date + " " + (t.title || ""), errors,
      `${a.maneuvers.length} maneuvers, ${a.legs.length} legs, ${dom.uplots} charts`);
    await page.close();
  }

  const views = ["#tracks", "#tracks/progress", "#tracks/polar", "#tracks/map", "#tracks/gear", "#tracks/ai",
    "#compare/" + tracks.slice(0, 3).map((t) => t.id).join(","), ""];
  for (const v of views) {
    const page = await browser.newPage();
    await page.setViewport({ width: 1280, height: 900 });
    await page.authenticate(AUTH);
    const errors = [];
    watch(page, errors);
    await page.goto(BASE + "/" + v, { waitUntil: "networkidle2", timeout: 60000 });
    await new Promise((r) => setTimeout(r, 1200));
    const info = await page.evaluate(() => ({
      err: Array.from(document.querySelectorAll("#tracksRoot .tk-msg.err")).map((e) => e.textContent),
      rows: document.querySelectorAll("#tracksRoot tbody tr").length,
      charts: document.querySelectorAll("#tracksRoot .uplot, #tracksRoot .tk-svg").length,
      cells: document.querySelectorAll("#tracksRoot .leaflet-overlay-pane canvas").length,
    }));
    if (info.err.length) errors.push(info.err.join("; "));
    if (v === "#tracks" && info.rows < tracks.length) errors.push("list shows " + info.rows + " rows");
    if (v === "#tracks/progress" && info.charts < 6) errors.push("progress charts " + info.charts);
    if (v === "") {
      // Garmin Sessions tab: Analyse buttons exist for water sessions
      await page.click('.tab-btn[data-tab="sessions"]');
      await new Promise((r) => setTimeout(r, 1500));
      const n = await page.evaluate(() => document.querySelectorAll(".tk-analyse").length);
      const open = await page.evaluate(() => Array.from(document.querySelectorAll(".tk-analyse")).filter((b) => b.textContent === "Open").length);
      const water = await api("/api/sessions?limit=500").then((rows) => rows.filter((s) =>
        /windsurf|kiteboard|kitesurf|wingfoil|wing foil|windfoil|sailing|foil/.test((s.activity_type + " " + (s.activity_name || "")).toLowerCase())).length);
      if (water && !n) errors.push("no Analyse buttons on the Sessions tab");
      info.analyse = n + " analyse buttons, " + open + " open";
    }
    if (SHOTS) await page.screenshot({ path: path.join(SHOTS, "view-" + (v.replace(/[#/,]/g, "_") || "home") + ".png"), fullPage: true });
    report("view " + (v || "(home → Sessions)"), errors, JSON.stringify(info));
    await page.close();
  }
  await browser.close();
  console.log(failures ? `\n${failures} page(s) failed` : "\nall pages OK");
  process.exit(failures ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(2); });
