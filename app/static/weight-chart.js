/* Bodyweight chart as inline SVG, shared by the phone page (/food/, works
   offline -- no chart library) and the Nutrition tab. Morning weights are
   solid dots, evening weights lighter dots, the EMA trend a line and the
   plan's target a dashed line from start weight to goal weight. Colours come
   from the page's CSS tokens, so dark mode follows automatically.

   WeightChart.svg(analysis, {days: 120, today: "YYYY-MM-DD"}) -> SVG string,
   where `analysis` is /api/nutrition/... "weight". */
(function () {
  "use strict";

  var MS_DAY = 86400000;
  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  function ensureStyle() {
    if (document.getElementById("wc-style")) return;
    var st = document.createElement("style");
    st.id = "wc-style";
    st.textContent =
      ".wc{width:100%;height:auto;display:block;font:11px system-ui,sans-serif}" +
      ".wc-grid{stroke:var(--border);stroke-width:1}" +
      ".wc-axis{fill:var(--ink-muted)}" +
      ".wc-m{fill:var(--teal)}" +
      ".wc-e{fill:var(--teal);opacity:.32}" +
      ".wc-trend{fill:none;stroke:var(--accent);stroke-width:2.5;stroke-linejoin:round;stroke-linecap:round}" +
      ".wc-target{fill:none;stroke:var(--plan);stroke-width:1.6;stroke-dasharray:6 5}" +
      ".wc-goal{fill:var(--plan)}" +
      ".wc-legend{display:flex;flex-wrap:wrap;gap:4px 14px;font-size:12px;color:var(--ink-muted);margin-top:6px}" +
      ".wc-legend i{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:5px;vertical-align:-1px}" +
      ".wc-legend .k-m{background:var(--teal)}.wc-legend .k-e{background:var(--teal);opacity:.32}" +
      ".wc-legend .k-t{border-radius:2px;height:3px;width:14px;background:var(--accent);vertical-align:3px}" +
      ".wc-legend .k-g{border-radius:0;height:0;width:14px;border-top:2px dashed var(--plan);vertical-align:3px}";
    document.head.appendChild(st);
  }

  function t(iso) { return Date.parse(iso + "T00:00:00Z"); }
  function iso(ms) { return new Date(ms).toISOString().slice(0, 10); }

  function svg(a, opts) {
    ensureStyle();
    opts = opts || {};
    var W = opts.width || 640, H = opts.height || 280;
    var pad = { l: 38, r: 10, t: 10, b: 24 };
    var today = opts.today || iso(Date.now());
    var span = opts.days || 120;
    var days = (a && a.days) || [];
    if (!days.length) return "";

    var last = days[days.length - 1].date;
    var x1 = Math.max(t(last), t(today));
    if (a.goal_date && t(a.goal_date) > x1 && t(a.goal_date) - x1 < 70 * MS_DAY) x1 = t(a.goal_date);
    var x0 = Math.max(t(days[0].date), x1 - span * MS_DAY);
    if (a.start_date && t(a.start_date) < x0 && t(a.start_date) > x1 - span * MS_DAY) x0 = t(a.start_date);
    if (x1 - x0 < 14 * MS_DAY) x0 = x1 - 14 * MS_DAY;

    function targetAt(ms) {
      var s = t(a.start_date), e = t(a.goal_date);
      return a.start_weight_kg + (a.goal_weight_kg - a.start_weight_kg) * (ms - s) / (e - s);
    }
    var hasTarget = a.start_date && a.goal_date && a.start_weight_kg != null && a.goal_weight_kg != null;
    var tgt = [];
    if (hasTarget) {
      var ts = Math.max(x0, t(a.start_date)), te = Math.min(x1, t(a.goal_date));
      if (te > ts) tgt = [[ts, targetAt(ts)], [te, targetAt(te)]];
    }

    var shown = days.filter(function (d) { return t(d.date) >= x0; });
    var vals = [];
    shown.forEach(function (d) { [d.morning, d.evening, d.trend].forEach(function (v) { if (v != null) vals.push(v); }); });
    tgt.forEach(function (p) { vals.push(p[1]); });
    if (!vals.length) return "";
    var y0 = Math.min.apply(null, vals), y1 = Math.max.apply(null, vals);
    if (y1 - y0 < 2) { var mid = (y0 + y1) / 2; y0 = mid - 1; y1 = mid + 1; }
    y0 = Math.floor((y0 - 0.2) * 2) / 2; y1 = Math.ceil((y1 + 0.2) * 2) / 2;

    function X(ms) { return pad.l + (ms - x0) / (x1 - x0) * (W - pad.l - pad.r); }
    function Y(v) { return pad.t + (y1 - v) / (y1 - y0) * (H - pad.t - pad.b); }
    function f(n) { return n.toFixed(1); }

    var out = ['<svg class="wc" viewBox="0 0 ' + W + " " + H + '" role="img" aria-label="Bodyweight chart">'];
    var step = y1 - y0 > 8 ? 2 : y1 - y0 > 4 ? 1 : 0.5;
    for (var v = Math.ceil(y0 / step) * step; v <= y1 + 1e-9; v += step) {
      out.push('<line class="wc-grid" x1="' + pad.l + '" x2="' + (W - pad.r) + '" y1="' + f(Y(v)) + '" y2="' + f(Y(v)) + '"/>');
      out.push('<text class="wc-axis" x="' + (pad.l - 5) + '" y="' + f(Y(v) + 4) + '" text-anchor="end">' + (step < 1 ? v.toFixed(1) : v) + "</text>");
    }
    // x labels: Mondays, thinned to fit
    var mondays = [];
    for (var ms = x0; ms <= x1; ms += MS_DAY) if (new Date(ms).getUTCDay() === 1) mondays.push(ms);
    var every = Math.max(1, Math.ceil(mondays.length / 6));
    mondays.forEach(function (m, i) {
      if (i % every) return;
      var d = new Date(m);
      out.push('<text class="wc-axis" x="' + f(X(m)) + '" y="' + (H - 6) + '" text-anchor="middle">' + d.getUTCDate() + " " + MONTHS[d.getUTCMonth()] + "</text>");
    });
    if (tgt.length) {
      out.push('<path class="wc-target" d="M' + f(X(tgt[0][0])) + " " + f(Y(tgt[0][1])) + "L" + f(X(tgt[1][0])) + " " + f(Y(tgt[1][1])) + '"/>');
      if (tgt[1][0] === t(a.goal_date)) out.push('<circle class="wc-goal" cx="' + f(X(tgt[1][0])) + '" cy="' + f(Y(tgt[1][1])) + '" r="3.5"/>');
    }
    shown.forEach(function (d) {
      if (d.evening != null) out.push('<circle class="wc-e" cx="' + f(X(t(d.date))) + '" cy="' + f(Y(d.evening)) + '" r="3.2"/>');
    });
    shown.forEach(function (d) {
      if (d.morning != null) out.push('<circle class="wc-m" cx="' + f(X(t(d.date))) + '" cy="' + f(Y(d.morning)) + '" r="3.2"/>');
    });
    var pts = shown.filter(function (d) { return d.trend != null; }).map(function (d) { return f(X(t(d.date))) + " " + f(Y(d.trend)); });
    if (pts.length > 1) out.push('<path class="wc-trend" d="M' + pts.join("L") + '"/>');
    out.push("</svg>");
    out.push('<div class="wc-legend"><span><i class="k-m"></i>Morning</span><span><i class="k-e"></i>Evening</span>' +
      '<span><i class="k-t"></i>Trend</span>' + (tgt.length ? '<span><i class="k-g"></i>Plan to ' + a.goal_weight_kg + " kg</span>" : "") + "</div>");
    return out.join("");
  }

  window.WeightChart = { svg: svg };
})();
