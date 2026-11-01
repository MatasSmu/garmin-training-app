/* Nutrition tab: bodyweight trend vs the coach's plan (chart from
   weight-chart.js), today's protein and kcal, the last 14 days, weekly
   summaries and the expenditure estimate. Logging happens on the phone page
   /food/ (offline-first); this tab only reads. Uses the TK helpers from
   tracks-core.js. */
(function () {
  "use strict";

  var TK = window.TK, el = TK.el;
  var root = document.getElementById("nutritionRoot");
  var tabBtn = document.querySelector('.tab-btn[data-tab="nutrition"]');

  function fmtDate(iso) {
    if (!iso) return "—";
    return new Date(iso + "T12:00:00").toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" });
  }
  function r0(v) { return v === null || v === undefined ? "—" : Math.round(v).toLocaleString("en-US"); }
  function signed(v, nd) { return v === null || v === undefined ? "—" : (v > 0 ? "+" : v < 0 ? "−" : "±") + Math.abs(v).toFixed(nd); }

  function load() {
    root.textContent = "";
    root.appendChild(el("p", { class: "tk-msg", text: "Loading…" }));
    TK.api("/api/nutrition/summary").then(function (s) {
      root.textContent = "";
      root.appendChild(headPanel(s));
      root.appendChild(weightPanel(s.weight));
      root.appendChild(energyPanel(s));
      root.appendChild(daysPanel(s));
      root.appendChild(weeksPanel(s.weeks));
    }).catch(function (e) {
      root.textContent = "";
      root.appendChild(el("p", { class: "tk-msg err", text: "Couldn't load nutrition data: " + e.message }));
    });
  }

  function tile(value, label, cls, test) {
    return el("div", { class: "stat-tile" + (cls ? " " + cls : "") }, [
      el("span", { class: "stat-value mono", text: value, "data-test": test || null }),
      el("span", { class: "stat-label", text: label }),
    ]);
  }

  function headPanel(s) {
    var w = s.weight, t = s.targets, d = s.today;
    return TK.panel("Weight & food", "Goal " + t.goal_weight_kg + " kg by " + fmtDate(t.goal_date) + " (" + signed(t.target_rate_kg_week, 2) +
      " kg/week) · " + r0(t.kcal_min) + "–" + r0(t.kcal_max) + " kcal and " + t.protein_g + " g protein a day — from the coach's plan.", [
      el("div", { class: "hero-stats nut-stats" }, [
        tile(w.trend_kg != null ? w.trend_kg.toFixed(1) + " kg" : "—", "Trend weight" + (w.trend_date ? ", " + fmtDate(w.trend_date) : ""), "accent", "nut-trend"),
        tile(w.rate_kg_week != null ? signed(w.rate_kg_week, 2) + " kg/wk" : "—", "Weekly rate (target " + signed(w.target_rate_kg_week, 1) + ")"),
        tile(w.status_label, w.projected_date ? w.goal_weight_kg + " kg projected " + fmtDate(w.projected_date) : "Status vs plan", "nut-" + w.status, "nut-status"),
        tile(r0(d.protein_g) + " / " + t.protein_g + " g", "Protein today · " + r0(d.kcal) + " kcal"),
      ]),
      el("p", { class: "panel-sub", text: "Log food and type in your weights on the phone (works offline, syncs when the laptop is reachable)." }),
      el("a", { class: "btn-primary", href: "/food/", target: "_blank", rel: "noopener", text: "Open food & weight logger" }),
    ]);
  }

  function weightPanel(w) {
    var children = [];
    if (w.days && w.days.length) {
      children.push(el("div", { class: "nut-chart", html: window.WeightChart.svg(w, { width: 820, height: 300, days: 180 }) }));
      if (w.evening_diff_kg != null) {
        children.push(el("p", { class: "panel-sub", text: "Evening weighs " + signed(w.evening_diff_kg, 1) + " kg vs morning on average (info only; the trend uses mornings)." }));
      }
    } else {
      children.push(el("p", { class: "empty-hint", text: "No weights yet — use the week grid on the phone page." }));
    }
    return TK.panel("Bodyweight", "Morning dots, evening dots (lighter), trend (EMA of mornings) and the plan's line to the goal.", children);
  }

  function energyPanel(s) {
    var e = s.expenditure, t = s.targets, kids = [];
    if (e.basis === "intake_and_weight") {
      kids.push(el("p", null, ["Estimated expenditure ", el("b", { class: "mono", text: "~" + r0(e.expenditure_kcal) + " kcal/day" }),
        " — an estimate from " + e.days_with_both + " days with both food and weight (avg intake " + r0(e.avg_intake_kcal) +
        " kcal, trend " + signed(e.rate_kg_week, 2) + " kg/week)."]));
      kids.push(el("p", null, ["To gain " + signed(t.target_rate_kg_week, 2) + " kg/week: about ", el("b", { class: "mono", text: r0(e.suggested_intake_kcal) + " kcal/day" }),
        " (" + signed(e.suggested_change_kcal, 0) + " kcal vs now)."]));
    } else {
      kids.push(el("p", { class: "panel-sub", text: "The expenditure estimate needs " + e.days_needed + " days with both a morning weight and a full food log (≥ " +
        r0(e.food_day_min_kcal) + " kcal) in the last " + e.window_days + " days — " + e.days_with_both + " so far. Until then the advice is from the weight trend only." }));
    }
    if (e.advice) kids.push(el("p", { class: "nut-advice", "data-test": "nut-advice", text: e.advice.charAt(0).toUpperCase() + e.advice.slice(1) + "." }));
    return TK.panel("Energy", null, kids);
  }

  function daysPanel(s) {
    var t = s.targets;
    var rows = s.last_days.slice().reverse();
    return TK.panel("Last 14 days", "Today is still in progress.", [TK.table([
      { key: "date", label: "Day", fmt: function (r) { return fmtDate(r.date); } },
      { key: "kcal", label: "kcal", num: true, fmt: function (r) { return r.entries ? r0(r.kcal) : "—"; } },
      { key: "protein_g", label: "Protein g", num: true, fmt: function (r) {
        return r.entries ? r0(r.protein_g) + (r.protein_g >= t.protein_g ? " ✓" : "") : "—";
      } },
      { key: "entries", label: "Entries", num: true },
    ], rows, { sortKey: "date", sortDir: -1 })]);
  }

  function weeksPanel(weeks) {
    return TK.panel("Weeks", "Mon–Sun. Averages over days with food logged; trend change from the end of the week before.", [TK.table([
      { key: "week_start", label: "Week", fmt: function (r) { return fmtDate(r.week_start) + " – " + fmtDate(r.week_end); } },
      { key: "avg_kcal", label: "Avg kcal", num: true, fmt: function (r) { return r0(r.avg_kcal); } },
      { key: "avg_protein_g", label: "Avg protein g", num: true },
      { key: "protein_days", label: "Protein target days", num: true, fmt: function (r) { return r.protein_days + " / " + r.food_days; } },
      { key: "weigh_ins", label: "Weigh-ins", num: true },
      { key: "trend_change_kg", label: "Trend Δ kg", num: true, fmt: function (r) { return signed(r.trend_change_kg, 2); } },
      { key: "status", label: "Status", sortable: false, fmt: function (r) { return r.status_label; } },
    ], weeks, { sortKey: "week_start", sortDir: -1, empty: "No data yet." })]);
  }

  tabBtn.addEventListener("click", load);
})();
