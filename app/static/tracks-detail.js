/* Session analysis -- the single-session page: map replay, synced charts,
   maneuver / leg tables, polar, HR, wind, race, settings. */
(function () {
  "use strict";

  var TK = window.TK, el = TK.el;
  var D = null; // state of the page currently shown

  TK.destroyDetail = function () {
    if (!D) return;
    D.playing = false;
    if (D.raf) cancelAnimationFrame(D.raf);
    TK.destroyCharts(D.charts);
    TK.destroyCharts(D.extraCharts);
    if (D.map) D.map.remove();
    TK.untip();
    D = null;
  };

  TK.renderDetail = function (root, id) {
    TK.destroyDetail();
    root.textContent = "";
    root.appendChild(el("p", { class: "tk-msg", text: "Analysing session…" }));
    return Promise.all([TK.api("/api/tracks/" + id + "/analysis"), TK.api("/api/tracks/options")])
      .then(function (res) {
        var scroll = window.scrollY;
        TK.destroyDetail();
        build(root, res[0], res[1]);
        window.scrollTo(0, scroll);
      })
      .catch(function (e) {
        root.textContent = "";
        root.appendChild(el("a", { class: "tk-back", href: "#tracks", text: "← All tracks" }));
        root.appendChild(el("p", { class: "tk-msg err", text: "Couldn't load this session: " + e.message }));
      });
  };

  function reload() { return TK.renderDetail(D.root, D.id); }

  function patch(body, msgEl) {
    if (msgEl) { msgEl.textContent = "Saving and re-analysing…"; msgEl.className = "tk-msg"; }
    return TK.api("/api/tracks/" + D.id, { method: "PATCH", body: body })
      .then(reload)
      .catch(function (e) { if (msgEl) { msgEl.textContent = e.message; msgEl.className = "tk-msg err"; } else alert(e.message); });
  }

  // ================================================================== build
  function build(root, A, OPTS) {
    var S = A.series, n = S.t.length, tr = A.track;
    var off = tr.utc_offset_s || 0, t0 = A.summary.start_utc;
    D = {
      root: root, id: tr.id, A: A, OPTS: OPTS, n: n, off: off,
      X: S.t.map(function (t) { return t0 + t + off; }),
      charts: [], extraCharts: [], idx: 0, playing: false, mode: "speed", hl: null,
      stateName: A.summary.state_name === "foiling" ? "Foiling" : "Planing",
    };
    root.textContent = "";
    document.body.classList.add("wide");

    root.appendChild(header(A));
    (A.warnings || []).forEach(function (w) { root.appendChild(el("div", { class: "tk-warn", role: "status", text: w })); });
    root.appendChild(tiles(A));

    var grid = el("div", { class: "tk-grid2", id: "tkTop" });
    var mapPanel = TK.panel(null, null, []);
    var chartPanel = TK.panel(null, null, []);
    grid.appendChild(mapPanel);
    grid.appendChild(chartPanel);
    root.appendChild(grid);
    root.appendChild(el("div", { style: { height: "24px" } }));

    buildMap(mapPanel);
    buildCharts(chartPanel);
    if (OPTS.sail_sports.indexOf(tr.sport) >= 0) root.appendChild(sailingPanel());
    root.appendChild(maneuverPanel());
    root.appendChild(legsPanel());
    var g2 = el("div", { class: "tk-grid-even" });
    g2.appendChild(splitsPanel());
    g2.appendChild(posPanel());
    root.appendChild(g2);
    var g3 = el("div", { class: "tk-grid-even" });
    g3.appendChild(polarPanel());
    g3.appendChild(gainPanel());
    root.appendChild(g3);
    var g4 = el("div", { class: "tk-grid-even" });
    g4.appendChild(windPanel());
    g4.appendChild(statePanel());
    if (A.hr) g4.appendChild(hrPanel());
    root.appendChild(g4);
    root.appendChild(racePanel());
    root.appendChild(infoPanel());
    root.appendChild(settingsPanel());
    setScrub(0);
    fitAll();
  }

  // ================================================================== header + tiles
  function header(A) {
    var tr = A.track, s = A.summary;
    var h = el("div", { class: "tk-head" });
    var left = el("div");
    left.appendChild(el("a", { class: "tk-back", href: "#tracks", text: "← All tracks" }));
    left.appendChild(el("h2", { text: tr.title || "Session" }));
    var when = tr.start_date + " · " + TK.clock(s.start_utc, tr.utc_offset_s) + "–" + TK.clock(s.end_utc, tr.utc_offset_s);
    left.appendChild(el("div", { class: "meta", text: [when, tr.spot, TK.SPORT_LABEL[tr.sport] || tr.sport,
      (tr.gear || []).map(function (g) { return g.code || g.name; }).join(", ")].filter(Boolean).join(" · ") }));
    h.appendChild(left);
    var actions = el("div", { class: "actions" });
    actions.appendChild(el("a", { class: "tk-btn", href: "#tracks?compare=" + tr.id, text: "Compare…" }));
    actions.appendChild(el("a", { class: "tk-btn", href: "/api/tracks/" + tr.id + "/file", text: "Download file" }));
    actions.appendChild(el("button", { class: "tk-btn", type: "button", text: "Re-analyse", onclick: function () {
      TK.api("/api/tracks/" + D.id + "/analysis?force=true").then(reload);
    } }));
    actions.appendChild(el("button", { class: "tk-btn danger", type: "button", text: "Delete", onclick: function () {
      if (!confirm("Delete this session and its analysis? The Garmin activity itself is not touched.")) return;
      TK.api("/api/tracks/" + D.id, { method: "DELETE" }).then(function () { location.hash = "#tracks"; });
    } }));
    h.appendChild(actions);
    return h;
  }

  function tile(v, unit, label, detail) {
    var t = el("div", { class: "tk-tile" });
    var vv = el("span", { class: "v", text: v });
    if (unit && v !== "—") vv.appendChild(el("small", { text: unit }));
    t.appendChild(vv);
    t.appendChild(el("span", { class: "l", text: label }));
    if (detail) t.appendChild(el("span", { class: "d", text: detail }));
    return t;
  }

  function tiles(A) {
    var s = A.summary, c = A.track.conditions || {};
    var wrap = el("div", { class: "tk-tiles", role: "list" });
    [
      tile(TK.km(s.distance_m), "km", "Distance", TK.hm(s.moving_s) + " moving"),
      tile(TK.dur(s.duration_s), "", "Duration", "avg " + TK.kn(s.avg_moving_kn) + " kn moving"),
      tile(TK.kn(s.best_2s_kn), "kn", "Top speed (2 s)", "10 s: " + TK.kn(s.best_10s_kn) + " kn"),
      tile(TK.kn(s.best_500m_kn), "kn", "Best 500 m", "1 nm: " + TK.kn(s.best_1nm_kn) + " kn"),
      tile(TK.num(s.state_pct, 0), "%", D.stateName + " (of moving)", s.touchdowns + " touchdowns · " + s.flights + " flights"),
      tile(String(s.tacks), "", "Tacks", s.tack_success_pct === null ? "" : TK.num(s.tack_success_pct, 0) + "% " + D.stateName.toLowerCase() + " · " + TK.num(s.tack_loss_m, 0) + " m lost"),
      tile(String(s.gybes), "", "Gybes", s.gybe_success_pct === null ? "" : TK.num(s.gybe_success_pct, 0) + "% " + D.stateName.toLowerCase() + " · " + TK.num(s.gybe_loss_m, 0) + " m lost"),
      tile(TK.kn(s.upwind_vmg_kn), "kn", "Upwind VMG", "at " + TK.num(s.upwind_twa, 0) + "° TWA · " + TK.kn(s.upwind_kn) + " kn"),
      tile(TK.kn(s.downwind_vmg_kn), "kn", "Downwind VMG", "at " + TK.num(s.downwind_twa, 0) + "° TWA · " + TK.kn(s.downwind_kn) + " kn"),
      tile(TK.deg(s.wind_deg), "", "Wind (" + s.wind_source + ", " + s.wind_confidence + ")",
        c.wind_kn !== undefined && c.wind_kn !== null ? TK.num(c.wind_kn, 0) + " kn, gusts " + TK.num(c.gust_kn, 0) : "no conditions"),
      A.hr ? tile(TK.num(s.hr_avg, 0), "bpm", "Avg heart rate", "max " + TK.num(s.hr_max, 0)) : null,
    ].forEach(function (t) { if (t) { t.setAttribute("role", "listitem"); wrap.appendChild(t); } });
    return wrap;
  }

  // ================================================================== map
  var MODES = [["speed", "Speed"], ["vmg", "VMG"], ["state", "Foiling"], ["side", "Tack"]];

  function buildMap(panel) {
    var A = D.A, S = A.series;
    var tools = el("div", { class: "tk-map-tools" });
    var seg = el("div", { class: "tk-seg", role: "group", "aria-label": "Colour track by" });
    MODES.forEach(function (m) {
      var label = m[0] === "state" ? D.stateName : m[1];
      var b = el("button", { type: "button", text: label, "aria-pressed": m[0] === D.mode ? "true" : "false", onclick: function () {
        D.mode = m[0];
        seg.querySelectorAll("button").forEach(function (x) { x.setAttribute("aria-pressed", x === b ? "true" : "false"); });
        drawTrack();
      } });
      seg.appendChild(b);
    });
    tools.appendChild(seg);
    var toggles = el("div", { style: { display: "flex", gap: "6px" } });
    var manBtn = el("button", { class: "tk-btn", type: "button", "aria-pressed": "true", text: "Maneuvers", onclick: function () {
      var on = manBtn.getAttribute("aria-pressed") !== "true";
      manBtn.setAttribute("aria-pressed", on ? "true" : "false");
      if (on) D.manLayer.addTo(D.map); else D.manLayer.remove();
    } });
    var heatBtn = el("button", { class: "tk-btn", type: "button", "aria-pressed": "false", text: "Speed heat", onclick: function () {
      var on = heatBtn.getAttribute("aria-pressed") !== "true";
      heatBtn.setAttribute("aria-pressed", on ? "true" : "false");
      if (on) D.heatLayer.addTo(D.map); else D.heatLayer.remove();
    } });
    var fitBtn = el("button", { class: "tk-btn", type: "button", text: "Reset view", onclick: fitAll });
    toggles.appendChild(manBtn);
    toggles.appendChild(heatBtn);
    toggles.appendChild(fitBtn);
    tools.appendChild(toggles);
    panel.appendChild(tools);

    var mapDiv = el("div", { class: "tk-map", role: "application", "aria-label": "Track map" });
    panel.appendChild(mapDiv);
    D.legendEl = el("div");
    panel.appendChild(D.legendEl);

    // scrubber
    var sc = el("div", { class: "tk-scrub" });
    D.playBtn = el("button", { class: "tk-btn primary", type: "button", text: "▶ Play", "aria-label": "Play replay", onclick: togglePlay });
    D.speedSel = el("select", { "aria-label": "Replay speed", class: "tk-btn" });
    [1, 5, 10, 30, 60, 120].forEach(function (v) { D.speedSel.appendChild(el("option", { value: v, text: v + "×", selected: v === 30 })); });
    D.slider = el("input", { type: "range", min: 0, max: D.n - 1, value: 0, step: 1, "aria-label": "Replay position" });
    D.slider.addEventListener("input", function () { setScrub(+D.slider.value); });
    D.clockEl = el("span", { class: "clock" });
    sc.appendChild(D.playBtn);
    sc.appendChild(D.speedSel);
    sc.appendChild(D.slider);
    sc.appendChild(D.clockEl);
    panel.appendChild(sc);
    D.readout = el("div", { class: "tk-readout", "aria-live": "off" });
    panel.appendChild(D.readout);

    var map = (D.map = L.map(mapDiv, { preferCanvas: true, zoomSnap: 0.25 }));
    var dark = matchMedia("(prefers-color-scheme: dark)").matches && document.documentElement.dataset.theme !== "light";
    var base = TK.grayTiles(dark);
    var osm = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 19, attribution: "© OpenStreetMap contributors" });
    var sat = L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
      { maxZoom: 19, attribution: "Imagery © Esri" });
    base.addTo(map);
    L.control.layers({ "Gray": base, "OpenStreetMap": osm, "Satellite": sat }, null, { position: "topleft" }).addTo(map);
    L.control.scale({ imperial: false }).addTo(map);

    D.trackLayer = L.layerGroup().addTo(map);
    D.hlLayer = L.layerGroup().addTo(map);
    D.manLayer = L.layerGroup().addTo(map);
    D.raceLayer = L.layerGroup().addTo(map);
    D.heatLayer = heatLayer(A.speed_grid);
    drawTrack();
    drawManeuvers();
    drawRace();

    D.ghost = L.circleMarker([S.lat[0], S.lon[0]], { radius: 6, color: TK.css("--track-ink"), weight: 2, fill: false, interactive: false });
    D.boat = L.marker([S.lat[0], S.lon[0]], {
      icon: L.divIcon({ className: "tk-boat", iconSize: null,
        html: '<svg viewBox="-11 -11 22 22"><path d="M0,-10 L6,7 L0,3 L-6,7 Z" fill="' + TK.css("--accent") + '" stroke="' + TK.css("--surface") + '" stroke-width="2" stroke-linejoin="round"/></svg>' }),
      keyboard: false, interactive: false, zIndexOffset: 1000,
    }).addTo(map);

    var WindCtl = L.Control.extend({
      onAdd: function () {
        var d = L.DomUtil.create("div", "tk-wind-ctl");
        d.innerHTML = '<svg viewBox="-15 -15 30 30" aria-hidden="true"><path d="M0,-12 L0,10 M-5,4 L0,11 L5,4" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/></svg><span></span>';
        D.windCtl = d;
        return d;
      },
    });
    new WindCtl({ position: "topright" }).addTo(map);

    map.on("click", function (e) {
      if (D.placing) { addMark(e.latlng); return; }
      var best = -1, bd = Infinity, p0 = map.latLngToContainerPoint(e.latlng);
      for (var i = 0; i < D.n; i += 1) {
        var p = map.latLngToContainerPoint([S.lat[i], S.lon[i]]);
        var d = (p.x - p0.x) * (p.x - p0.x) + (p.y - p0.y) * (p.y - p0.y);
        if (d < bd) { bd = d; best = i; }
      }
      if (best >= 0 && bd < 900) setScrub(best);
    });
  }

  function classesFor(mode) {
    var S = D.A.series, n = D.n, cls = new Array(n), colors, legend;
    if (mode === "speed") {
      var vmax = Math.max(10, Math.ceil(TK.percentile(S.kn, 0.995) / 5) * 5);
      for (var i = 0; i < n; i++) cls[i] = TK.speedClass(S.kn[i], vmax);
      colors = []; for (var k = 0; k < TK.speedClasses; k++) colors.push(TK.speedColor(k));
      legend = rampLegend(colors, "0", vmax + " kn", "Speed");
    } else if (mode === "vmg") {
      var lim = Math.max(5, TK.percentile(S.vmg.map(Math.abs), 0.95));
      for (i = 0; i < n; i++) cls[i] = TK.divClass(S.vmg[i], lim);
      colors = [0, 1, 2, 3, 4, 5, 6].map(TK.divColor);
      legend = rampLegend(colors, "−" + Math.round(lim) + " kn downwind", "+" + Math.round(lim) + " kn upwind", "VMG");
    } else if (mode === "state") {
      for (i = 0; i < n; i++) cls[i] = S.on[i] ? 0 : 1;
      colors = [TK.css("--series-1"), TK.css("--series-2")];
      legend = TK.legend([{ label: D.stateName, color: colors[0] }, { label: "Not " + D.stateName.toLowerCase(), color: colors[1] }]);
    } else {
      var slow = D.A.settings.stop_kn;
      for (i = 0; i < n; i++) cls[i] = S.kn[i] < slow ? 2 : S.twa[i] > 0 ? 0 : 1;
      colors = [TK.css("--series-1"), TK.css("--series-2"), TK.css("--div-mid")];
      legend = TK.legend([{ label: "Port tack", color: colors[0] }, { label: "Starboard tack", color: colors[1] }, { label: "Stopped", color: colors[2] }]);
    }
    return { cls: cls, colors: colors, legend: legend };
  }

  function rampLegend(colors, lo, hi, title) {
    var lg = el("div", { class: "tk-legend" });
    lg.appendChild(el("span", { text: title + ":" }));
    lg.appendChild(el("span", { text: lo }));
    var r = el("span", { class: "ramp", "aria-hidden": "true" });
    colors.forEach(function (c) { r.appendChild(el("i", { style: { background: c } })); });
    lg.appendChild(r);
    lg.appendChild(el("span", { text: hi }));
    lg.appendChild(el("span", { class: "sym", text: "  ▲" }));
    lg.appendChild(el("span", { text: "tack" }));
    lg.appendChild(el("span", { class: "sym", text: "●" }));
    lg.appendChild(el("span", { text: "gybe (hollow = touched down)" }));
    lg.appendChild(el("span", { class: "sym", style: { color: TK.css("--accent") }, text: "○" }));
    lg.appendChild(el("span", { text: "mark rounding" }));
    return lg;
  }

  function drawTrack() {
    var S = D.A.series, c = classesFor(D.mode);
    D.trackLayer.clearLayers();
    var start = 0;
    function flush(endIncl, k) {
      if (endIncl - start < 1) return;
      var pts = [];
      for (var j = start; j <= endIncl; j++) pts.push([S.lat[j], S.lon[j]]);
      L.polyline(pts, { color: c.colors[k], weight: 3, opacity: 0.95, lineCap: "round", lineJoin: "round", interactive: false }).addTo(D.trackLayer);
    }
    for (var i = 1; i < D.n; i++) {
      if (S.gap[i]) { flush(i - 1, c.cls[start]); start = i; continue; }
      if (c.cls[i] !== c.cls[start]) { flush(i, c.cls[start]); start = i; }
    }
    flush(D.n - 1, c.cls[start]);
    D.legendEl.textContent = "";
    D.legendEl.appendChild(c.legend);
  }

  function manIcon(m) {
    var ink = TK.css("--track-ink"), surf = TK.css("--surface");
    var fill = m.success ? ink : surf;
    var shape = m.type === "tack"
      ? '<path d="M0,-6 L5.5,4.5 L-5.5,4.5 Z" fill="' + fill + '" stroke="' + ink + '" stroke-width="2" stroke-linejoin="round"/>'
      : '<circle r="4.5" fill="' + fill + '" stroke="' + ink + '" stroke-width="2"/>';
    return L.divIcon({ className: "tk-man-icon", iconSize: [16, 16], iconAnchor: [8, 8],
      html: '<svg viewBox="-8 -8 16 16" width="16" height="16"><circle r="7.5" fill="' + surf + '" opacity=".7"/>' + shape + "</svg>" });
  }

  function drawManeuvers() {
    D.manLayer.clearLayers();
    D.A.maneuvers.forEach(function (m) {
      var mk = L.marker([m.lat, m.lon], { icon: manIcon(m), keyboard: false, title: m.type + " #" + m.id });
      mk.bindTooltip(
        TK.esc(m.type[0].toUpperCase() + m.type.slice(1)) + " #" + m.id + " · " + TK.clock(m.tc, D.off) +
        "<br>" + TK.kn(m.entry_kn) + " → " + TK.kn(m.min_kn) + " → " + TK.kn(m.exit_kn) + " kn · lost " + TK.num(m.distance_lost_m, 0) + " m" +
        "<br>" + (m.success ? D.stateName + " throughout" : "Touched down"), { direction: "top" });
      mk.on("click", function () { focusManeuver(m); });
      mk.addTo(D.manLayer);
    });
  }

  function heatLayer(grid) {
    var g = L.layerGroup();
    if (!grid || !grid.cells.length) return g;
    var vmax = Math.max(10, Math.ceil(TK.percentile(grid.cells.map(function (c) { return c[3] / c[2]; }), 0.98) / 5) * 5);
    grid.cells.forEach(function (c) {
      var mean = c[3] / c[2];
      var b = [[c[0] * grid.dlat, c[1] * grid.dlon], [(c[0] + 1) * grid.dlat, (c[1] + 1) * grid.dlon]];
      L.rectangle(b, { stroke: false, fillColor: TK.speedColor(TK.speedClass(mean, vmax)), fillOpacity: 0.75 })
        .bindTooltip(mean.toFixed(1) + " kn avg · " + c[4].toFixed(1) + " kn max · " + Math.round(c[2]) + " s")
        .addTo(g);
    });
    return g;
  }

  function drawRace() {
    D.raceLayer.clearLayers();
    var marks = D.A.settings.marks || [];
    var ink = TK.css("--track-ink");
    marks.forEach(function (mk, i) {
      L.circle([mk.lat, mk.lon], { radius: D.A.settings.mark_radius_m, color: ink, weight: 1, fill: false, interactive: false }).addTo(D.raceLayer);
      L.marker([mk.lat, mk.lon], { icon: L.divIcon({ className: "tk-man-icon", iconSize: [18, 18], iconAnchor: [9, 9],
        html: '<svg viewBox="-9 -9 18 18" width="18" height="18"><path d="M0,-8 L8,0 L0,8 L-8,0 Z" fill="' + TK.css("--accent") + '" stroke="' + TK.css("--surface") + '" stroke-width="2"/></svg>' }) })
        .bindTooltip(TK.esc(mk.name || "M" + (i + 1)), { permanent: true, direction: "right", offset: [8, 0] })
        .addTo(D.raceLayer);
    });
    if (!marks.length && D.A.race && D.A.race.roundings) {
      D.A.race.roundings.forEach(function (r) {
        L.circleMarker([r.lat, r.lon], { radius: 5, color: TK.css("--accent"), weight: 2, fillOpacity: 0.2 })
          .bindTooltip(TK.esc(r.name) + " rounding · " + TK.clock(r.t, D.off)).addTo(D.raceLayer);
      });
    }
  }

  // ================================================================== replay
  function setScrub(i, fromChart) {
    var S = D.A.series;
    i = Math.max(0, Math.min(D.n - 1, Math.round(i)));
    D.idx = i;
    D.slider.value = i;
    D.boat.setLatLng([S.lat[i], S.lon[i]]);
    var svg = D.boat.getElement() && D.boat.getElement().querySelector("svg");
    if (svg) svg.style.transform = "rotate(" + (S.hdg[i] || 0) + "deg)";
    D.clockEl.textContent = TK.clock(D.X[i], 0, true);
    renderReadout(i);
    if (D.windCtl) {
      var w = S.wind[i];
      // the arrow points down (blowing from the north) at 0 deg: rotate by where the wind comes from
      D.windCtl.querySelector("svg").style.transform = "rotate(" + w + "deg)";
      D.windCtl.querySelector("span").textContent = "Wind " + TK.deg(w);
    }
    if (!fromChart) redrawCharts();
  }

  function renderReadout(i) {
    var S = D.A.series;
    var twa = S.twa[i];
    var items = [
      [TK.kn(S.kn[i]), "kn speed"],
      [S.hdg[i] === null ? "—" : Math.round(S.hdg[i]) + "°", "heading"],
      [twa === null ? "—" : Math.abs(Math.round(twa)) + "° " + (twa > 0 ? "P" : "S"), "TWA"],
      [TK.kn(S.vmg[i]), "kn VMG"],
      [S.target_kn[i] === null ? "—" : Math.round((100 * S.kn[i]) / S.target_kn[i]) + "%", "of target " + TK.kn(S.target_kn[i])],
      [S.on[i] ? "yes" : "no", D.stateName.toLowerCase()],
      [S.hr[i] === null ? "—" : String(S.hr[i]), "bpm"],
      [TK.num(S.gain[i], 0), "m upwind"],
    ];
    D.readout.textContent = "";
    items.forEach(function (it) { D.readout.appendChild(el("div", null, [el("b", { text: it[0] }), el("span", { text: it[1] })])); });
  }

  var lastChartDraw = 0;
  function redrawCharts(force) {
    var now = performance.now();
    if (!force && D.playing && now - lastChartDraw < 120) return;
    lastChartDraw = now;
    D.charts.forEach(function (c) { c.redraw(false, false); });
  }

  function togglePlay() {
    D.playing = !D.playing;
    D.playBtn.textContent = D.playing ? "❚❚ Pause" : "▶ Play";
    D.playBtn.setAttribute("aria-label", D.playing ? "Pause replay" : "Play replay");
    if (!D.playing) { redrawCharts(true); return; }
    if (D.idx >= D.n - 1) setScrub(0);
    var last = performance.now(), tpos = D.X[D.idx];
    function step(now) {
      if (!D || !D.playing) return;
      var dt = (now - last) / 1000;
      last = now;
      tpos += dt * +D.speedSel.value;
      var i = D.idx;
      while (i < D.n - 1 && D.X[i + 1] <= tpos) i++;
      if (i >= D.n - 1) { setScrub(D.n - 1); togglePlay(); return; }
      if (i !== D.idx) {
        if (D.X[i] - tpos > 5) tpos = D.X[i]; // jump over recording gaps
        setScrub(i);
        var b = D.map.getBounds();
        if (!b.contains(D.boat.getLatLng())) D.map.panTo(D.boat.getLatLng(), { animate: false });
      }
      D.raf = requestAnimationFrame(step);
    }
    D.raf = requestAnimationFrame(step);
  }

  function fitAll() {
    var S = D.A.series, pts = [];
    for (var i = 0; i < D.n; i += 5) pts.push([S.lat[i], S.lon[i]]);
    D.map.fitBounds(pts, { padding: [16, 16] });
    D.hl = null;
    D.hlLayer.clearLayers();
    D.charts.forEach(function (c) { c.setScale("x", { min: D.X[0], max: D.X[D.n - 1] }); });
    redrawCharts(true);
  }

  function focusRange(i0, i1, iScrub) {
    var S = D.A.series;
    i0 = Math.max(0, i0); i1 = Math.min(D.n - 1, i1);
    var pts = [];
    for (var i = i0; i <= i1; i++) pts.push([S.lat[i], S.lon[i]]);
    D.hlLayer.clearLayers();
    if (pts.length > 1) {
      L.polyline(pts, { color: TK.css("--track-ink"), weight: 9, opacity: 0.25, lineCap: "round", interactive: false }).addTo(D.hlLayer);
      D.map.fitBounds(pts, { padding: [40, 40], maxZoom: 17 });
    }
    D.hl = [D.X[i0], D.X[i1]];
    var span = D.X[i1] - D.X[i0], pad = Math.max(15, span * 0.25);
    D.charts.forEach(function (c) { c.setScale("x", { min: D.X[i0] - pad, max: D.X[i1] + pad }); });
    setScrub(iScrub === undefined ? i0 : iScrub);
    redrawCharts(true);
    var top = document.getElementById("tkTop");
    if (top && top.getBoundingClientRect().top < -40) top.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function focusManeuver(m) { focusRange(m.i0 - 8, m.i_rec + 5, m.ic); }

  // ================================================================== charts
  function buildCharts(panel) {
    var A = D.A, S = A.series;
    panel.appendChild(el("h2", { text: "Timeline" }));
    panel.appendChild(el("p", { class: "panel-sub", text: "Drag across a chart to zoom all of them · double-click to reset · click to move the replay." }));
    var wrap = el("div", { class: "tk-charts" });
    panel.appendChild(wrap);
    function nulled(arr) {
      var out = arr.slice();
      for (var i = 0; i < D.n; i++) if (S.gap[i]) out[i] = null;
      return out;
    }
    var onRuns = [], st = -1;
    for (var i = 0; i < D.n; i++) {
      if (S.on[i] && st < 0) st = i;
      if ((!S.on[i] || S.gap[i]) && st >= 0) { onRuns.push([st, i - 1]); st = S.on[i] ? i : -1; }
    }
    if (st >= 0) onRuns.push([st, D.n - 1]);

    var c1 = TK.css("--series-1"), c2 = TK.css("--series-2"), c3 = TK.css("--series-3");
    addChart(wrap, "Speed (kn) — strip = " + D.stateName.toLowerCase(), [
      { label: "Speed", data: nulled(S.kn), color: c1, unit: " kn" },
    ], 170, { onRuns: onRuns, zero: true });
    addChart(wrap, "VMG (kn) — positive is upwind", [{ label: "VMG", data: nulled(S.vmg), color: c1, unit: " kn" }], 130, { zeroLine: true });
    addChart(wrap, "True wind angle (°) — positive is port tack", [{ label: "TWA", data: nulled(S.twa), color: c1, unit: "°", nd: 0 }], 130,
      { yRange: [-180, 180], zeroLine: true, bands: [A.settings.upwind_max_twa, A.settings.downwind_min_twa] });
    if (S.hr.some(function (v) { return v !== null; })) {
      addChart(wrap, "Heart rate (bpm)", [{ label: "HR", data: nulled(S.hr), color: c3, unit: " bpm", nd: 0 }], 110, {});
    }
    addChart(wrap, "Distance made to windward (m, cumulative)", [{ label: "Gain", data: nulled(S.gain), color: c1, unit: " m", nd: 0 }], 120, { zeroLine: true });
  }

  function addChart(wrap, title, defs, height, extra) {
    wrap.appendChild(el("div", { class: "tk-chart-title", text: title }));
    var box = el("div");
    wrap.appendChild(box);
    var ink = TK.css("--track-ink"), grid = TK.css("--grid"), hlFill = "rgba(0,147,168,0.12)", onColor = TK.css("--series-3");
    var axX = Object.assign(TK.axisStyle(), { size: 30, values: function (u, v) { return v.map(function (x) { return TK.clock(x, 0); }); } });
    var axY = Object.assign(TK.axisStyle(), { size: 46 });
    var opts = {
      width: box.clientWidth || 600, height: height,
      tzDate: function (ts) { return uPlot.tzDate(new Date(ts * 1e3), "Etc/UTC"); },
      scales: { x: { time: true }, y: extra.yRange ? { range: extra.yRange } : extra.zero ? { range: function (u, lo, hi) { return [0, Math.max(5, hi * 1.05)]; } } : {} },
      axes: [axX, axY],
      series: [{ value: function (u, v) { return v === null ? "—" : TK.clock(v, 0, true); } }].concat(defs.map(function (d) {
        return { label: d.label, stroke: d.color, width: d.width || 2, spanGaps: false, points: { show: false },
          value: function (u, v) { return v === null || v === undefined ? "—" : v.toFixed(d.nd === undefined ? 1 : d.nd) + (d.unit || ""); } };
      })),
      legend: { show: true, live: true },
      cursor: { sync: { key: "trk" + D.id, setSeries: false }, drag: { x: true, y: false, setScale: true }, points: { size: 7 } },
      hooks: {
        setScale: [function (u, key) {
          if (key !== "x" || D.syncing) return;
          D.syncing = true;
          D.charts.forEach(function (c) { if (c !== u) c.setScale("x", { min: u.scales.x.min, max: u.scales.x.max }); });
          D.syncing = false;
        }],
        setCursor: [function (u) {
          var i = u.cursor.idx;
          if (i === null || i === undefined || u.cursor.left < 0) { if (D.ghost) D.ghost.remove(); return; }
          var S = D.A.series;
          D.ghost.setLatLng([S.lat[i], S.lon[i]]);
          if (!D.map.hasLayer(D.ghost)) D.ghost.addTo(D.map);
        }],
        drawClear: [function (u) {
          var ctx = u.ctx, b = u.bbox;
          ctx.save();
          if (extra.bands) {
            // shade the reach band between upwind and downwind thresholds
            ctx.fillStyle = grid;
            ctx.globalAlpha = 0.45;
            [[extra.bands[0], extra.bands[1]], [-extra.bands[1], -extra.bands[0]]].forEach(function (r) {
              var y0 = u.valToPos(r[1], "y", true), y1 = u.valToPos(r[0], "y", true);
              ctx.fillRect(b.left, Math.min(y0, y1), b.width, Math.abs(y1 - y0));
            });
            ctx.globalAlpha = 1;
          }
          if (D.hl) {
            var a = u.valToPos(D.hl[0], "x", true), c = u.valToPos(D.hl[1], "x", true);
            ctx.fillStyle = hlFill;
            ctx.fillRect(Math.max(b.left, a), b.top, Math.min(b.left + b.width, c) - Math.max(b.left, a), b.height);
          }
          ctx.restore();
        }],
        draw: [function (u) {
          var ctx = u.ctx, b = u.bbox, dpr = devicePixelRatio || 1;
          ctx.save();
          ctx.beginPath();
          ctx.rect(b.left, b.top, b.width, b.height);
          ctx.clip();
          if (extra.zeroLine) {
            var zy = Math.round(u.valToPos(0, "y", true)) + 0.5;
            ctx.strokeStyle = TK.css("--axis");
            ctx.lineWidth = dpr;
            ctx.beginPath(); ctx.moveTo(b.left, zy); ctx.lineTo(b.left + b.width, zy); ctx.stroke();
          }
          if (extra.onRuns) {
            ctx.fillStyle = onColor;
            extra.onRuns.forEach(function (r) {
              var a = u.valToPos(D.X[r[0]], "x", true), c = u.valToPos(D.X[r[1]], "x", true);
              ctx.fillRect(a, b.top + b.height - 4 * dpr, Math.max(dpr, c - a), 4 * dpr);
            });
          }
          var sx = Math.round(u.valToPos(D.X[D.idx], "x", true)) + 0.5;
          ctx.strokeStyle = ink;
          ctx.lineWidth = 1.5 * dpr;
          ctx.beginPath(); ctx.moveTo(sx, b.top); ctx.lineTo(sx, b.top + b.height); ctx.stroke();
          ctx.restore();
        }],
      },
    };
    var chart = new uPlot(opts, [D.X].concat(defs.map(function (d) { return d.data; })), box);
    TK.fitWidth(chart, box, height);
    var downX = null;
    chart.over.addEventListener("mousedown", function (e) { downX = e.clientX; });
    chart.over.addEventListener("mouseup", function (e) {
      if (downX !== null && Math.abs(e.clientX - downX) < 4 && chart.cursor.idx !== null) setScrub(chart.cursor.idx);
      downX = null;
    });
    chart.over.addEventListener("dblclick", function () { D.hl = null; D.hlLayer.clearLayers(); });
    D.charts.push(chart);
    return chart;
  }

  // ================================================================== maneuvers
  function maneuverPanel() {
    var A = D.A, mans = A.maneuvers;
    var p = TK.panel("Maneuvers", mans.length + " detected — a tack turns through the wind, a gybe through dead downwind. Distance lost = over 10 s before to 20 s after the turn, what your steady VMG (average of before and after) would have made minus what you actually made. Roundings (upwind ↔ downwind) get no loss figure. Click a row or point to zoom.");
    var filter = "all";
    var seg = el("div", { class: "tk-seg", role: "group", "aria-label": "Show maneuvers" });
    [["all", "All"], ["tack", "Tacks"], ["gybe", "Gybes"]].forEach(function (f) {
      var b = el("button", { type: "button", text: f[1], "aria-pressed": f[0] === "all" ? "true" : "false", onclick: function () {
        filter = f[0];
        seg.querySelectorAll("button").forEach(function (x) { x.setAttribute("aria-pressed", x === b ? "true" : "false"); });
        render();
      } });
      seg.appendChild(b);
    });
    p.appendChild(seg);
    var grid = el("div", { class: "tk-grid-even", style: { marginTop: "12px", marginBottom: "0" } });
    var scatterBox = el("div"), statsBox = el("div");
    grid.appendChild(scatterBox);
    grid.appendChild(statsBox);
    p.appendChild(grid);
    var tableBox = el("div");
    p.appendChild(tableBox);
    var c1 = TK.css("--series-1"), c2 = TK.css("--series-2");
    var st = D.stateName.toLowerCase();
    var cols = [
      { key: "id", label: "#", num: true },
      { key: "tc", label: "Time", fmt: function (m) { return TK.clock(m.tc, D.off, true); } },
      { key: "type", label: "Type", fmt: function (m) { return m.type + (m.direction === "downwind" ? " (dn)" : m.direction === "rounding" ? " (rounding)" : ""); } },
      { key: "from_side", label: "From", fmt: function (m) { return m.from_side === "port" ? "port → stbd" : "stbd → port"; } },
      { key: "entry_kn", label: "Entry kn", num: true, fmt: function (m) { return TK.kn(m.entry_kn); } },
      { key: "min_kn", label: "Min kn", num: true, fmt: function (m) { return TK.kn(m.min_kn); } },
      { key: "exit_kn", label: "Exit kn", num: true, fmt: function (m) { return TK.kn(m.exit_kn); } },
      { key: "duration_s", label: "Turn s", num: true, fmt: function (m) { return TK.num(m.duration_s, 0); } },
      { key: "recovery_s", label: "Recover s", num: true, fmt: function (m) { return TK.num(m.recovery_s, 0) + (m.recovered ? "" : "+"); } },
      { key: "distance_lost_m", label: "Lost m", num: true, fmt: function (m) { return m.distance_lost_m === null ? "—" : TK.num(m.distance_lost_m, 0); } },
      { key: "entry_twa", label: "TWA in → out", num: true, fmt: function (m) { return TK.num(m.entry_twa, 0) + "° → " + TK.num(m.exit_twa, 0) + "°"; } },
      { key: "success", label: D.stateName, sort: function (m) { return m.success ? 1 : 0; }, fmt: function (m) {
        return el("span", { class: m.success ? "ok" : "bad", text: m.success ? "✓ stayed " + st : "✗ touched down" }); } },
      { key: "hr_max", label: "HR max", num: true, fmt: function (m) { return TK.num(m.hr_max, 0); } },
    ];
    var table = TK.table(cols, mans, { onRow: focusManeuver, maxHeight: "360px", empty: "No maneuvers detected." });
    tableBox.appendChild(table);

    function render() {
      var list = mans.filter(function (m) { return filter === "all" || m.type === filter; });
      scatterBox.textContent = "";
      scatterBox.appendChild(el("div", { class: "tk-chart-title", text: "Minimum speed vs distance lost" }));
      var withLoss = list.filter(function (m) { return m.distance_lost_m !== null; });
      if (withLoss.length) {
        TK.scatter(scatterBox, withLoss.map(function (m) {
          return { x: m.min_kn, y: m.distance_lost_m, color: m.type === "tack" ? c1 : c2, shape: m.type === "tack" ? "tri" : "dot",
            hollow: !m.success, data: m, title: m.type + " #" + m.id + " · " + TK.clock(m.tc, D.off),
            tip: [{ value: TK.kn(m.min_kn) + " kn", label: "min speed" }, { value: TK.num(m.distance_lost_m, 0) + " m", label: "lost" },
              { value: TK.num(m.duration_s, 0) + " s", label: "turn" }, { value: m.success ? "stayed " + st : "touched down" }] };
        }), { xLabel: "minimum speed (kn)", yLabel: "metres lost", xMin: 0, onClick: function (pt) { focusManeuver(pt.data); }, aria: "Maneuver minimum speed versus distance lost" });
        scatterBox.appendChild(TK.legend([{ label: "Tack", color: c1, shape: "dot" }, { label: "Gybe", color: c2, shape: "dot" }, { label: "hollow = touched down", color: TK.css("--muted-ink"), shape: "dot", hollow: true }]));
      } else scatterBox.appendChild(el("p", { class: "tk-hint", text: "None." }));
      statsBox.textContent = "";
      statsBox.appendChild(el("div", { class: "tk-chart-title", text: "Averages" }));
      var kv = el("dl", { class: "tk-kv" });
      ["tack", "gybe"].forEach(function (k) {
        var ms = mans.filter(function (m) { return m.type === k; });
        if (!ms.length) return;
        function avg(key) {
          var v = ms.filter(function (m) { return m[key] !== null && m[key] !== undefined; });
          return v.length ? v.reduce(function (a, m) { return a + m[key]; }, 0) / v.length : null;
        }
        var ok = ms.filter(function (m) { return m.success; }).length;
        var lbl = k === "tack" ? "Tacks" : "Gybes";
        kv.appendChild(el("dt", { text: lbl })); kv.appendChild(el("dd", { text: ms.length + " · " + Math.round((100 * ok) / ms.length) + "% " + st }));
        kv.appendChild(el("dt", { text: lbl + " entry → min → exit" })); kv.appendChild(el("dd", { text: TK.kn(avg("entry_kn")) + " → " + TK.kn(avg("min_kn")) + " → " + TK.kn(avg("exit_kn")) + " kn" }));
        kv.appendChild(el("dt", { text: lbl + " turn / recovery" })); kv.appendChild(el("dd", { text: TK.num(avg("duration_s"), 1) + " s / " + TK.num(avg("recovery_s"), 1) + " s" }));
        kv.appendChild(el("dt", { text: lbl + " distance lost" })); kv.appendChild(el("dd", { text: TK.num(avg("distance_lost_m"), 0) + " m" }));
        var best = ms.filter(function (m) { return m.distance_lost_m !== null; }).sort(function (a, b) { return a.distance_lost_m - b.distance_lost_m; })[0];
        if (best) { kv.appendChild(el("dt", { text: "Best " + k })); kv.appendChild(el("dd", { text: "#" + best.id + " · " + TK.num(best.distance_lost_m, 0) + " m" })); }
        var nr = ms.filter(function (m) { return m.direction === "rounding"; }).length;
        if (nr) { kv.appendChild(el("dt", { text: lbl + " that were roundings" })); kv.appendChild(el("dd", { text: nr + " (upwind ↔ downwind; no distance-lost figure)" })); }
      });
      statsBox.appendChild(kv);
      table.refresh(list);
    }
    render();
    return p;
  }

  // ================================================================== legs
  function focusLeg(lg) { focusRange(lg.i0, lg.i1); }

  function legsPanel() {
    var A = D.A;
    var p = TK.panel("Legs", "Steady sailing between maneuvers and stops, split when the point of sail changes. Gain is metres made along the wind axis (upwind +). Targets: " + targetText(A.targets) + ".");
    var cols = [
      { key: "id", label: "#", num: true },
      { key: "t0", label: "Start", fmt: function (l) { return TK.clock(l.t0, D.off, true); } },
      { key: "type", label: "Type" },
      { key: "side", label: "Tack" },
      { key: "duration_s", label: "Time", num: true, fmt: function (l) { return TK.dur(l.duration_s); } },
      { key: "distance_m", label: "Dist m", num: true, fmt: function (l) { return TK.num(l.distance_m, 0); } },
      { key: "avg_kn", label: "Avg kn", num: true, fmt: function (l) { return TK.kn(l.avg_kn); } },
      { key: "max_kn", label: "Max kn", num: true, fmt: function (l) { return TK.kn(l.max_kn); } },
      { key: "avg_twa", label: "TWA", num: true, fmt: function (l) { return TK.num(l.avg_twa, 0) + "°"; } },
      { key: "avg_vmg_kn", label: "VMG kn", num: true, fmt: function (l) { return TK.kn(l.avg_vmg_kn); } },
      { key: "gain_m", label: "Gain m", num: true, fmt: function (l) { return TK.num(l.gain_m, 0); } },
      { key: "pct_target_speed", label: "% tgt speed", num: true, fmt: function (l) { return TK.num(l.pct_target_speed, 0); } },
      { key: "pct_target_vmg", label: "% tgt VMG", num: true, fmt: function (l) { return TK.num(l.pct_target_vmg, 0); } },
      { key: "gain_vs_target_m", label: "vs tgt m", num: true, fmt: function (l) { return TK.num(l.gain_vs_target_m, 0); } },
      { key: "angle_vs_ref", label: "Δ angle", num: true, title: "Average TWA minus the best-VMG angle (negative = pointing higher)", fmt: function (l) { return l.angle_vs_ref === null ? "—" : (l.angle_vs_ref > 0 ? "+" : "") + TK.num(l.angle_vs_ref, 0) + "°"; } },
      { key: "state_pct", label: D.stateName + " %", num: true, fmt: function (l) { return TK.num(l.state_pct, 0); } },
      { key: "hr_avg", label: "HR", num: true, fmt: function (l) { return TK.num(l.hr_avg, 0); } },
    ];
    p.appendChild(TK.table(cols, A.legs, { onRow: focusLeg, maxHeight: "380px", empty: "No legs." }));
    return p;
  }

  function targetText(t) {
    if (!t || (!t.upwind_kn && !t.downwind_kn)) return "none yet";
    return "upwind " + TK.kn(t.upwind_kn) + " kn @ " + TK.num(t.upwind_twa, 0) + "° (VMG " + TK.kn(t.upwind_vmg_kn) + "), downwind " +
      TK.kn(t.downwind_kn) + " kn @ " + TK.num(t.downwind_twa, 0) + "° (VMG " + TK.kn(t.downwind_vmg_kn) + ") from " + t.source;
  }

  // ================================================================== splits + point of sail
  function splitsPanel() {
    var p = TK.panel("Best splits", "Highest average speed over each time or distance. Click to see where.");
    p.appendChild(TK.table([
      { key: "label", label: "Split", sortable: false },
      { key: "kn", label: "Speed kn", num: true, sortable: false, fmt: function (s) { return TK.kn(s.kn, 2); } },
      { key: "t0", label: "At", sortable: false, fmt: function (s) { return s.t0 ? TK.clock(s.t0, D.off, true) : "—"; } },
      { key: "duration_s", label: "Took", num: true, sortable: false, fmt: function (s) { return s.duration_s ? TK.dur(s.duration_s) : "—"; } },
    ], D.A.splits, { onRow: function (s) { if (s.kn) focusRange(s.i0, s.i1); } }));
    return p;
  }

  function posPanel() {
    var pos = D.A.point_of_sail;
    var p = TK.panel("Upwind / downwind × port / starboard", "Steady sailing only (maneuvers excluded). VMG is positive upwind.");
    var rows = [];
    [["upwind", "Upwind"], ["reach", "Reach"], ["downwind", "Downwind"], ["all", "All"]].forEach(function (k) {
      [["port", "port"], ["starboard", "stbd"], ["all", "both"]].forEach(function (s) {
        var b = pos[k[0]][s[0]];
        rows.push(Object.assign({ name: k[1] + " · " + s[1], strong: s[0] === "all" }, b));
      });
    });
    p.appendChild(TK.table([
      { key: "name", label: "", sortable: false },
      { key: "time_s", label: "Time", num: true, sortable: false, fmt: function (r) { return TK.dur(r.time_s); } },
      { key: "distance_m", label: "Dist km", num: true, sortable: false, fmt: function (r) { return TK.km(r.distance_m); } },
      { key: "avg_kn", label: "Avg kn", num: true, sortable: false, fmt: function (r) { return TK.kn(r.avg_kn); } },
      { key: "max_kn", label: "Max kn", num: true, sortable: false, fmt: function (r) { return TK.kn(r.max_kn); } },
      { key: "avg_vmg_kn", label: "VMG kn", num: true, sortable: false, fmt: function (r) { return TK.kn(r.avg_vmg_kn); } },
      { key: "avg_twa", label: "TWA", num: true, sortable: false, fmt: function (r) { return r.avg_twa === null ? "—" : TK.num(r.avg_twa, 0) + "°"; } },
      { key: "state_pct", label: D.stateName + " %", num: true, sortable: false, fmt: function (r) { return TK.num(r.state_pct, 0); } },
    ], rows, { rowClass: function (r) { return r.strong ? "sel" : null; } }));
    return p;
  }

  // ================================================================== polar + gain
  function polarPanel() {
    var A = D.A;
    var p = TK.panel("Polar", "Speed by true wind angle (5° bins, steady sailing). p90 is a strong-but-repeatable speed; the personal polar merges every " + (TK.SPORT_LABEL[A.track.sport] || "") + " session.");
    var box = el("div", { style: { maxWidth: "440px" } });
    p.appendChild(box);
    function pts(bins, key) { return bins.map(function (b) { return { twa: b.twa, kn: b[key] }; }); }
    var series = [{ label: "This session p90", color: TK.css("--series-1"), points: pts(A.polar.bins, "p90") },
      { label: "This session median", color: TK.css("--series-3"), points: pts(A.polar.bins, "p50"), width: 1.5 }];
    if (A.personal_polar) series.push({ label: "Personal polar p90", color: TK.css("--series-2"), points: pts(A.personal_polar.bins, "p90") });
    var bu = A.polar.best_upwind, bd = A.polar.best_downwind;
    TK.polarChart(box, series, { marks: [bu && { twa: bu.twa, kn: bu.kn, label: "best VMG " + TK.kn(bu.vmg_kn) }, bd && { twa: bd.twa, kn: bd.kn, label: "best VMG " + TK.kn(bd.vmg_kn) }], aria: "Session polar diagram" });
    var det = el("details");
    det.appendChild(el("summary", { class: "tk-hint", text: "Table view" }));
    det.appendChild(TK.table([
      { key: "twa", label: "TWA", num: true, fmt: function (b) { return b.twa - 2.5 + "–" + (b.twa + 2.5) + "°"; } },
      { key: "seconds", label: "Time s", num: true },
      { key: "mean", label: "Mean kn", num: true, fmt: function (b) { return TK.kn(b.mean); } },
      { key: "p50", label: "p50", num: true, fmt: function (b) { return TK.kn(b.p50); } },
      { key: "p90", label: "p90", num: true, fmt: function (b) { return TK.kn(b.p90); } },
      { key: "max", label: "Max", num: true, fmt: function (b) { return TK.kn(b.max); } },
    ], A.polar.bins.filter(function (b) { return b.seconds > 0; }), { maxHeight: "260px" }));
    p.appendChild(det);
    return p;
  }

  function gainPanel() {
    var A = D.A;
    var legs = A.legs.filter(function (l) { return l.gain_vs_target_m !== null && l.gain_vs_target_m !== undefined; });
    var p = TK.panel("Gain / loss vs target", "Per leg: metres gained (+) or lost (−) along the wind axis compared with sailing the target VMG for the same time. The timeline's last chart shows the cumulative distance made to windward.");
    if (!legs.length) { p.appendChild(el("p", { class: "tk-hint", text: "No upwind/downwind legs with a target yet." })); return p; }
    var pos = TK.css("--div-pos-2"), neg = TK.css("--div-neg-2");
    TK.bars(p, legs.map(function (l) {
      return { label: String(l.id), value: l.gain_vs_target_m, color: l.gain_vs_target_m >= 0 ? pos : neg, data: l,
        title: "Leg " + l.id + " · " + l.type + " " + l.side,
        tip: [{ value: TK.num(l.gain_vs_target_m, 0) + " m", label: "vs target" }, { value: TK.num(l.pct_target_vmg, 0) + " %", label: "of target VMG" },
          { value: (l.angle_vs_ref > 0 ? "+" : "") + TK.num(l.angle_vs_ref, 0) + "°", label: "vs best angle" }] };
    }), { onClick: function (b) { focusLeg(b.data); }, aria: "Gain versus target per leg" });
    var tot = legs.reduce(function (a, l) { return a + l.gain_vs_target_m; }, 0);
    var up = legs.filter(function (l) { return l.type === "upwind"; }).reduce(function (a, l) { return a + l.gain_vs_target_m; }, 0);
    p.appendChild(TK.legend([{ label: "gained on target", color: pos }, { label: "lost to target", color: neg }]));
    p.appendChild(el("p", { class: "tk-hint", text: "Total " + TK.num(tot, 0) + " m vs target (upwind legs " + TK.num(up, 0) + " m, downwind " + TK.num(tot - up, 0) + " m). Leg numbers match the legs table." }));
    return p;
  }

  // ================================================================== wind
  function windPanel() {
    var A = D.A, w = A.wind, cond = A.track.conditions || {};
    var p = TK.panel("Wind", null);
    var srcText = w.source === "auto" ? "Inferred from your headings" : w.source === "manual" ? "Set manually" : "Manual keyframes";
    p.appendChild(el("p", { class: "panel-sub", text: srcText + " — " + TK.deg(w.deg) + ", confidence " + w.confidence + (w.shift_range_deg ? ", shifts over " + w.shift_range_deg + "°" : "") + "." }));
    var row = el("div", { class: "tk-grid-even", style: { marginBottom: "0", gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))" } });
    var roseBox = el("div", { style: { maxWidth: "260px" } });
    roseBox.appendChild(el("div", { class: "tk-chart-title", text: "Time on each heading, arrow = wind" }));
    TK.rose(roseBox, w.rose_min, w.deg);
    row.appendChild(roseBox);
    var kv = el("dl", { class: "tk-kv" });
    function kvRow(k, v) { kv.appendChild(el("dt", { text: k })); kv.appendChild(el("dd", { text: v })); }
    kvRow("Auto estimate", TK.deg(w.auto_deg));
    if (cond.wind_dir !== undefined && cond.wind_dir !== null) kvRow("Conditions (" + (cond.source || "") + ")", TK.deg(cond.wind_dir) + " · " + TK.num(cond.wind_kn, 0) + " kn");
    if (w.modes && w.modes.upwind_modes) kvRow("Close-hauled headings", w.modes.upwind_modes.map(function (x) { return Math.round(x) + "°"; }).join(" / "));
    if (w.modes && w.modes.downwind_modes) kvRow("Downwind headings", w.modes.downwind_modes.map(function (x) { return Math.round(x) + "°"; }).join(" / "));
    row.appendChild(kv);
    p.appendChild(row);
    if (w.direction) {
      var names = { time: "more time spent upwind", speed: "slower upwind", turns: "tacks cost more speed than gybes", nogo: "empty no-go arc", drift: "drift while stopped", prior: "weather/device hint" };
      var agree = [], against = [];
      Object.keys(w.direction.votes).forEach(function (k) { (w.direction.votes[k][0] >= 0 ? agree : against).push(names[k]); });
      p.appendChild(el("p", { class: "tk-hint", text: "Which end of the axis is upwind: " + (agree.length ? "supported by " + agree.join(", ") : "no supporting evidence") + (against.length ? "; against: " + against.join(", ") : "") + "." }));
    }
    if (w.series_t && w.series_t.length > 1) {
      var box = el("div", { style: { marginTop: "10px" } });
      p.appendChild(el("div", { class: "tk-chart-title", text: "Wind direction through the session (°)" }));
      p.appendChild(box);
      var xs = w.series_t.map(function (t) { return t + D.off; });
      var ys = w.series_deg.slice();
      for (var i = 1; i < ys.length; i++) { while (ys[i] - ys[i - 1] > 180) ys[i] -= 360; while (ys[i] - ys[i - 1] < -180) ys[i] += 360; }
      var ch = new uPlot({
        width: box.clientWidth || 400, height: 140, tzDate: function (ts) { return uPlot.tzDate(new Date(ts * 1e3), "Etc/UTC"); },
        scales: { x: { time: true } }, axes: [Object.assign(TK.axisStyle(), { values: function (u, v) { return v.map(function (x) { return TK.clock(x, 0); }); } }), Object.assign(TK.axisStyle(), { size: 44, values: function (u, v) { return v.map(function (x) { return ((Math.round(x) % 360) + 360) % 360 + "°"; }); } })],
        series: [{ value: function (u, v) { return v === null ? "—" : TK.clock(v, 0); } }, { label: "Wind from", stroke: TK.css("--series-1"), width: 2, points: { show: true, size: 6 },
          value: function (u, v) { return v === null ? "—" : (((Math.round(v) % 360) + 360) % 360) + "°"; } }],
      }, [xs, ys], box);
      TK.fitWidth(ch, box, 140);
      D.extraCharts.push(ch);
    }
    // controls
    var msg = el("p", { class: "tk-msg" });
    var form = el("div", { class: "tk-form", style: { marginTop: "14px" } });
    var inp = el("input", { type: "number", min: 0, max: 359, step: 1, value: A.settings.wind_override_deg === null ? "" : A.settings.wind_override_deg, placeholder: String(Math.round(w.auto_deg)) });
    form.appendChild(el("label", null, ["Fixed wind direction (°, from)", inp]));
    var actions = el("div", { class: "row-actions" });
    actions.appendChild(el("button", { class: "tk-btn primary", type: "button", text: "Use this direction", onclick: function () {
      if (inp.value === "") return;
      patch({ settings: { wind_override_deg: ((+inp.value % 360) + 360) % 360, wind_keyframes: null } }, msg);
    } }));
    actions.appendChild(el("button", { class: "tk-btn", type: "button", text: "Add keyframe at replay time", title: "Build a shifting wind: each keyframe is the wind at one moment; it's interpolated in between.", onclick: function () {
      var deg = inp.value === "" ? Math.round(A.series.wind[D.idx]) : +inp.value;
      var frames = (A.settings.wind_keyframes || []).concat([{ t: D.X[D.idx] - D.off, deg: deg }]);
      patch({ settings: { wind_keyframes: frames, wind_override_deg: null } }, msg);
    } }));
    if (w.source !== "auto") actions.appendChild(el("button", { class: "tk-btn", type: "button", text: "Back to automatic", onclick: function () {
      patch({ settings: { wind_override_deg: null, wind_keyframes: null } }, msg);
    } }));
    form.appendChild(actions);
    p.appendChild(form);
    var frames = A.settings.wind_keyframes || [];
    if (frames.length) {
      var ul = el("ul", { class: "tk-hint" });
      frames.forEach(function (f, i) {
        ul.appendChild(el("li", null, [TK.clock(f.t, D.off, true) + " → " + Math.round(f.deg) + "° ", el("button", { class: "tk-btn", type: "button", text: "remove", onclick: function () {
          var fr = frames.slice(); fr.splice(i, 1);
          patch({ settings: { wind_keyframes: fr.length ? fr : null } }, msg);
        } })]));
      });
      p.appendChild(ul);
    }
    p.appendChild(msg);
    return p;
  }

  // ================================================================== foiling state
  function statePanel() {
    var st = D.A.state;
    var p = TK.panel(D.stateName, "On at " + st.on_kn + " kn, off below " + st.off_kn + " kn (change in Settings).");
    var kv = el("dl", { class: "tk-kv" });
    [["Time " + D.stateName.toLowerCase(), TK.hm(st.time_s) + " (" + TK.num(st.pct_of_moving, 0) + "% of moving, " + TK.num(st.pct_of_total, 0) + "% of total)"],
      ["Distance", TK.km(st.distance_m) + " km"], ["Flights", String(st.flights)], ["Touchdowns", String(st.touchdowns)],
      ["Longest", TK.dur(st.longest_s) + " · " + TK.km(st.longest_m) + " km"], ["Average flight", TK.dur(st.avg_flight_s)]].forEach(function (r) {
      kv.appendChild(el("dt", { text: r[0] })); kv.appendChild(el("dd", { text: r[1] }));
    });
    p.appendChild(kv);
    var fl = D.A.flights.slice().sort(function (a, b) { return b.duration_s - a.duration_s; }).slice(0, 40);
    if (fl.length) {
      p.appendChild(el("div", { class: "tk-chart-title", style: { marginTop: "12px" }, text: "Longest flights" }));
      p.appendChild(TK.table([
        { key: "t0", label: "Start", fmt: function (f) { return TK.clock(f.t0, D.off, true); } },
        { key: "duration_s", label: "Time", num: true, fmt: function (f) { return TK.dur(f.duration_s); } },
        { key: "distance_m", label: "Dist m", num: true, fmt: function (f) { return TK.num(f.distance_m, 0); } },
        { key: "max_kn", label: "Max kn", num: true, fmt: function (f) { return TK.kn(f.max_kn); } },
      ], fl, { onRow: function (f) { focusRange(f.i0, f.i1); }, maxHeight: "220px", sortKey: "duration_s", sortDir: -1 }));
    }
    return p;
  }

  // ================================================================== HR
  function hrPanel() {
    var h = D.A.hr;
    var p = TK.panel("Heart rate", "Zones are % of max HR " + h.max_hr_setting + " bpm (set max HR in Settings).");
    var kv = el("dl", { class: "tk-kv" });
    [["Average / max", TK.num(h.avg, 0) + " / " + TK.num(h.max, 0) + " bpm"], ["Steady sailing", TK.num(h.sailing_avg, 0) + " bpm"],
      ["During maneuvers", TK.num(h.maneuver_avg, 0) + " bpm"], ["Peak per tack", TK.num(h.tack_peak_avg, 0) + " bpm"], ["Peak per gybe", TK.num(h.gybe_peak_avg, 0) + " bpm"]].forEach(function (r) {
      kv.appendChild(el("dt", { text: r[0] })); kv.appendChild(el("dd", { text: r[1] }));
    });
    p.appendChild(kv);
    p.appendChild(el("div", { class: "tk-chart-title", style: { marginTop: "12px" }, text: "Minutes in each zone" }));
    TK.bars(p, h.zones.map(function (z) {
      return { label: "Z" + z.zone, value: z.time_s / 60, color: TK.speedColor(Math.min(7, z.zone + 2)),
        title: "Zone " + z.zone + " · " + z.lo_bpm + "–" + z.hi_bpm + " bpm", tip: [{ value: TK.num(z.time_s / 60, 1) + " min" }] };
    }), { height: 170, aria: "Minutes per heart-rate zone" });
    p.appendChild(el("div", { class: "tk-chart-title", style: { marginTop: "12px" }, text: "Average HR by speed" }));
    TK.lineChart(p, h.by_speed.map(function (b) {
      return { x: b.kn, y: b.hr, title: (b.kn - 1) + "–" + (b.kn + 1) + " kn", tip: [{ value: TK.num(b.hr, 0) + " bpm" }, { value: TK.dur(b.time_s), label: "time" }] };
    }), { xLabel: "speed (kn)", aria: "Heart rate by speed", color: TK.css("--series-3") });
    return p;
  }

  // ================================================================== race
  function addMark(latlng) {
    var marks = (D.A.settings.marks || []).concat([{ lat: +latlng.lat.toFixed(6), lon: +latlng.lng.toFixed(6), name: "M" + ((D.A.settings.marks || []).length + 1) }]);
    D.placing = false;
    document.body.classList.remove("tk-placing");
    patch({ settings: { marks: marks } });
  }

  function racePanel() {
    var A = D.A, race = A.race, st = A.settings;
    var p = TK.panel("Race", "Set the start gun to split pre-start from race. Legs run between marks: place your own on the map (rounded in order, laps repeat) or leave them empty for automatic windward/leeward detection.");
    var msg = el("p", { class: "tk-msg" });
    var form = el("div", { class: "tk-form" });
    var start = el("input", { type: "datetime-local", step: 1, value: st.race_start ? TK.localDateTimeInput(st.race_start, D.off) : "" });
    form.appendChild(el("label", null, ["Start gun (local time)", start]));
    var radius = el("input", { type: "number", min: 10, max: 500, step: 5, value: st.mark_radius_m });
    form.appendChild(el("label", null, ["Mark rounding radius (m)", radius]));
    var acts = el("div", { class: "row-actions" });
    acts.appendChild(el("button", { class: "tk-btn primary", type: "button", text: "Save", onclick: function () {
      patch({ settings: { race_start: TK.fromLocalInput(start.value, D.off), mark_radius_m: +radius.value || null } }, msg);
    } }));
    acts.appendChild(el("button", { class: "tk-btn", type: "button", text: "Start = replay time", onclick: function () {
      patch({ settings: { race_start: D.X[D.idx] - D.off } }, msg);
    } }));
    var placeBtn = el("button", { class: "tk-btn", type: "button", "aria-pressed": "false", text: "Place a mark on the map", onclick: function () {
      D.placing = !D.placing;
      placeBtn.setAttribute("aria-pressed", D.placing ? "true" : "false");
      document.body.classList.toggle("tk-placing", D.placing);
      if (D.placing) document.getElementById("tkTop").scrollIntoView({ behavior: "smooth" });
    } });
    acts.appendChild(placeBtn);
    if ((st.marks || []).length) acts.appendChild(el("button", { class: "tk-btn", type: "button", text: "Clear marks", onclick: function () { patch({ settings: { marks: null } }, msg); } }));
    if (st.race_start) acts.appendChild(el("button", { class: "tk-btn", type: "button", text: "Clear start", onclick: function () { patch({ settings: { race_start: null } }, msg); } }));
    form.appendChild(acts);
    p.appendChild(form);
    p.appendChild(msg);
    if (race.warning) p.appendChild(el("div", { class: "tk-warn", text: race.warning }));
    if (race.pre_start && race.race) {
      var rows = [Object.assign({ name: "Pre-start" }, race.pre_start), Object.assign({ name: "Race" }, race.race)];
      p.appendChild(phaseTable(rows));
    }
    p.appendChild(el("div", { class: "tk-chart-title", style: { marginTop: "12px" }, text: "Legs between " + (race.marks_mode === "user" ? "your marks" : "detected roundings") + " (" + race.roundings.length + " roundings)" }));
    var legs = race.legs.map(function (l) { return Object.assign({ name: l.from + " → " + l.to }, l); });
    p.appendChild(phaseTable(legs, true));
    return p;
  }

  function phaseTable(rows, clickable) {
    return TK.table([
      { key: "name", label: "", sortable: false },
      { key: "t0", label: "From", sortable: false, fmt: function (r) { return TK.clock(r.t0, D.off, true); } },
      { key: "duration_s", label: "Time", num: true, sortable: false, fmt: function (r) { return TK.dur(r.duration_s); } },
      { key: "distance_m", label: "Dist km", num: true, sortable: false, fmt: function (r) { return TK.km(r.distance_m); } },
      { key: "avg_kn", label: "Avg kn", num: true, sortable: false, fmt: function (r) { return TK.kn(r.avg_kn); } },
      { key: "max_kn", label: "Max kn", num: true, sortable: false, fmt: function (r) { return TK.kn(r.max_kn); } },
      { key: "avg_vmg_up_kn", label: "VMG up", num: true, sortable: false, fmt: function (r) { return TK.kn(r.avg_vmg_up_kn); } },
      { key: "avg_vmg_down_kn", label: "VMG dn", num: true, sortable: false, fmt: function (r) { return TK.kn(r.avg_vmg_down_kn); } },
      { key: "gain_m", label: "Gain m", num: true, sortable: false, fmt: function (r) { return TK.num(r.gain_m, 0); } },
      { key: "tacks", label: "T / G", num: true, sortable: false, fmt: function (r) { return r.tacks + " / " + r.gybes; } },
      { key: "state_pct", label: D.stateName + " %", num: true, sortable: false, fmt: function (r) { return TK.num(r.state_pct, 0); } },
    ], rows, { onRow: clickable ? function (r) { focusRange(r.i0, r.i1); } : null, empty: "No legs." });
  }

  // ================================================================== iQFOIL: vs baseline + DeepSeek analysis
  function sailingPanel() {
    var wrap = el("div", { class: "tk-grid-even" });
    var bp = TK.panel("vs baseline", null);
    var ap = TK.panel("Coach analysis", null);
    wrap.appendChild(bp);
    wrap.appendChild(ap);
    bp.appendChild(el("p", { class: "tk-msg", text: "Loading…" }));
    var id = D.id, polls = 0;

    function load() {
      TK.api("/api/sailing/sessions/" + id).then(function (st) {
        if (!D || D.id !== id) return;  // navigated away
        renderBaseline(st);
        renderAnalysis(st);
        if (st.running && polls++ < 40) setTimeout(load, 3000);
      }).catch(function (e) { bp.textContent = ""; bp.appendChild(el("p", { class: "tk-msg err", text: e.message })); });
    }

    function renderBaseline(st) {
      bp.textContent = "";
      bp.appendChild(el("h2", { text: "vs baseline" }));
      if (!st.eligible) { bp.appendChild(el("p", { class: "tk-msg", text: st.reason })); return; }
      var b = st.baseline;
      var head = "Wind " + TK.num(st.wind_kn, 1) + " kn → band " + TK.bandLabel(st.band) + ". ";
      if (!b.sessions) {
        bp.appendChild(el("p", { class: "panel-sub", text: head }));
        bp.appendChild(el("p", { class: "tk-msg", text: "No earlier sessions in this band yet — this one becomes the baseline." }));
        return;
      }
      var what = b.level === "band+gear" ? "same " + st.match_parts.map(function (k) { return TK.GEAR_LABEL[k].toLowerCase(); }).join(" + ") + " (" + b.gear_label + ")" : "all gear";
      bp.appendChild(el("p", { class: "panel-sub", text: head + "Compared with " + b.sessions + " earlier session" + (b.sessions === 1 ? "" : "s") +
        " in this band with " + what + "; averages over the last " + b.rolling_n + "." }));
      if (b.fallback) bp.appendChild(el("div", { class: "tk-warn", role: "status", text: "Band-only baseline: " + b.fallback_reason + "." }));
      var rows = TK.SAIL_METRICS.map(function (m) { return Object.assign({ key: m[0], label: m[1], nd: m[2] }, st.comparison[m[0]]); });
      bp.appendChild(TK.table([
        { key: "label", label: "Metric", sortable: false },
        { key: "value", label: "This session", num: true, sortable: false, fmt: function (r) { return TK.num(r.value, r.nd) + (r.new_best ? " ★" : ""); } },
        { key: "rolling_avg", label: "Rolling avg", num: true, sortable: false, fmt: function (r) { return TK.num(r.rolling_avg, r.nd); } },
        { key: "pct_vs_avg", label: "Δ vs avg", num: true, sortable: false, fmt: function (r) {
          if (r.pct_vs_avg === null || r.pct_vs_avg === undefined) return "—";
          return el("span", { class: "tk-delta " + (r.better === true ? "good" : r.better === false ? "bad" : ""),
            text: (r.pct_vs_avg > 0 ? "+" : "") + r.pct_vs_avg.toFixed(1) + " %" + (r.better === true ? " ▲" : r.better === false ? " ▼" : "") });
        } },
        { key: "best", label: "Best", num: true, sortable: false, fmt: function (r) { return TK.num(r.best, r.nd); } },
      ], rows));
      bp.appendChild(el("p", { class: "tk-hint", text: "▲ better / ▼ worse than your average (a lower upwind angle is better). ★ new personal best for this baseline." }));
    }

    function renderAnalysis(st) {
      ap.textContent = "";
      ap.appendChild(el("h2", { text: "Coach analysis" }));
      var a = st.analysis;
      var msg = el("p", { class: "tk-msg", "aria-live": "polite" });
      var btn = el("button", { class: "tk-btn" + (a ? "" : " primary"), type: "button", text: a ? "Re-analyze" : "Analyze with DeepSeek",
        disabled: !st.eligible || !st.ai_enabled || st.running });
      btn.addEventListener("click", function () {
        btn.disabled = true; msg.className = "tk-msg"; msg.textContent = "Asking DeepSeek… (10–30 s)";
        TK.api("/api/sailing/sessions/" + id + "/analyze", { method: "POST" })
          .then(function (s) { renderBaseline(s); renderAnalysis(s); })
          .catch(function (e) { btn.disabled = false; msg.className = "tk-msg err"; msg.textContent = e.message; });
      });
      if (st.running) msg.textContent = "Analysing in the background…";
      else if (!st.ai_enabled) msg.textContent = "Set DEEPSEEK_API_KEY to enable AI analysis.";
      else if (!a && st.eligible) msg.textContent = "Not analysed yet. Tag the front wing and sail first for a gear-matched comparison.";
      if (a && a.stale) ap.appendChild(el("div", { class: "tk-warn", role: "status", text: "Gear, note or wind changed since this analysis — re-analyze to update it." }));
      if (a) ap.appendChild(analysisCard(a));
      ap.appendChild(el("div", { class: "row-actions", style: { display: "flex", gap: "8px", marginTop: "10px" } }, [btn]));
      ap.appendChild(msg);
    }

    load();
    return wrap;
  }

  function analysisCard(a) {
    var r = a.result, c = el("div", { class: "tk-ai" });
    c.appendChild(el("p", { text: r.summary }));
    function list(title, items, cls) {
      if (!items || !items.length) return;
      c.appendChild(el("h4", { text: title }));
      c.appendChild(el("ul", { class: cls || null }, items.map(function (x) { return el("li", { text: x }); })));
    }
    var focus = el("div", { class: "tk-focus" });
    focus.appendChild(el("h4", { text: "Focus next session" }));
    focus.appendChild(el("ul", null, (r.focus_next_session || []).map(function (x) { return el("li", { text: x }); })));
    c.appendChild(focus);
    list("Strengths", r.strengths);
    list("Weaknesses", r.weaknesses);
    if (r.vs_baseline) { c.appendChild(el("h4", { text: "vs baseline" })); c.appendChild(el("p", { text: r.vs_baseline })); }
    var pc = r.possible_causes || {};
    list("Possible causes — technique", pc.technique);
    list("Possible causes — gear", pc.gear);
    list("Possible causes — fatigue / recovery", pc.fatigue_recovery);
    if (r.plan_adjustment) { c.appendChild(el("h4", { text: "Plan adjustment" })); c.appendChild(el("p", { text: r.plan_adjustment })); }
    var u = a.usage || {};
    c.appendChild(el("p", { class: "tk-hint", text: [(a.model || "DeepSeek") + " · " + a.created_at.slice(0, 16).replace("T", " ") + " UTC",
      u.prompt_tokens ? u.prompt_tokens + " prompt tokens (" + (u.cache_hit_tokens || 0) + " cached) + " + u.completion_tokens + " output" : null,
      u.cost_usd !== null && u.cost_usd !== undefined ? "≈ $" + u.cost_usd.toFixed(5) : null,
      "packet ≈ " + a.packet_tokens + " tokens"].filter(Boolean).join(" · ") }));
    return c;
  }

  // ================================================================== info (notes, spot, gear, conditions)
  function infoPanel() {
    var tr = D.A.track, O = D.OPTS, cond = tr.conditions || {};
    var p = TK.panel("Notes, spot, gear & conditions", null);
    var msg = el("p", { class: "tk-msg" });
    var f = el("div", { class: "tk-form" });
    var title = el("input", { type: "text", maxlength: 120, value: tr.title || "" });
    var spot = el("input", { type: "text", maxlength: 80, value: tr.spot || "", list: "tkSpotList" });
    var dl = el("datalist", { id: "tkSpotList" });
    O.spots.forEach(function (s) { dl.appendChild(el("option", { value: s.spot })); });
    var notes = el("textarea", { maxlength: 5000, placeholder: "How you felt, conditions, setup changes (mast rake, outhaul, downhaul…)" });
    notes.value = tr.notes || "";
    f.appendChild(el("label", null, ["Title", title]));
    f.appendChild(el("label", null, ["Spot", spot, dl]));
    f.appendChild(el("label", { class: "wide" }, ["Session note", notes]));
    // gear: one dropdown per iQFOIL part (a setup fills them all), checkboxes for the rest
    var mine = {};
    (tr.gear || []).forEach(function (g) { mine[g.id] = true; });
    var usable = O.gear.filter(function (g) { return !g.retired || mine[g.id]; });
    var slots = {};
    var slotBox = el("div", { class: "tk-form tk-slots" });
    var setupSel = el("select");
    setupSel.appendChild(el("option", { value: "", text: O.setups.length ? "Apply a setup…" : "No setups yet" }));
    O.setups.forEach(function (s) { setupSel.appendChild(el("option", { value: s.id, text: s.name })); });
    setupSel.addEventListener("change", function () {
      var s = O.setups.filter(function (x) { return String(x.id) === setupSel.value; })[0];
      if (!s) return;
      Object.keys(slots).forEach(function (k) { slots[k].value = ""; });
      s.parts.forEach(function (p) { if (slots[p.kind]) slots[p.kind].value = p.id; });
    });
    slotBox.appendChild(el("label", null, ["Setup", setupSel]));
    O.sail_parts.forEach(function (k) {
      var sel = el("select");
      sel.appendChild(el("option", { value: "", text: "—" }));
      usable.filter(function (g) { return g.kind === k; }).forEach(function (g) {
        sel.appendChild(el("option", { value: g.id, text: (g.code || g.name) + (g.code && g.name !== g.code ? " · " + g.name : ""), selected: !!mine[g.id] }));
      });
      slots[k] = sel;
      slotBox.appendChild(el("label", null, [TK.GEAR_LABEL[k], sel]));
    });
    f.appendChild(el("div", { class: "wide", style: { gridColumn: "1 / -1" } }, [slotBox]));
    var gearBox = el("div", { class: "tk-gear-pick" });
    usable.filter(function (g) { return O.sail_parts.indexOf(g.kind) < 0; }).forEach(function (g) {
      gearBox.appendChild(el("label", null, [el("input", { type: "checkbox", value: g.id, checked: !!mine[g.id] }), TK.gearText(g) + (g.size ? " " + g.size : "")]));
    });
    if (gearBox.childNodes.length) f.appendChild(el("label", { class: "wide" }, ["Other gear", gearBox]));
    if (!O.gear.length) f.appendChild(el("p", { class: "tk-hint", style: { gridColumn: "1 / -1" }, html: 'No gear yet — add parts by code and save setups under <a href="#tracks/gear">Gear</a>.' }));
    // conditions
    var cw = el("input", { type: "number", min: 0, max: 100, step: 0.5, value: cond.wind_kn === undefined || cond.wind_kn === null ? "" : cond.wind_kn });
    var cg = el("input", { type: "number", min: 0, max: 150, step: 0.5, value: cond.gust_kn === undefined || cond.gust_kn === null ? "" : cond.gust_kn });
    var cd = el("input", { type: "number", min: 0, max: 359, step: 1, value: cond.wind_dir === undefined || cond.wind_dir === null ? "" : cond.wind_dir });
    var ca = el("input", { type: "number", min: -40, max: 50, step: 0.5, value: cond.air_c === undefined || cond.air_c === null ? "" : cond.air_c });
    var cwt = el("input", { type: "number", min: -5, max: 40, step: 0.5, value: cond.water_c === undefined || cond.water_c === null ? "" : cond.water_c });
    f.appendChild(el("label", null, ["Wind (kn)", cw]));
    f.appendChild(el("label", null, ["Gusts (kn)", cg]));
    f.appendChild(el("label", null, ["Wind from (°)", cd]));
    f.appendChild(el("label", null, ["Air (°C)", ca]));
    f.appendChild(el("label", null, ["Water (°C)", cwt]));
    f.appendChild(el("p", { class: "tk-hint", style: { gridColumn: "1 / -1" }, text: cond.source ? "Conditions from " + cond.source + (cond.fetched_at ? " (" + cond.fetched_at.slice(0, 10) + ")" : "") + ". Editing them makes them manual; a manual wind direction also tells the engine which end of the wind axis is upwind." : "No conditions yet." }));
    var acts = el("div", { class: "row-actions" });
    function numOrNull(i) { return i.value === "" ? null : +i.value; }
    acts.appendChild(el("button", { class: "tk-btn primary", type: "button", text: "Save", onclick: function () {
      var ids = Array.prototype.map.call(gearBox.querySelectorAll("input:checked"), function (i) { return +i.value; })
        .concat(Object.keys(slots).map(function (k) { return +slots[k].value; }).filter(Boolean));
      var body = { title: title.value, spot: spot.value, notes: notes.value, gear_ids: ids };
      var newCond = { wind_kn: numOrNull(cw), gust_kn: numOrNull(cg), wind_dir: numOrNull(cd), air_c: numOrNull(ca), water_c: numOrNull(cwt) };
      var changed = ["wind_kn", "gust_kn", "wind_dir", "air_c", "water_c"].some(function (k) { return (cond[k] === undefined ? null : cond[k]) !== newCond[k]; });
      if (changed) body.conditions = newCond;
      patch(body, msg);
    } }));
    acts.appendChild(el("button", { class: "tk-btn", type: "button", text: "Fetch conditions from Open-Meteo", onclick: function () {
      msg.textContent = "Fetching…"; msg.className = "tk-msg";
      TK.api("/api/tracks/" + D.id + "/conditions/fetch", { method: "POST" }).then(reload).catch(function (e) { msg.textContent = e.message; msg.className = "tk-msg err"; });
    } }));
    f.appendChild(acts);
    p.appendChild(f);
    p.appendChild(msg);
    return p;
  }

  // ================================================================== settings
  function settingsPanel() {
    var st = D.A.settings, O = D.OPTS;
    var p = el("details", { class: "panel" });
    p.appendChild(el("summary", { text: "Analysis settings" }));
    p.appendChild(el("p", { class: "panel-sub", text: "Everything the engine assumed for this session. Empty = default." }));
    var msg = el("p", { class: "tk-msg" });
    var f = el("div", { class: "tk-form" });
    var sport = el("select");
    O.sports.forEach(function (s) { sport.appendChild(el("option", { value: s.key, text: s.label, selected: s.key === D.A.track.sport })); });
    f.appendChild(el("label", null, ["Sport", sport]));
    var fields = [
      ["on_kn", D.stateName + " on at (kn)", 0.5], ["off_kn", D.stateName + " off below (kn)", 0.5], ["stop_kn", "Stopped below (kn)", 0.5],
      ["min_steady_s", "Min steady time per tack (s)", 1], ["maneuver_window_s", "Max maneuver gap (s)", 1], ["steady_turn_dps", "Steady if turning < (°/s)", 1],
      ["upwind_max_twa", "Upwind if TWA ≤ (°)", 1], ["downwind_min_twa", "Downwind if TWA ≥ (°)", 1], ["max_hr", "Max heart rate (bpm)", 1],
      ["max_accel_ms2", "Spike threshold (m/s²)", 0.5], ["wind_window_min", "Wind-shift window (min)", 1],
    ];
    var inputs = {};
    var saved = D.A.track.settings || {};
    fields.forEach(function (fd) {
      var i = el("input", { type: "number", step: fd[2], value: saved[fd[0]] === undefined ? "" : saved[fd[0]], placeholder: st[fd[0]] === null || st[fd[0]] === undefined ? "auto" : String(st[fd[0]]) });
      inputs[fd[0]] = i;
      f.appendChild(el("label", null, [fd[1], i]));
    });
    var tg = st.targets || {};
    var tmode = el("select");
    [["polar", "From personal polar"], ["manual", "Manual"]].forEach(function (m) { tmode.appendChild(el("option", { value: m[0], text: m[1], selected: (tg.mode || "polar") === m[0] })); });
    f.appendChild(el("label", null, ["Targets", tmode]));
    var tpct = el("select");
    [[50, "Median (p50)"], [75, "Strong (p75)"], [90, "Best (p90)"]].forEach(function (m) { tpct.appendChild(el("option", { value: m[0], text: m[1], selected: (tg.percentile || 75) === m[0] })); });
    f.appendChild(el("label", null, ["Polar target level", tpct]));
    var tIn = {};
    [["upwind_kn", "Upwind target (kn)"], ["upwind_twa", "Upwind target TWA (°)"], ["downwind_kn", "Downwind target (kn)"], ["downwind_twa", "Downwind target TWA (°)"]].forEach(function (t) {
      var cur = D.A.targets[t[0]];
      var i = el("input", { type: "number", step: 0.5, value: tg.mode === "manual" && tg[t[0]] !== null && tg[t[0]] !== undefined ? tg[t[0]] : "", placeholder: cur === null || cur === undefined ? "" : String(cur) });
      tIn[t[0]] = i;
      f.appendChild(el("label", null, [t[1], i]));
    });
    var acts = el("div", { class: "row-actions" });
    acts.appendChild(el("button", { class: "tk-btn primary", type: "button", text: "Save & re-analyse", onclick: function () {
      var s = {};
      Object.keys(inputs).forEach(function (k) { s[k] = inputs[k].value === "" ? null : +inputs[k].value; });
      var targets = { mode: tmode.value, percentile: +tpct.value };
      Object.keys(tIn).forEach(function (k) { targets[k] = tIn[k].value === "" ? null : +tIn[k].value; });
      s.targets = targets;
      patch({ sport: sport.value, settings: s }, msg);
    } }));
    acts.appendChild(el("button", { class: "tk-btn", type: "button", text: "Reset to defaults", onclick: function () {
      var s = { targets: null };
      Object.keys(inputs).forEach(function (k) { s[k] = null; });
      patch({ settings: s }, msg);
    } }));
    f.appendChild(acts);
    p.appendChild(f);
    p.appendChild(msg);
    var clean = D.A.cleaning;
    p.appendChild(el("p", { class: "tk-hint", text: "Cleaning: " + clean.input_fixes + " fixes, " + clean.position_jumps_dropped + " position jumps dropped, " +
      clean.speed_spikes_removed + " speed-spike samples removed, " + (clean.hdop_available ? clean.bad_hdop_fixes + " bad-HDOP fixes flagged" : "no HDOP in file (" + (clean.suspect_fixes || 0) + " Doppler/position mismatches flagged)") +
      ", " + clean.segments + " segment(s), speed from " + clean.speed_source + ". Engine " + D.A.engine_version + "." }));
    return p;
  }
})();
