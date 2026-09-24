/* Session analysis -- shared helpers: API, formatting, tables, charts. */
(function () {
  "use strict";

  var TK = (window.TK = window.TK || {});

  // ------------------------------------------------------------------ api
  TK.api = function (path, opts) {
    opts = opts || {};
    var init = { method: opts.method || "GET", headers: {} };
    if (opts.body !== undefined) {
      if (opts.body instanceof FormData) {
        init.body = opts.body;
      } else {
        init.headers["Content-Type"] = "application/json";
        init.body = JSON.stringify(opts.body);
      }
    }
    return fetch(path, init).then(function (r) {
      return r.text().then(function (txt) {
        var data = null;
        try { data = txt ? JSON.parse(txt) : null; } catch (e) { data = txt; }
        if (!r.ok) {
          var msg = data && data.detail ? data.detail : r.status + " " + r.statusText;
          if (Array.isArray(msg)) msg = msg.map(function (d) { return d.msg; }).join("; ");
          throw new Error(msg);
        }
        return data;
      });
    });
  };

  // ------------------------------------------------------------------ dom
  TK.el = function (tag, attrs, children) {
    var e = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        var v = attrs[k];
        if (v === null || v === undefined || v === false) return;
        if (k === "text") e.textContent = v;
        else if (k === "html") e.innerHTML = v;
        else if (k === "class") e.className = v;
        else if (k.slice(0, 2) === "on" && typeof v === "function") e.addEventListener(k.slice(2), v);
        else if (k === "style" && typeof v === "object") Object.assign(e.style, v);
        else e.setAttribute(k, v === true ? "" : v);
      });
    }
    (children || []).forEach(function (c) {
      if (c === null || c === undefined || c === false) return;
      e.appendChild(typeof c === "string" || typeof c === "number" ? document.createTextNode(String(c)) : c);
    });
    return e;
  };
  var el = TK.el;

  TK.esc = function (s) {
    return String(s === null || s === undefined ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  };

  TK.css = function (name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "#888";
  };

  TK.panel = function (title, sub, children, attrs) {
    var p = el("div", Object.assign({ class: "panel" }, attrs || {}));
    if (title) p.appendChild(el("h2", { text: title }));
    if (sub) p.appendChild(el("p", { class: "panel-sub", text: sub }));
    (children || []).forEach(function (c) { if (c) p.appendChild(c); });
    return p;
  };

  // ------------------------------------------------------------------ format
  TK.num = function (v, nd) {
    if (v === null || v === undefined || isNaN(v)) return "—";
    return Number(v).toFixed(nd === undefined ? 1 : nd);
  };
  TK.kn = function (v, nd) { return TK.num(v, nd === undefined ? 1 : nd); };
  TK.dur = function (s) {
    if (s === null || s === undefined || isNaN(s)) return "—";
    s = Math.round(s);
    var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    if (h) return h + ":" + String(m).padStart(2, "0") + ":" + String(sec).padStart(2, "0");
    return m + ":" + String(sec).padStart(2, "0");
  };
  TK.hm = function (s) {
    if (s === null || s === undefined) return "—";
    var h = Math.floor(s / 3600), m = Math.round((s % 3600) / 60);
    return h ? h + "h " + String(m).padStart(2, "0") + "m" : m + " min";
  };
  TK.km = function (m) { return m === null || m === undefined ? "—" : (m / 1000).toFixed(1); };
  var COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"];
  TK.compass = function (d) { return d === null || d === undefined ? "—" : COMPASS[Math.round(((d % 360) + 360) % 360 / 22.5) % 16]; };
  TK.deg = function (d) { return d === null || d === undefined ? "—" : Math.round(d) + "° " + TK.compass(d); };
  // spot-local wall clock from epoch + offset
  TK.clock = function (epoch, offset, withSec) {
    var d = new Date((epoch + (offset || 0)) * 1000);
    var s = String(d.getUTCHours()).padStart(2, "0") + ":" + String(d.getUTCMinutes()).padStart(2, "0");
    return withSec ? s + ":" + String(d.getUTCSeconds()).padStart(2, "0") : s;
  };
  TK.localDateTimeInput = function (epoch, offset) {
    var d = new Date((epoch + (offset || 0)) * 1000);
    return d.toISOString().slice(0, 19);
  };
  TK.fromLocalInput = function (value, offset) {
    if (!value) return null;
    var ms = Date.parse(value.length === 16 ? value + ":00Z" : value + "Z");
    return isNaN(ms) ? null : ms / 1000 - (offset || 0);
  };
  TK.SPORT_LABEL = { windfoil: "Windfoil", wingfoil: "Wingfoil", kitefoil: "Kitefoil", windsurf: "Windsurf", kite: "Kite", sail: "Sailing" };
  TK.GEAR_LABEL = { board: "Board", front_wing: "Front wing", back_wing: "Back wing", foil_mast: "Foil mast", fuselage: "Fuselage",
    sail: "Sail", mast: "Mast", boom: "Boom", fin: "Fin", foil: "Foil", wing: "Wing", kite: "Kite", other: "Other" };
  // iQFOIL baseline metrics: [key, label, decimals, higher is better]
  TK.SAIL_METRICS = [["vmg_up_kn", "VMG upwind (kn)", 2, true], ["vmg_down_kn", "VMG downwind (kn)", 2, true],
    ["max_kn", "Max speed 2 s (kn)", 1, true], ["angle_up_deg", "Upwind angle (°)", 1, false]];
  TK.bandLabel = function (b) { return b ? b.replace("-", "–") + " kn" : "—"; };
  TK.gearText = function (g) { return (TK.GEAR_LABEL[g.kind] || g.kind) + ": " + (g.code || g.name) + (g.code && g.name && g.name !== g.code ? " (" + g.name + ")" : ""); };

  // Esri's gray canvas basemap: quiet enough for coloured tracks, no API key.
  TK.grayTiles = function (dark) {
    return L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/" + (dark ? "World_Dark_Gray_Base" : "World_Light_Gray_Base") + "/MapServer/tile/{z}/{y}/{x}",
      { maxZoom: 20, maxNativeZoom: 16, attribution: "Tiles © Esri — Esri, HERE, Garmin, © OpenStreetMap contributors" });
  };

  // ------------------------------------------------------------------ tooltip
  var tipEl = null;
  TK.tip = function (evt, rows, title) {
    if (!tipEl) {
      tipEl = el("div", { class: "tk-tip", role: "tooltip" });
      document.body.appendChild(tipEl);
    }
    tipEl.textContent = "";
    if (title) tipEl.appendChild(el("div", { class: "nm", text: title }));
    rows.forEach(function (r) {
      var row = el("div", { class: "row" });
      if (r.color) row.appendChild(el("span", { class: "tk-key", style: { background: r.color } }));
      row.appendChild(el("b", { text: r.value }));
      if (r.label) row.appendChild(el("span", { class: "nm", text: r.label }));
      tipEl.appendChild(row);
    });
    tipEl.style.display = "block";
    var x = evt.clientX + 14, y = evt.clientY + 14;
    var w = tipEl.offsetWidth, h = tipEl.offsetHeight;
    if (x + w > window.innerWidth - 8) x = evt.clientX - w - 14;
    if (y + h > window.innerHeight - 8) y = evt.clientY - h - 14;
    tipEl.style.left = x + "px";
    tipEl.style.top = y + "px";
  };
  TK.untip = function () { if (tipEl) tipEl.style.display = "none"; };

  // ------------------------------------------------------------------ sortable table
  /* cols: [{key, label, num, fmt(row)->string|Node, sort(row)->value, title}] */
  TK.table = function (cols, rows, opts) {
    opts = opts || {};
    var state = { key: opts.sortKey || null, dir: opts.sortDir || 1 };
    var wrap = el("div", { class: "table-scroll", style: opts.maxHeight ? { maxHeight: opts.maxHeight } : null });
    var table = el("table", { class: "tk-table" });
    var thead = el("thead"), tbody = el("tbody");
    table.appendChild(thead);
    table.appendChild(tbody);
    wrap.appendChild(table);

    function val(c, r) { return c.sort ? c.sort(r) : r[c.key]; }

    function renderHead() {
      thead.textContent = "";
      var tr = el("tr");
      cols.forEach(function (c) {
        var th = el("th", { class: c.num ? "num" : null, title: c.title || null, scope: "col" });
        if (c.key && c.sortable !== false) {
          th.dataset.key = c.key;
          th.tabIndex = 0;
          th.setAttribute("aria-sort", state.key === c.key ? (state.dir > 0 ? "ascending" : "descending") : "none");
        }
        th.appendChild(document.createTextNode(c.label));
        if (state.key === c.key) th.appendChild(el("span", { class: "arrow", text: state.dir > 0 ? "▲" : "▼" }));
        var onSort = function () {
          if (!c.key || c.sortable === false) return;
          if (state.key === c.key) state.dir = -state.dir;
          else { state.key = c.key; state.dir = c.num ? -1 : 1; }
          render();
        };
        th.addEventListener("click", onSort);
        th.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onSort(); } });
        tr.appendChild(th);
      });
      thead.appendChild(tr);
    }

    function render() {
      renderHead();
      tbody.textContent = "";
      var list = rows.slice();
      if (state.key) {
        var c = cols.filter(function (x) { return x.key === state.key; })[0];
        if (c) {
          list.sort(function (a, b) {
            var va = val(c, a), vb = val(c, b);
            if (va === null || va === undefined) return 1;
            if (vb === null || vb === undefined) return -1;
            return (va > vb ? 1 : va < vb ? -1 : 0) * state.dir;
          });
        }
      }
      if (!list.length) {
        tbody.appendChild(el("tr", null, [el("td", { colspan: cols.length, class: "empty-hint txt", text: opts.empty || "Nothing here." })]));
        return;
      }
      list.forEach(function (r) {
        var tr = el("tr", { class: opts.onRow ? "clickable" : null });
        if (opts.rowClass) { var rc = opts.rowClass(r); if (rc) tr.classList.add(rc); }
        cols.forEach(function (c) {
          var v = c.fmt ? c.fmt(r) : r[c.key];
          var td = el("td", { class: (c.num ? "num" : "txt") + (c.cls ? " " + c.cls : "") });
          if (v instanceof Node) td.appendChild(v);
          else td.textContent = v === null || v === undefined ? "—" : String(v);
          tr.appendChild(td);
        });
        if (opts.onRow) {
          tr.tabIndex = 0;
          tr.addEventListener("click", function (e) {
            if (e.target.closest("input,button,a")) return;
            opts.onRow(r, tr);
          });
          tr.addEventListener("keydown", function (e) { if (e.key === "Enter") opts.onRow(r, tr); });
        }
        tbody.appendChild(tr);
      });
    }
    render();
    wrap.refresh = function (newRows) { if (newRows) rows = newRows; render(); };
    return wrap;
  };

  // ------------------------------------------------------------------ color scales
  TK.speedClasses = 8;
  TK.speedColor = function (k) { return TK.css("--seq-" + (k + 1)); };
  TK.speedClass = function (v, vmax) {
    if (v === null || v === undefined) return 0;
    return Math.max(0, Math.min(TK.speedClasses - 1, Math.floor((v / vmax) * TK.speedClasses)));
  };
  var DIV = ["--div-neg-3", "--div-neg-2", "--div-neg-1", "--div-mid", "--div-pos-1", "--div-pos-2", "--div-pos-3"];
  TK.divColor = function (k) { return TK.css(DIV[k]); };
  TK.divClass = function (v, lim) {
    if (v === null || v === undefined) return 3;
    var r = v / lim;
    if (r < -0.6) return 0;
    if (r < -0.3) return 1;
    if (r < -0.08) return 2;
    if (r <= 0.08) return 3;
    if (r <= 0.3) return 4;
    if (r <= 0.6) return 5;
    return 6;
  };
  TK.percentile = function (arr, p) {
    var a = arr.filter(function (v) { return v !== null && !isNaN(v); }).sort(function (x, y) { return x - y; });
    if (!a.length) return 0;
    return a[Math.min(a.length - 1, Math.floor(p * a.length))];
  };

  // ------------------------------------------------------------------ uPlot helpers
  function axisStyle() {
    return {
      stroke: TK.css("--muted-ink"),
      grid: { stroke: TK.css("--grid"), width: 1 },
      ticks: { stroke: TK.css("--grid"), width: 1, size: 4 },
      font: "10.5px IBM Plex Mono, monospace",
    };
  }
  TK.axisStyle = axisStyle;

  /* Track all charts that should resize with their container. */
  TK.fitWidth = function (chart, container, height) {
    var ro = new ResizeObserver(function () {
      var w = container.clientWidth;
      if (w > 0 && Math.abs(w - chart.width) > 2) chart.setSize({ width: w, height: height });
    });
    ro.observe(container);
    chart._ro = ro;
  };
  TK.destroyCharts = function (list) {
    (list || []).forEach(function (c) { if (c._ro) c._ro.disconnect(); c.destroy(); });
  };

  /* Date-axis chart for trends (x = epoch seconds, local calendar dates). */
  TK.trendChart = function (container, x, series, opts) {
    opts = opts || {};
    var h = opts.height || 200;
    var o = {
      width: container.clientWidth || 600, height: h,
      scales: { x: { time: true }, y: opts.yRange ? { range: opts.yRange } : {} },
      axes: [axisStyle(), Object.assign(axisStyle(), { size: 48, values: opts.yFmt ? function (u, v) { return v.map(opts.yFmt); } : undefined })],
      series: [{ value: function (u, v) { return v === null ? "—" : new Date(v * 1000).toISOString().slice(0, 10); } }].concat(series.map(function (s) {
        return {
          label: s.label, stroke: s.color, width: 2, spanGaps: true,
          points: { show: true, size: 8, width: 2, fill: s.color, stroke: TK.css("--surface") },
          value: function (u, v) { return v === null || v === undefined ? "—" : v.toFixed(s.nd === undefined ? 1 : s.nd) + (s.unit || ""); },
        };
      })),
      legend: { show: true, live: true },
      cursor: { points: { size: 10 } },
    };
    var chart = new uPlot(o, [x].concat(series.map(function (s) { return s.data; })), container);
    TK.fitWidth(chart, container, h);
    if (opts.onClick) {
      chart.over.addEventListener("click", function () {
        var i = chart.cursor.idx;
        if (i !== null && i !== undefined) opts.onClick(i);
      });
      chart.over.style.cursor = "pointer";
    }
    return chart;
  };

  // ------------------------------------------------------------------ SVG charts
  var SVGNS = "http://www.w3.org/2000/svg";
  function s(tag, attrs, parent) {
    var e = document.createElementNS(SVGNS, tag);
    Object.keys(attrs || {}).forEach(function (k) { e.setAttribute(k, attrs[k]); });
    if (parent) parent.appendChild(e);
    return e;
  }
  TK.svg = s;
  function txt(parent, x, y, str, attrs) {
    var t = s("text", Object.assign({ x: x, y: y }, attrs || {}), parent);
    t.textContent = str;
    return t;
  }
  TK.svgText = txt;

  TK.legend = function (items) {
    var lg = el("div", { class: "tk-legend" });
    items.forEach(function (it) {
      var span = el("span");
      if (it.shape === "dot") span.appendChild(el("span", { class: "tk-dot", style: { background: it.color, border: it.hollow ? "2px solid " + it.color : null, backgroundColor: it.hollow ? "transparent" : it.color } }));
      else if (it.shape === "sym") span.appendChild(el("span", { class: "sym", text: it.sym + " " }));
      else span.appendChild(el("span", { class: "tk-key", style: { background: it.color } }));
      span.appendChild(document.createTextNode(it.label));
      lg.appendChild(span);
    });
    return lg;
  };

  /* Half polar: |TWA| 0 (up) .. 180 (down) on the right-hand side, radius = speed (kn).
     series: [{label, color, points:[{twa, kn}], width}]; marks: [{twa, kn, label}] */
  TK.polarChart = function (container, series, opts) {
    opts = opts || {};
    var W = 360, H = 400, cx = 40, cy = 200, R = 180;
    var vmax = 5;
    series.forEach(function (sr) { sr.points.forEach(function (p) { if (p.kn > vmax) vmax = p.kn; }); });
    vmax = Math.ceil(vmax / 5) * 5;
    var svg = s("svg", { viewBox: "0 0 " + W + " " + H, class: "tk-svg", role: "img", "aria-label": opts.aria || "Polar diagram" });
    function pt(twa, kn) {
      var a = (twa * Math.PI) / 180, r = (kn / vmax) * R;
      return [cx + r * Math.sin(a), cy - r * Math.cos(a)];
    }
    for (var k = 5; k <= vmax; k += 5) {
      var r = (k / vmax) * R;
      s("path", { d: "M " + cx + " " + (cy - r) + " A " + r + " " + r + " 0 0 1 " + cx + " " + (cy + r), class: "gridline" }, svg);
      txt(svg, cx - 4, cy - r + 4, k, { "text-anchor": "end" });
    }
    for (var a = 0; a <= 180; a += 30) {
      var p = pt(a, vmax);
      s("line", { x1: cx, y1: cy, x2: p[0], y2: p[1], class: a % 90 === 0 ? "axisline" : "gridline" }, svg);
      var lp = pt(a, vmax * 1.07);
      txt(svg, lp[0], lp[1] + 4, a + "°", { "text-anchor": a === 0 || a === 180 ? "middle" : "start" });
    }
    txt(svg, cx - 4, cy + 16, "kn", { "text-anchor": "end" });
    series.forEach(function (sr) {
      var pts = sr.points.filter(function (q) { return q.kn !== null && q.kn !== undefined; });
      if (!pts.length) return;
      var d = pts.map(function (q, i) { var c = pt(q.twa, q.kn); return (i ? "L" : "M") + c[0].toFixed(1) + " " + c[1].toFixed(1); }).join(" ");
      s("path", { d: d, fill: "none", stroke: sr.color, "stroke-width": sr.width || 2, "stroke-linejoin": "round", "stroke-linecap": "round", opacity: sr.opacity || 1 }, svg);
    });
    (opts.marks || []).forEach(function (m) {
      if (!m || m.kn === null || m.kn === undefined) return;
      var c = pt(m.twa, m.kn);
      s("circle", { cx: c[0], cy: c[1], r: 5, fill: m.color || TK.css("--track-ink"), stroke: TK.css("--surface"), "stroke-width": 2 }, svg);
      txt(svg, c[0] + 8, c[1] + (m.twa < 90 ? -6 : 14), m.label, { class: "lbl" });
    });
    // hover: nearest TWA bin across all series
    var hit = s("rect", { x: cx, y: cy - R - 10, width: R + 20, height: 2 * R + 20, class: "hit" }, svg);
    var cursor = s("line", { x1: cx, y1: cy, x2: cx, y2: cy, stroke: TK.css("--muted-ink"), "stroke-width": 1, visibility: "hidden" }, svg);
    hit.addEventListener("pointermove", function (e) {
      var box = svg.getBoundingClientRect();
      var sx = ((e.clientX - box.left) / box.width) * W - cx, sy = cy - ((e.clientY - box.top) / box.height) * H;
      var twa = Math.max(0, Math.min(180, (Math.atan2(sx, sy) * 180) / Math.PI));
      var e2 = pt(twa, vmax);
      cursor.setAttribute("x2", e2[0]); cursor.setAttribute("y2", e2[1]); cursor.setAttribute("visibility", "visible");
      var rows = [];
      series.forEach(function (sr) {
        var best = null;
        sr.points.forEach(function (q) { if (q.kn !== null && (best === null || Math.abs(q.twa - twa) < Math.abs(best.twa - twa))) best = q; });
        if (best && Math.abs(best.twa - twa) <= 5) rows.push({ color: sr.color, value: best.kn.toFixed(1) + " kn", label: sr.label });
      });
      TK.tip(e, rows.length ? rows : [{ value: "no data" }], "TWA " + Math.round(twa) + "°");
    });
    hit.addEventListener("pointerleave", function () { TK.untip(); cursor.setAttribute("visibility", "hidden"); });
    container.appendChild(svg);
    if (series.length > 1) container.appendChild(TK.legend(series.map(function (sr) { return { label: sr.label, color: sr.color }; })));
    return svg;
  };

  /* Scatter: points [{x, y, color, hollow, shape, tip:[rows], title}] */
  TK.scatter = function (container, points, opts) {
    var W = 520, H = 300, m = { l: 46, r: 12, t: 12, b: 34 };
    var xs = points.map(function (p) { return p.x; }), ys = points.map(function (p) { return p.y; });
    var x0 = opts.xMin !== undefined ? opts.xMin : Math.min.apply(null, xs.concat([0]));
    var x1 = Math.max.apply(null, xs.concat([1])) * 1.05;
    var y0 = Math.min.apply(null, ys.concat([0])), y1 = Math.max.apply(null, ys.concat([1])) * 1.08;
    function X(v) { return m.l + ((v - x0) / (x1 - x0 || 1)) * (W - m.l - m.r); }
    function Y(v) { return H - m.b - ((v - y0) / (y1 - y0 || 1)) * (H - m.t - m.b); }
    var svg = s("svg", { viewBox: "0 0 " + W + " " + H, class: "tk-svg", role: "img", "aria-label": opts.aria || "Scatter plot" });
    function ticks(lo, hi, n) {
      var step = Math.pow(10, Math.floor(Math.log10((hi - lo) / n || 1)));
      [1, 2, 5, 10].some(function (f) { if ((hi - lo) / (step * f) <= n) { step *= f; return true; } return false; });
      var out = [];
      for (var v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(+v.toFixed(6));
      return out;
    }
    ticks(y0, y1, 5).forEach(function (v) {
      s("line", { x1: m.l, x2: W - m.r, y1: Y(v), y2: Y(v), class: v === 0 ? "axisline" : "gridline" }, svg);
      txt(svg, m.l - 6, Y(v) + 3.5, v, { "text-anchor": "end" });
    });
    ticks(x0, x1, 6).forEach(function (v) {
      s("line", { x1: X(v), x2: X(v), y1: m.t, y2: H - m.b, class: "gridline" }, svg);
      txt(svg, X(v), H - m.b + 14, v, { "text-anchor": "middle" });
    });
    txt(svg, (m.l + W - m.r) / 2, H - 4, opts.xLabel || "", { "text-anchor": "middle" });
    txt(svg, 10, m.t + 4, opts.yLabel || "", {});
    var surface = TK.css("--surface");
    points.forEach(function (p) {
      var cx = X(p.x), cy = Y(p.y);
      var mark;
      if (p.shape === "tri") {
        mark = s("path", { d: "M" + cx + " " + (cy - 6) + " L" + (cx + 5.5) + " " + (cy + 4) + " L" + (cx - 5.5) + " " + (cy + 4) + " Z" }, svg);
      } else {
        mark = s("circle", { cx: cx, cy: cy, r: 4.5 }, svg);
      }
      mark.setAttribute("fill", p.hollow ? surface : p.color);
      mark.setAttribute("stroke", p.hollow ? p.color : surface);
      mark.setAttribute("stroke-width", 2);
      var hit = s("circle", { cx: cx, cy: cy, r: 12, class: "hit", tabindex: 0 }, svg);
      hit.addEventListener("pointermove", function (e) { TK.tip(e, p.tip, p.title); });
      hit.addEventListener("pointerleave", TK.untip);
      hit.addEventListener("focus", function () { var b = hit.getBoundingClientRect(); TK.tip({ clientX: b.right, clientY: b.bottom }, p.tip, p.title); });
      hit.addEventListener("blur", TK.untip);
      if (opts.onClick) {
        hit.addEventListener("click", function () { opts.onClick(p); });
        hit.addEventListener("keydown", function (e) { if (e.key === "Enter") opts.onClick(p); });
      }
    });
    container.appendChild(svg);
    return svg;
  };

  /* Vertical bars from a zero baseline: [{label, value, color, tip, title, data}] */
  TK.bars = function (container, bars, opts) {
    opts = opts || {};
    var W = 520, H = opts.height || 220, m = { l: 46, r: 8, t: 10, b: 26 };
    var vals = bars.map(function (b) { return b.value || 0; });
    var lo = Math.min.apply(null, vals.concat([0])), hi = Math.max.apply(null, vals.concat([0]));
    if (hi === lo) hi = lo + 1;
    var pad = (hi - lo) * 0.08;
    lo = lo < 0 ? lo - pad : 0; hi = hi > 0 ? hi + pad : 0;
    function Y(v) { return m.t + ((hi - v) / (hi - lo)) * (H - m.t - m.b); }
    var svg = s("svg", { viewBox: "0 0 " + W + " " + H, class: "tk-svg", role: "img", "aria-label": opts.aria || "Bar chart" });
    var n = bars.length || 1, band = (W - m.l - m.r) / n, bw = Math.min(24, band - 2);
    var stepV = Math.pow(10, Math.floor(Math.log10((hi - lo) / 4)));
    [1, 2, 5, 10].some(function (f) { if ((hi - lo) / (stepV * f) <= 5) { stepV *= f; return true; } return false; });
    for (var v = Math.ceil(lo / stepV) * stepV; v <= hi + 1e-9; v += stepV) {
      s("line", { x1: m.l, x2: W - m.r, y1: Y(v), y2: Y(v), class: Math.abs(v) < 1e-9 ? "axisline" : "gridline" }, svg);
      txt(svg, m.l - 6, Y(v) + 3.5, +v.toFixed(3), { "text-anchor": "end" });
    }
    var surface = TK.css("--surface");
    bars.forEach(function (b, i) {
      var x = m.l + i * band + (band - bw) / 2, v = b.value || 0;
      var y0 = Y(0), y1 = Y(v), top = Math.min(y0, y1), h = Math.max(1, Math.abs(y1 - y0));
      var rr = Math.min(4, h / 2), up = v >= 0;
      // rounded data end, square at the baseline
      var d = up
        ? "M" + x + " " + (top + h) + " V" + (top + rr) + " Q" + x + " " + top + " " + (x + rr) + " " + top + " H" + (x + bw - rr) + " Q" + (x + bw) + " " + top + " " + (x + bw) + " " + (top + rr) + " V" + (top + h) + " Z"
        : "M" + x + " " + top + " V" + (top + h - rr) + " Q" + x + " " + (top + h) + " " + (x + rr) + " " + (top + h) + " H" + (x + bw - rr) + " Q" + (x + bw) + " " + (top + h) + " " + (x + bw) + " " + (top + h - rr) + " V" + top + " Z";
      s("path", { d: d, fill: b.color, stroke: surface, "stroke-width": 1 }, svg);
      if (n <= 24 || i % Math.ceil(n / 24) === 0) txt(svg, x + bw / 2, H - m.b + 14, b.label, { "text-anchor": "middle" });
      var hit = s("rect", { x: m.l + i * band, y: m.t, width: band, height: H - m.t - m.b, class: "hit", tabindex: 0 }, svg);
      hit.addEventListener("pointermove", function (e) { TK.tip(e, b.tip || [{ value: String(v) }], b.title); });
      hit.addEventListener("pointerleave", TK.untip);
      if (opts.onClick) hit.addEventListener("click", function () { opts.onClick(b); });
    });
    container.appendChild(svg);
    return svg;
  };

  /* Heading rose (72 x 5 deg bins, minutes) with the wind arrow. */
  TK.rose = function (container, rose, windDeg) {
    var W = 260, C = 130, R = 110;
    var svg = s("svg", { viewBox: "0 0 " + W + " " + W, class: "tk-svg", role: "img", "aria-label": "Time spent on each heading" });
    var mx = Math.max.apply(null, rose.concat([0.01]));
    [0.33, 0.66, 1].forEach(function (f) { s("circle", { cx: C, cy: C, r: R * f, class: "gridline" }, svg); });
    ["N", "E", "S", "W"].forEach(function (l, i) {
      var a = (i * 90 * Math.PI) / 180;
      txt(svg, C + (R + 12) * Math.sin(a), C - (R + 12) * Math.cos(a) + 4, l, { "text-anchor": "middle" });
    });
    var color = TK.css("--series-1"), surface = TK.css("--surface");
    rose.forEach(function (v, i) {
      if (!v) return;
      var a0 = ((i * 5 + 0.4) * Math.PI) / 180, a1 = (((i + 1) * 5 - 0.4) * Math.PI) / 180, r = (v / mx) * R;
      var d = "M" + C + " " + C + " L" + (C + r * Math.sin(a0)) + " " + (C - r * Math.cos(a0)) +
        " A" + r + " " + r + " 0 0 1 " + (C + r * Math.sin(a1)) + " " + (C - r * Math.cos(a1)) + " Z";
      var p = s("path", { d: d, fill: color, stroke: surface, "stroke-width": 0.5 }, svg);
      p.addEventListener("pointermove", function (e) { TK.tip(e, [{ value: v.toFixed(1) + " min", label: "heading " + i * 5 + "–" + (i * 5 + 5) + "°" }]); });
      p.addEventListener("pointerleave", TK.untip);
    });
    if (windDeg !== null && windDeg !== undefined) {
      var a = (windDeg * Math.PI) / 180, ink = TK.css("--track-ink");
      var x0 = C + (R + 2) * Math.sin(a), y0 = C - (R + 2) * Math.cos(a), x1 = C + 0.45 * R * Math.sin(a), y1 = C - 0.45 * R * Math.cos(a);
      s("line", { x1: x0, y1: y0, x2: x1, y2: y1, stroke: ink, "stroke-width": 2.5, "stroke-linecap": "round" }, svg);
      var ah = 8, bx = Math.sin(a), by = -Math.cos(a), px = -by, py = bx;
      s("path", { d: "M" + x1 + " " + y1 + " L" + (x1 + bx * ah + px * ah * 0.6) + " " + (y1 + by * ah + py * ah * 0.6) + " L" + (x1 + bx * ah - px * ah * 0.6) + " " + (y1 + by * ah - py * ah * 0.6) + " Z", fill: ink }, svg);
    }
    container.appendChild(svg);
    return svg;
  };

  /* Small numeric-x line chart (e.g. HR vs speed). */
  TK.lineChart = function (container, pts, opts) {
    var W = 520, H = 200, m = { l: 42, r: 10, t: 10, b: 30 };
    if (!pts.length) { container.appendChild(el("p", { class: "tk-hint", text: "No data." })); return; }
    var xs = pts.map(function (p) { return p.x; }), ys = pts.map(function (p) { return p.y; });
    var x0 = Math.min.apply(null, xs), x1 = Math.max.apply(null, xs);
    var y0 = Math.floor(Math.min.apply(null, ys) / 10) * 10, y1 = Math.ceil(Math.max.apply(null, ys) / 10) * 10;
    if (y1 === y0) y1 = y0 + 10;
    function X(v) { return m.l + ((v - x0) / ((x1 - x0) || 1)) * (W - m.l - m.r); }
    function Y(v) { return H - m.b - ((v - y0) / (y1 - y0)) * (H - m.t - m.b); }
    var svg = s("svg", { viewBox: "0 0 " + W + " " + H, class: "tk-svg", role: "img", "aria-label": opts.aria || "Line chart" });
    for (var v = y0; v <= y1; v += Math.max(5, Math.round((y1 - y0) / 4 / 5) * 5)) {
      s("line", { x1: m.l, x2: W - m.r, y1: Y(v), y2: Y(v), class: "gridline" }, svg);
      txt(svg, m.l - 6, Y(v) + 3.5, v, { "text-anchor": "end" });
    }
    pts.forEach(function (p, i) { if (i % Math.ceil(pts.length / 10) === 0) txt(svg, X(p.x), H - m.b + 14, p.x, { "text-anchor": "middle" }); });
    txt(svg, (m.l + W) / 2, H - 3, opts.xLabel || "", { "text-anchor": "middle" });
    var color = opts.color || TK.css("--series-1"), surface = TK.css("--surface");
    s("path", { d: pts.map(function (p, i) { return (i ? "L" : "M") + X(p.x) + " " + Y(p.y); }).join(" "), fill: "none", stroke: color, "stroke-width": 2, "stroke-linejoin": "round" }, svg);
    pts.forEach(function (p) {
      s("circle", { cx: X(p.x), cy: Y(p.y), r: 4, fill: color, stroke: surface, "stroke-width": 2 }, svg);
      var hit = s("circle", { cx: X(p.x), cy: Y(p.y), r: 12, class: "hit" }, svg);
      hit.addEventListener("pointermove", function (e) { TK.tip(e, p.tip, p.title); });
      hit.addEventListener("pointerleave", TK.untip);
    });
    container.appendChild(svg);
  };
})();
