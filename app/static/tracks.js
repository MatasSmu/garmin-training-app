/* Session analysis -- Tracks tab: router, session list, progress, polar,
   speed map, gear log, compare. The single-session page is tracks-detail.js. */
(function () {
  "use strict";

  var TK = window.TK, el = TK.el;
  var root = document.getElementById("tracksRoot");
  var tabBtn = document.querySelector('.tab-btn[data-tab="tracks"]');
  var OPTS = null;
  var FILTERS = {};
  var SELECTED = {};
  var charts = [];
  var maps = [];

  function cleanup() {
    TK.destroyDetail();
    TK.destroyCharts(charts);
    charts = [];
    maps.forEach(function (m) { m.remove(); });
    maps = [];
    TK.untip();
    document.body.classList.remove("wide");
  }

  function options(force) {
    if (OPTS && !force) return Promise.resolve(OPTS);
    return TK.api("/api/tracks/options").then(function (o) { OPTS = o; return o; });
  }

  // ------------------------------------------------------------------ router
  function parse() {
    var h = location.hash.replace(/^#/, "");
    var q = {};
    var qi = h.indexOf("?");
    if (qi >= 0) {
      h.slice(qi + 1).split("&").forEach(function (kv) { var p = kv.split("="); if (p[0]) q[p[0]] = decodeURIComponent(p[1] || ""); });
      h = h.slice(0, qi);
    }
    var parts = h.split("/");
    return { root: parts[0], sub: parts[1] || "", q: q };
  }

  function isOurs(r) { return r.root === "tracks" || r.root === "track" || r.root === "compare"; }

  function route() {
    var r = parse();
    if (!isOurs(r)) { cleanup(); return; }
    if (!tabBtn.classList.contains("active")) {
      document.querySelectorAll(".tab-btn").forEach(function (b) { b.classList.remove("active"); });
      document.querySelectorAll(".tab-section").forEach(function (s) { s.classList.remove("active"); });
      tabBtn.classList.add("active");
      document.getElementById("tab-tracks").classList.add("active");
    }
    cleanup();
    if (r.root === "track" && r.sub) {
      TK.renderDetail(root, +r.sub);
      return;
    }
    if (r.root === "compare") { renderCompare(r.sub.split(",").map(Number).filter(Boolean)); return; }
    if (r.q.compare) SELECTED[r.q.compare] = true;
    var view = r.sub || "list";
    document.body.classList.add("wide");
    options(true).then(function () {
      root.textContent = "";
      root.appendChild(subnav(view));
      var body = el("div");
      root.appendChild(body);
      ({ list: renderList, progress: renderProgress, polar: renderPolar, map: renderSpeedMap, gear: renderGear }[view] || renderList)(body);
    }).catch(function (e) {
      root.textContent = "";
      root.appendChild(el("p", { class: "tk-msg err", text: "Couldn't load tracks: " + e.message }));
    });
  }

  tabBtn.addEventListener("click", function () {
    if (!isOurs(parse())) location.hash = "#tracks";
  });
  document.querySelectorAll(".tab-btn").forEach(function (b) {
    if (b === tabBtn) return;
    b.addEventListener("click", function () {
      if (isOurs(parse())) history.replaceState(null, "", location.pathname + location.search);
      cleanup();
    });
  });
  window.addEventListener("hashchange", route);

  function subnav(active) {
    var nav = el("nav", { class: "tk-subnav", "aria-label": "Tracks views" });
    [["list", "Sessions"], ["progress", "Progress"], ["polar", "Personal polar"], ["map", "Speed map"], ["gear", "Gear"]].forEach(function (v) {
      nav.appendChild(el("a", { href: "#tracks" + (v[0] === "list" ? "" : "/" + v[0]), class: v[0] === active ? "active" : null,
        "aria-current": v[0] === active ? "page" : null, text: v[1] }));
    });
    return nav;
  }

  // ------------------------------------------------------------------ filters (shared)
  var COMPASS16 = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"];

  function filterBar(onChange, fields) {
    fields = fields || ["sport", "spot", "gear", "wind", "dir"];
    var bar = el("div", { class: "tk-filters", role: "search" });
    function sel(label, key, opts) {
      var s = el("select", { "aria-label": label });
      opts.forEach(function (o) { s.appendChild(el("option", { value: o[0], text: o[1], selected: String(FILTERS[key] || "") === String(o[0]) })); });
      s.addEventListener("change", function () { FILTERS[key] = s.value || null; onChange(); });
      bar.appendChild(el("label", null, [label, s]));
    }
    function num(label, key, ph) {
      var i = el("input", { type: "number", min: 0, max: 80, step: 1, placeholder: ph, value: FILTERS[key] === null || FILTERS[key] === undefined ? "" : FILTERS[key] });
      i.addEventListener("change", function () { FILTERS[key] = i.value === "" ? null : +i.value; onChange(); });
      bar.appendChild(el("label", null, [label, i]));
    }
    if (fields.indexOf("sport") >= 0) sel("Sport", "sport", [["", "All sports"]].concat(OPTS.sports.map(function (s) { return [s.key, s.label]; })));
    if (fields.indexOf("spot") >= 0) sel("Spot", "spot", [["", "All spots"]].concat(OPTS.spots.map(function (s) { return [s.spot, s.spot + " (" + s.sessions + ")"]; })));
    if (fields.indexOf("gear") >= 0) sel("Gear", "gear_id", [["", "Any gear"]].concat(OPTS.gear.map(function (g) { return [g.id, g.kind + ": " + g.name]; })));
    if (fields.indexOf("wind") >= 0) { num("Wind ≥ kn", "wind_min", "any"); num("Wind ≤ kn", "wind_max", "any"); }
    if (fields.indexOf("dir") >= 0) sel("Wind from", "wind_dir", [["", "Any direction"]].concat(COMPASS16.map(function (c, i) { return [i * 22.5, c + " (" + i * 22.5 + "°)"]; })));
    return bar;
  }

  function qs(extra) {
    var p = [];
    var map = { sport: "sport", spot: "spot", gear_id: "gear_id", wind_min: "wind_min", wind_max: "wind_max", wind_dir: "wind_dir" };
    Object.keys(map).forEach(function (k) { if (FILTERS[k] !== null && FILTERS[k] !== undefined && FILTERS[k] !== "") p.push(map[k] + "=" + encodeURIComponent(FILTERS[k])); });
    Object.keys(extra || {}).forEach(function (k) { if (extra[k] !== null && extra[k] !== undefined) p.push(k + "=" + encodeURIComponent(extra[k])); });
    return p.length ? "?" + p.join("&") : "";
  }

  // ------------------------------------------------------------------ list
  function renderList(body) {
    var head = TK.panel("Session analysis", "Upload FIT or GPX files, or pick water sessions on the Sessions tab and press Analyse. Click a session to open it; tick 2–4 to compare.");
    var drop = el("label", { class: "tk-drop", tabindex: 0 });
    var file = el("input", { type: "file", accept: ".fit,.gpx", multiple: true, style: { display: "none" } });
    drop.appendChild(file);
    drop.appendChild(document.createTextNode("Drop .fit / .gpx files here or click to choose"));
    var msg = el("p", { class: "tk-msg", "aria-live": "polite" });
    function upload(files) {
      var list = Array.prototype.slice.call(files);
      if (!list.length) return;
      var done = 0, lastId = null, errors = [];
      msg.className = "tk-msg";
      msg.textContent = "Analysing " + list.length + " file(s)…";
      list.reduce(function (p, f) {
        return p.then(function () {
          var fd = new FormData();
          fd.append("file", f, f.name);
          return TK.api("/api/tracks", { method: "POST", body: fd })
            .then(function (r) { lastId = r.id; done++; msg.textContent = "Analysed " + done + " / " + list.length + "…"; })
            .catch(function (e) { errors.push(f.name + ": " + e.message); });
        });
      }, Promise.resolve()).then(function () {
        if (errors.length) { msg.className = "tk-msg err"; msg.textContent = errors.join(" · "); }
        if (list.length === 1 && lastId && !errors.length) location.hash = "#track/" + lastId;
        else route();
      });
    }
    file.addEventListener("change", function () { upload(file.files); });
    drop.addEventListener("dragover", function (e) { e.preventDefault(); drop.classList.add("over"); });
    drop.addEventListener("dragleave", function () { drop.classList.remove("over"); });
    drop.addEventListener("drop", function (e) { e.preventDefault(); drop.classList.remove("over"); upload(e.dataTransfer.files); });
    drop.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); file.click(); } });
    head.appendChild(drop);
    head.appendChild(msg);
    body.appendChild(head);

    var panel = TK.panel(null, null, []);
    var tools = el("div", { class: "tk-map-tools" });
    var cmpBtn = el("button", { class: "tk-btn primary", type: "button", text: "Compare selected" });
    var box = el("div");
    panel.appendChild(filterBar(load));
    tools.appendChild(el("span", { class: "tk-hint", id: "tkCount" }));
    tools.appendChild(cmpBtn);
    panel.appendChild(tools);
    panel.appendChild(box);
    body.appendChild(panel);

    function updateCmp() {
      var ids = Object.keys(SELECTED).filter(function (k) { return SELECTED[k]; });
      cmpBtn.disabled = ids.length < 2 || ids.length > 4;
      cmpBtn.textContent = "Compare selected (" + ids.length + ")";
    }
    cmpBtn.addEventListener("click", function () {
      location.hash = "#compare/" + Object.keys(SELECTED).filter(function (k) { return SELECTED[k]; }).join(",");
    });

    function load() {
      box.style.opacity = 0.5;
      TK.api("/api/tracks" + qs()).then(function (rows) {
        box.style.opacity = 1;
        box.textContent = "";
        document.getElementById("tkCount").textContent = rows.length + " session" + (rows.length === 1 ? "" : "s");
        var cols = [
          { key: "sel", label: "", sortable: false, fmt: function (r) {
            var c = el("input", { type: "checkbox", "aria-label": "Select for compare", checked: !!SELECTED[r.id] });
            c.addEventListener("change", function () { SELECTED[r.id] = c.checked; updateCmp(); });
            return c; } },
          { key: "start_utc", label: "Date", fmt: function (r) { return r.start_date + " " + TK.clock(r.start_utc, r.utc_offset_s); } },
          { key: "title", label: "Session", fmt: function (r) {
            var d = el("span", null, [r.title || "—"]);
            (r.gear || []).forEach(function (g) { d.appendChild(document.createTextNode(" ")); d.appendChild(el("span", { class: "pill", text: g.name })); });
            return d; } },
          { key: "spot", label: "Spot" },
          { key: "sport", label: "Sport", fmt: function (r) { return TK.SPORT_LABEL[r.sport] || r.sport; } },
          { key: "duration_s", label: "Time", num: true, fmt: function (r) { return TK.dur(r.duration_s); } },
          { key: "distance_m", label: "km", num: true, fmt: function (r) { return TK.km(r.distance_m); } },
          { key: "max_kn", label: "Max kn", num: true, fmt: function (r) { return TK.kn(r.max_kn); } },
          { key: "best_500m_kn", label: "500 m", num: true, fmt: function (r) { return TK.kn(r.best_500m_kn); } },
          { key: "state_pct", label: "Foil/plane %", num: true, fmt: function (r) { return TK.num(r.state_pct, 0); } },
          { key: "tacks", label: "Tacks", num: true, fmt: function (r) { return r.tacks + (r.tack_success_pct === null ? "" : " · " + Math.round(r.tack_success_pct) + "%"); } },
          { key: "gybes", label: "Gybes", num: true, fmt: function (r) { return r.gybes + (r.gybe_success_pct === null ? "" : " · " + Math.round(r.gybe_success_pct) + "%"); } },
          { key: "upwind_vmg_kn", label: "VMG up", num: true, fmt: function (r) { return TK.kn(r.upwind_vmg_kn); } },
          { key: "wind_kn", label: "Wind", num: true, fmt: function (r) {
            return TK.compass(r.wind_deg) + (r.wind_kn === null || r.wind_kn === undefined ? "" : " " + Math.round(r.wind_kn) + "–" + Math.round(r.gust_kn) + " kn"); } },
        ];
        box.appendChild(TK.table(cols, rows, { sortKey: "start_utc", sortDir: -1, maxHeight: "640px", empty: "No sessions match — upload a file above.",
          onRow: function (r) { location.hash = "#track/" + r.id; } }));
        updateCmp();
      }).catch(function (e) { box.style.opacity = 1; box.textContent = e.message; });
    }
    load();
  }

  // ------------------------------------------------------------------ progress
  var TREND_CHARTS = [
    { title: "Top speed", unit: " kn", series: [["max_kn", "Top speed (2 s)", "--series-1"], ["best_500m_kn", "Best 500 m", "--series-2"]] },
    { title: "Velocity made good", unit: " kn", series: [["upwind_vmg_kn", "Upwind VMG", "--series-1"], ["downwind_vmg_kn", "Downwind VMG", "--series-2"]] },
    { title: "Maneuvers completed without touching down", unit: " %", nd: 0, series: [["tack_success_pct", "Tacks", "--series-1"], ["gybe_success_pct", "Gybes", "--series-2"]], yRange: [0, 100] },
    { title: "Distance lost per maneuver", unit: " m", nd: 0, series: [["tack_loss_m", "Tack", "--series-1"], ["gybe_loss_m", "Gybe", "--series-2"]] },
    { title: "Pointing: average upwind TWA (lower is higher)", unit: "°", nd: 0, series: [["upwind_twa", "Upwind TWA", "--series-1"]] },
    { title: "Time foiling / planing (% of moving)", unit: " %", nd: 0, series: [["state_pct", "Foiling / planing", "--series-1"]], yRange: [0, 100] },
  ];

  function renderProgress(body) {
    var panel = TK.panel("Progress", "One point per session, filtered below. Hover for values, click a point to open that session.");
    panel.appendChild(filterBar(load));
    var box = el("div", { class: "tk-grid-even" });
    panel.appendChild(box);
    body.appendChild(panel);
    function load() {
      TK.destroyCharts(charts); charts = [];
      box.style.opacity = 0.5;
      TK.api("/api/tracks/trends" + qs()).then(function (rows) {
        box.style.opacity = 1;
        box.textContent = "";
        if (rows.length < 1) { box.appendChild(el("p", { class: "tk-empty", text: "No sessions match." })); return; }
        var x = rows.map(function (r) { return r.start_utc; });
        TREND_CHARTS.forEach(function (tc) {
          var card = el("div");
          card.appendChild(el("div", { class: "tk-chart-title", text: tc.title }));
          var cbox = el("div");
          card.appendChild(cbox);
          box.appendChild(card);
          charts.push(TK.trendChart(cbox, x, tc.series.map(function (s) {
            return { label: s[1], color: TK.css(s[2]), unit: tc.unit, nd: tc.nd, data: rows.map(function (r) { return r[s[0]] === undefined ? null : r[s[0]]; }) };
          }), { height: 200, yRange: tc.yRange, onClick: function (i) { location.hash = "#track/" + rows[i].id; } }));
        });
      }).catch(function (e) { box.style.opacity = 1; box.textContent = e.message; });
    }
    load();
  }

  // ------------------------------------------------------------------ personal polar
  function renderPolar(body) {
    if (!FILTERS.sport) FILTERS.sport = "windfoil";
    var panel = TK.panel("Personal polar", "Every matching session's steady sailing merged into one polar. p90 per 5° bin is your repeatable top end — it's also the default target for leg and VMG comparisons.");
    panel.appendChild(filterBar(load, ["sport", "spot", "gear", "wind"]));
    var box = el("div", { class: "tk-grid-even" });
    panel.appendChild(box);
    body.appendChild(panel);
    function load() {
      box.style.opacity = 0.5;
      TK.api("/api/tracks/polar" + qs()).then(function (r) {
        box.style.opacity = 1;
        box.textContent = "";
        if (!r.polar) { box.appendChild(el("p", { class: "tk-empty", text: "No sessions match." })); return; }
        var pol = r.polar;
        var left = el("div", { style: { maxWidth: "480px" } });
        left.appendChild(el("div", { class: "tk-chart-title", text: r.sessions + " session" + (r.sessions === 1 ? "" : "s") }));
        function pts(k) { return pol.bins.map(function (b) { return { twa: b.twa, kn: b[k] }; }); }
        TK.polarChart(left, [
          { label: "p90 (personal polar)", color: TK.css("--series-1"), points: pts("p90") },
          { label: "Median", color: TK.css("--series-2"), points: pts("p50"), width: 1.5 },
        ], { marks: [pol.best_upwind && { twa: pol.best_upwind.twa, kn: pol.best_upwind.kn, label: "VMG " + TK.kn(pol.best_upwind.vmg_kn) },
          pol.best_downwind && { twa: pol.best_downwind.twa, kn: pol.best_downwind.kn, label: "VMG " + TK.kn(pol.best_downwind.vmg_kn) }], aria: "Personal polar" });
        box.appendChild(left);
        var right = el("div");
        var kv = el("dl", { class: "tk-kv" });
        [["Best upwind VMG", pol.best_upwind ? TK.kn(pol.best_upwind.vmg_kn) + " kn at " + pol.best_upwind.twa + "° (" + TK.kn(pol.best_upwind.kn) + " kn)" : "—"],
          ["Best downwind VMG", pol.best_downwind ? TK.kn(pol.best_downwind.vmg_kn) + " kn at " + pol.best_downwind.twa + "° (" + TK.kn(pol.best_downwind.kn) + " kn)" : "—"]].forEach(function (x) {
          kv.appendChild(el("dt", { text: x[0] })); kv.appendChild(el("dd", { text: x[1] }));
        });
        right.appendChild(kv);
        right.appendChild(TK.table([
          { key: "twa", label: "TWA", num: true, fmt: function (b) { return b.twa - 2.5 + "–" + (b.twa + 2.5) + "°"; } },
          { key: "seconds", label: "Time", num: true, fmt: function (b) { return TK.dur(b.seconds); } },
          { key: "p50", label: "Median kn", num: true, fmt: function (b) { return TK.kn(b.p50); } },
          { key: "p90", label: "p90 kn", num: true, fmt: function (b) { return TK.kn(b.p90); } },
          { key: "max", label: "Max kn", num: true, fmt: function (b) { return TK.kn(b.max); } },
        ], pol.bins.filter(function (b) { return b.seconds > 0; }), { maxHeight: "420px" }));
        box.appendChild(right);
      }).catch(function (e) { box.style.opacity = 1; box.textContent = e.message; });
    }
    load();
  }

  // ------------------------------------------------------------------ speed map
  function renderSpeedMap(body) {
    if (!FILTERS.spot && OPTS.spots.length) FILTERS.spot = OPTS.spots[0].spot;
    var panel = TK.panel("Speed map", "Where you're fast at a spot, merged across sessions (40 m cells). Filter by wind direction to see how the spot works in a given wind.");
    panel.appendChild(filterBar(load, ["sport", "spot", "dir"]));
    var metric = "mean";
    var seg = el("div", { class: "tk-seg", role: "group", "aria-label": "Cell value" });
    [["mean", "Average speed"], ["max", "Top speed"], ["on", "% foiling / planing"]].forEach(function (m) {
      var b = el("button", { type: "button", text: m[1], "aria-pressed": m[0] === metric ? "true" : "false", onclick: function () {
        metric = m[0];
        seg.querySelectorAll("button").forEach(function (x) { x.setAttribute("aria-pressed", x === b ? "true" : "false"); });
        draw();
      } });
      seg.appendChild(b);
    });
    panel.appendChild(seg);
    var info = el("p", { class: "tk-hint" });
    panel.appendChild(info);
    var mapDiv = el("div", { class: "tk-map", style: { marginTop: "10px" }, role: "application", "aria-label": "Speed map" });
    panel.appendChild(mapDiv);
    var legend = el("div");
    panel.appendChild(legend);
    body.appendChild(panel);
    var map = L.map(mapDiv, { preferCanvas: true });
    maps.push(map);
    TK.grayTiles(false).addTo(map);
    map.setView([55, 21], 6);
    var layer = L.layerGroup().addTo(map);
    var data = null;
    function draw() {
      layer.clearLayers();
      legend.textContent = "";
      if (!data || !data.cells.length) { info.textContent = "No sessions match."; return; }
      info.textContent = data.track_ids.length + " session(s), " + data.cells.length + " cells.";
      var val = function (c) { return metric === "mean" ? c.mean_kn : metric === "max" ? c.max_kn : c.on_pct; };
      var vmax = metric === "on" ? 100 : Math.max(10, Math.ceil(TK.percentile(data.cells.map(val), 0.98) / 5) * 5);
      var bounds = [];
      data.cells.forEach(function (c) {
        var b = [[c.lat - data.dlat / 2, c.lon - data.dlon / 2], [c.lat + data.dlat / 2, c.lon + data.dlon / 2]];
        bounds.push(b[0], b[1]);
        L.rectangle(b, { stroke: false, fillColor: TK.speedColor(TK.speedClass(val(c), vmax)), fillOpacity: 0.8 })
          .bindTooltip(TK.kn(c.mean_kn) + " kn avg · " + TK.kn(c.max_kn) + " kn max · " + Math.round(c.on_pct) + "% on · " + TK.dur(c.seconds) + " over " + c.sessions + " session(s)")
          .addTo(layer);
      });
      map.fitBounds(bounds, { padding: [20, 20] });
      var lg = el("div", { class: "tk-legend" });
      lg.appendChild(el("span", { text: (metric === "on" ? "% on" : "Speed") + ": 0" }));
      var r = el("span", { class: "ramp", "aria-hidden": "true" });
      for (var k = 0; k < TK.speedClasses; k++) r.appendChild(el("i", { style: { background: TK.speedColor(k) } }));
      lg.appendChild(r);
      lg.appendChild(el("span", { text: vmax + (metric === "on" ? " %" : " kn") }));
      legend.appendChild(lg);
    }
    function load() {
      info.textContent = "Loading…";
      TK.api("/api/tracks/speedmap" + qs({ dir_tol: 30 })).then(function (d) { data = d; draw(); })
        .catch(function (e) { info.textContent = e.message; });
    }
    load();
  }

  // ------------------------------------------------------------------ gear
  function renderGear(body) {
    var panel = TK.panel("Gear log", "Boards, sails, fins, foils, wings and kites. Attach gear on a session's page; hours and distance add up here.");
    var msg = el("p", { class: "tk-msg" });
    var f = el("div", { class: "tk-form" });
    var editing = null;
    var kind = el("select");
    OPTS.gear_kinds.forEach(function (k) { kind.appendChild(el("option", { value: k, text: k })); });
    var name = el("input", { type: "text", maxlength: 120, placeholder: "e.g. Freefoil 125" });
    var brand = el("input", { type: "text", maxlength: 120 });
    var size = el("input", { type: "text", maxlength: 60, placeholder: "e.g. 7.0 m² / 1000 cm²" });
    var notes = el("input", { type: "text", maxlength: 2000 });
    var retired = el("input", { type: "checkbox" });
    f.appendChild(el("label", null, ["Kind", kind]));
    f.appendChild(el("label", null, ["Name", name]));
    f.appendChild(el("label", null, ["Brand", brand]));
    f.appendChild(el("label", null, ["Size", size]));
    f.appendChild(el("label", null, ["Notes", notes]));
    f.appendChild(el("label", { style: { flexDirection: "row", alignItems: "center", gap: "6px" } }, [retired, "Retired"]));
    var save = el("button", { class: "tk-btn primary", type: "button", text: "Add gear" });
    var cancel = el("button", { class: "tk-btn", type: "button", text: "Cancel", style: { display: "none" } });
    var del = el("button", { class: "tk-btn danger", type: "button", text: "Delete", style: { display: "none" } });
    f.appendChild(el("div", { class: "row-actions" }, [save, cancel, del]));
    panel.appendChild(f);
    panel.appendChild(msg);
    var box = el("div");
    panel.appendChild(box);
    body.appendChild(panel);

    function reset() {
      editing = null; name.value = brand.value = size.value = notes.value = ""; retired.checked = false;
      save.textContent = "Add gear"; cancel.style.display = del.style.display = "none";
    }
    function edit(g) {
      editing = g.id; kind.value = g.kind; name.value = g.name; brand.value = g.brand || ""; size.value = g.size || ""; notes.value = g.notes || "";
      retired.checked = !!g.retired; save.textContent = "Save changes"; cancel.style.display = del.style.display = "";
      name.focus();
    }
    cancel.addEventListener("click", reset);
    del.addEventListener("click", function () {
      if (!confirm("Delete this gear? It's removed from every session it was attached to.")) return;
      TK.api("/api/gear/" + editing, { method: "DELETE" }).then(function () { reset(); load(); });
    });
    save.addEventListener("click", function () {
      var bodyJ = { kind: kind.value, name: name.value.trim(), brand: brand.value.trim() || null, size: size.value.trim() || null, notes: notes.value.trim() || null, retired: retired.checked };
      if (!bodyJ.name) { msg.className = "tk-msg err"; msg.textContent = "Name is required."; return; }
      TK.api(editing ? "/api/gear/" + editing : "/api/gear", { method: editing ? "PATCH" : "POST", body: bodyJ })
        .then(function () { msg.className = "tk-msg"; msg.textContent = "Saved."; reset(); load(); })
        .catch(function (e) { msg.className = "tk-msg err"; msg.textContent = e.message; });
    });
    function load() {
      TK.api("/api/gear").then(function (rows) {
        box.textContent = "";
        box.appendChild(TK.table([
          { key: "kind", label: "Kind" },
          { key: "name", label: "Name", fmt: function (g) { return g.name + (g.retired ? " (retired)" : ""); } },
          { key: "brand", label: "Brand" },
          { key: "size", label: "Size" },
          { key: "sessions", label: "Sessions", num: true },
          { key: "hours", label: "Hours", num: true, fmt: function (g) { return TK.num(g.hours, 1); } },
          { key: "km", label: "km", num: true, fmt: function (g) { return TK.num(g.km, 0); } },
          { key: "nm", label: "nm", num: true, fmt: function (g) { return TK.num(g.nm, 0); } },
          { key: "max_kn", label: "Top kn", num: true, fmt: function (g) { return g.max_kn ? TK.kn(g.max_kn) : "—"; } },
          { key: "last_used", label: "Last used" },
        ], rows, { onRow: edit, empty: "No gear yet — add your first board or sail above." }));
      });
    }
    load();
  }

  // ------------------------------------------------------------------ compare
  var CMP_ROWS = [
    ["Date", function (c) { return c.track.start_date; }],
    ["Spot", function (c) { return c.track.spot || "—"; }],
    ["Wind", function (c) { var k = c.track.conditions || {}; return TK.deg(c.wind.deg) + (k.wind_kn ? " · " + Math.round(k.wind_kn) + "–" + Math.round(k.gust_kn) + " kn" : ""); }],
    ["Gear", function (c) { return (c.track.gear || []).map(function (g) { return g.name; }).join(", ") || "—"; }],
    ["Duration", function (c) { return TK.dur(c.summary.duration_s); }],
    ["Distance (km)", function (c) { return TK.km(c.summary.distance_m); }, function (c) { return c.summary.distance_m; }],
    ["Top speed 2 s (kn)", function (c) { return TK.kn(c.summary.best_2s_kn); }, function (c) { return c.summary.best_2s_kn; }],
    ["Best 10 s (kn)", function (c) { return TK.kn(c.summary.best_10s_kn); }, function (c) { return c.summary.best_10s_kn; }],
    ["Best 500 m (kn)", function (c) { return TK.kn(c.summary.best_500m_kn); }, function (c) { return c.summary.best_500m_kn; }],
    ["Best 1 nm (kn)", function (c) { return TK.kn(c.summary.best_1nm_kn); }, function (c) { return c.summary.best_1nm_kn; }],
    ["Avg moving (kn)", function (c) { return TK.kn(c.summary.avg_moving_kn); }, function (c) { return c.summary.avg_moving_kn; }],
    ["Foiling / planing %", function (c) { return TK.num(c.summary.state_pct, 0); }, function (c) { return c.summary.state_pct; }],
    ["Upwind VMG (kn)", function (c) { return TK.kn(c.summary.upwind_vmg_kn); }, function (c) { return c.summary.upwind_vmg_kn; }],
    ["Upwind TWA", function (c) { return TK.num(c.summary.upwind_twa, 0) + "°"; }, function (c) { return c.summary.upwind_twa === null ? null : -c.summary.upwind_twa; }],
    ["Downwind VMG (kn)", function (c) { return TK.kn(c.summary.downwind_vmg_kn); }, function (c) { return c.summary.downwind_vmg_kn; }],
    ["Tacks (% clean)", function (c) { return c.summary.tacks + " (" + TK.num(c.summary.tack_success_pct, 0) + "%)"; }, function (c) { return c.summary.tack_success_pct; }],
    ["Gybes (% clean)", function (c) { return c.summary.gybes + " (" + TK.num(c.summary.gybe_success_pct, 0) + "%)"; }, function (c) { return c.summary.gybe_success_pct; }],
    ["Tack loss (m)", function (c) { return TK.num(c.summary.tack_loss_m, 0); }, function (c) { return c.summary.tack_loss_m === null ? null : -c.summary.tack_loss_m; }],
    ["Gybe loss (m)", function (c) { return TK.num(c.summary.gybe_loss_m, 0); }, function (c) { return c.summary.gybe_loss_m === null ? null : -c.summary.gybe_loss_m; }],
    ["Avg HR", function (c) { return TK.num(c.summary.hr_avg, 0); }],
  ];

  function renderCompare(ids) {
    root.textContent = "";
    document.body.classList.add("wide");
    root.appendChild(el("a", { class: "tk-back", href: "#tracks", text: "← All tracks" }));
    if (ids.length < 2) { root.appendChild(el("p", { class: "tk-msg err", text: "Pick at least two sessions to compare." })); return; }
    root.appendChild(el("p", { class: "tk-msg", text: "Loading " + ids.length + " sessions…" }));
    TK.api("/api/tracks/compare?ids=" + ids.join(",")).then(function (list) {
      root.textContent = "";
      root.appendChild(el("a", { class: "tk-back", href: "#tracks", text: "← All tracks" }));
      var colors = ["--series-1", "--series-2", "--series-3", "--series-4"].map(TK.css);
      var tp = TK.panel("Side-by-side", "Best value per row is highlighted.");
      var wrap = el("div", { class: "table-scroll", style: { maxHeight: "none" } });
      var t = el("table", { class: "tk-table tk-cmp" });
      var thead = el("thead"), trh = el("tr");
      trh.appendChild(el("th", { text: "" }));
      list.forEach(function (c, i) {
        var th = el("th", { class: "num" });
        th.appendChild(el("span", { class: "tk-key", style: { background: colors[i] } }));
        th.appendChild(el("a", { href: "#track/" + c.track.id, text: c.track.title || "Session " + c.track.id }));
        trh.appendChild(th);
      });
      thead.appendChild(trh);
      t.appendChild(thead);
      var tb = el("tbody");
      CMP_ROWS.forEach(function (row) {
        var tr = el("tr");
        tr.appendChild(el("td", { text: row[0] }));
        var best = null;
        if (row[2]) list.forEach(function (c) { var v = row[2](c); if (v !== null && v !== undefined && (best === null || v > best)) best = v; });
        list.forEach(function (c) {
          var v = row[2] ? row[2](c) : null;
          tr.appendChild(el("td", { class: "num" + (row[2] && v === best && v !== null ? " best" : ""), text: row[1](c) }));
        });
        tb.appendChild(tr);
      });
      t.appendChild(tb);
      wrap.appendChild(t);
      tp.appendChild(wrap);
      root.appendChild(tp);

      var g = el("div", { class: "tk-grid-even" });
      var pp = TK.panel("Polars (p90)", "Each session's p90 speed by TWA.");
      var pbox = el("div", { style: { maxWidth: "460px" } });
      pp.appendChild(pbox);
      TK.polarChart(pbox, list.map(function (c, i) {
        return { label: c.track.start_date + " " + (c.track.spot || ""), color: colors[i], points: c.polar.bins.map(function (b) { return { twa: b.twa, kn: b.p90 }; }) };
      }), { aria: "Polar comparison" });
      g.appendChild(pp);
      var mp = TK.panel("Maneuvers", "Minimum speed vs distance lost, one colour per session.");
      var pts = [];
      list.forEach(function (c, i) {
        c.maneuvers.forEach(function (m) {
          if (m.distance_lost_m === null) return;
          pts.push({ x: m.min_kn, y: m.distance_lost_m, color: colors[i], shape: m.type === "tack" ? "tri" : "dot", hollow: !m.success,
            title: (c.track.title || "") + " · " + m.type, tip: [{ value: TK.kn(m.min_kn) + " kn", label: "min" }, { value: TK.num(m.distance_lost_m, 0) + " m", label: "lost" }] });
        });
      });
      if (pts.length) TK.scatter(mp, pts, { xLabel: "minimum speed (kn)", yLabel: "metres lost", xMin: 0, aria: "Maneuver comparison" });
      mp.appendChild(TK.legend(list.map(function (c, i) { return { label: c.track.start_date + " " + (c.track.spot || ""), color: colors[i] }; })
        .concat([{ label: "▲ tack  ● gybe  hollow = touched down", color: "transparent" }])));
      g.appendChild(mp);
      root.appendChild(g);

      var mapsP = TK.panel("Tracks", "Coloured by speed on a shared scale.");
      var mg = el("div", { class: "tk-cmp-maps" });
      mapsP.appendChild(mg);
      root.appendChild(mapsP);
      var vmax = Math.max.apply(null, list.map(function (c) { return c.summary.best_2s_kn || 10; }));
      vmax = Math.ceil(vmax / 5) * 5;
      Promise.all(list.map(function (c) { return TK.api("/api/tracks/" + c.track.id + "/analysis"); })).then(function (full) {
        full.forEach(function (a, i) {
          var cell = el("div");
          cell.appendChild(el("div", { class: "tk-chart-title", text: (a.track.title || "") + " · " + a.track.start_date }));
          var md = el("div", { class: "tk-map small" });
          cell.appendChild(md);
          mg.appendChild(cell);
          var m = L.map(md, { preferCanvas: true, attributionControl: false, zoomControl: false });
          maps.push(m);
          TK.grayTiles(false).addTo(m);
          var S = a.series, start = 0, pts2 = [];
          function flush(end, k) {
            var p = [];
            for (var j = start; j <= end; j++) p.push([S.lat[j], S.lon[j]]);
            if (p.length > 1) L.polyline(p, { color: TK.speedColor(k), weight: 2.5, interactive: false }).addTo(m);
          }
          var cls = S.kn.map(function (v) { return TK.speedClass(v, vmax); });
          for (var j = 1; j < S.lat.length; j++) {
            if (S.gap[j]) { flush(j - 1, cls[start]); start = j; continue; }
            if (cls[j] !== cls[start]) { flush(j, cls[start]); start = j; }
            if (j % 10 === 0) pts2.push([S.lat[j], S.lon[j]]);
          }
          flush(S.lat.length - 1, cls[start]);
          m.fitBounds(pts2, { padding: [10, 10] });
        });
        var lg = el("div", { class: "tk-legend" });
        lg.appendChild(el("span", { text: "Speed: 0" }));
        var r = el("span", { class: "ramp", "aria-hidden": "true" });
        for (var k = 0; k < TK.speedClasses; k++) r.appendChild(el("i", { style: { background: TK.speedColor(k) } }));
        lg.appendChild(r);
        lg.appendChild(el("span", { text: vmax + " kn" }));
        mapsP.appendChild(lg);
      });
    }).catch(function (e) { root.appendChild(el("p", { class: "tk-msg err", text: e.message })); });
  }

  // ------------------------------------------------------------------ Sessions tab hook
  // Garmin water sessions get an Analyse / Open button (see app.js).
  var byActivity = null;
  function refreshActivityButtons() {
    var btns = document.querySelectorAll(".tk-analyse");
    if (!btns.length) return;
    var p = byActivity ? Promise.resolve(byActivity) : TK.api("/api/tracks").then(function (rows) {
      byActivity = {};
      rows.forEach(function (r) { if (r.activity_id) byActivity[r.activity_id] = r.id; });
      return byActivity;
    });
    p.then(function (m) {
      document.querySelectorAll(".tk-analyse").forEach(function (b) {
        b.textContent = m[b.dataset.aid] ? "Open" : "Analyse";
      });
    }).catch(function () {});
  }
  document.addEventListener("click", function (e) {
    var b = e.target.closest(".tk-analyse");
    if (!b) return;
    var aid = b.dataset.aid;
    if (byActivity && byActivity[aid]) { location.hash = "#track/" + byActivity[aid]; return; }
    b.disabled = true;
    b.textContent = "Downloading…";
    TK.api("/api/tracks/garmin/" + aid, { method: "POST" }).then(function (r) {
      byActivity = null;
      location.hash = "#track/" + r.id;
    }).catch(function (err) {
      b.disabled = false;
      b.textContent = "Analyse";
      alert(err.message);
    });
  });
  new MutationObserver(refreshActivityButtons).observe(document.getElementById("sessionsBody"), { childList: true });

  route();
})();
