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
  function mealsText(r) {
    if (!r || (!r.meals && !r.snacks)) return "—";
    return r.meals + (r.meals === 1 ? " meal" : " meals") + (r.snacks ? " + " + r.snacks + (r.snacks === 1 ? " snack" : " snacks") : "");
  }
  function signed(v, nd) { return v === null || v === undefined ? "—" : (v > 0 ? "+" : v < 0 ? "−" : "±") + Math.abs(v).toFixed(nd); }

  function load() {
    root.textContent = "";
    root.appendChild(el("p", { class: "tk-msg", text: "Loading…" }));
    Promise.all([TK.api("/api/nutrition/summary"), TK.api("/api/nutrition/checkins?days=60"),
      TK.api("/api/nutrition/sheet").catch(function () { return null; })]).then(function (r) {
      var s = r[0];
      root.textContent = "";
      root.appendChild(headPanel(s));
      root.appendChild(weightPanel(s.weight));
      root.appendChild(checkinsPanel(r[1]));
      root.appendChild(energyPanel(s));
      root.appendChild(daysPanel(s));
      root.appendChild(weeksPanel(s.weeks));
      if (r[2]) root.appendChild(sheetPanel(r[2]));
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
        tile(r0(d.carbs_g) + " g · " + r0(d.fat_g) + " g", "Carbs · fat today" +
          (d.protein_pct != null ? " (energy " + d.protein_pct + "% P / " + d.carbs_pct + "% C / " + d.fat_pct + "% F)" : ""), null, "nut-cf"),
        tile(mealsText(d), "Eaten today" + (d.entries ? " · " + d.entries + " entries" : ""), null, "nut-meals"),
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
      { key: "carbs_g", label: "Carbs g", num: true, fmt: function (r) { return r.entries ? r0(r.carbs_g) : "—"; } },
      { key: "fat_g", label: "Fat g", num: true, fmt: function (r) { return r.entries ? r0(r.fat_g) : "—"; } },
      { key: "carbs_pct", label: "Split P/C/F %", sortable: false, fmt: function (r) {
        return r.entries && r.protein_pct != null ? r.protein_pct + " / " + r.carbs_pct + " / " + r.fat_pct : "—";
      } },
      { key: "eating_times", label: "Meals", num: true, fmt: function (r) { return r.entries ? mealsText(r) : "—"; } },
      { key: "entries", label: "Entries", num: true },
    ], rows, { sortKey: "date", sortDir: -1 })]);
  }

  function weeksPanel(weeks) {
    return TK.panel("Weeks", "Mon–Sun. Averages over days with food logged; trend change from the end of the week before.", [TK.table([
      { key: "week_start", label: "Week", fmt: function (r) { return fmtDate(r.week_start) + " – " + fmtDate(r.week_end); } },
      { key: "avg_kcal", label: "Avg kcal", num: true, fmt: function (r) { return r0(r.avg_kcal); } },
      { key: "avg_protein_g", label: "Avg protein g", num: true },
      { key: "avg_carbs_g", label: "Avg carbs g", num: true, fmt: function (r) { return r0(r.avg_carbs_g); } },
      { key: "avg_fat_g", label: "Avg fat g", num: true, fmt: function (r) { return r0(r.avg_fat_g); } },
      { key: "avg_eating_times", label: "Eating times / day", num: true, fmt: function (r) {
        return r.avg_eating_times != null ? String(r.avg_eating_times) : "—"; } },
      { key: "protein_days", label: "Protein target days", num: true, fmt: function (r) { return r.protein_days + " / " + r.food_days; } },
      { key: "weigh_ins", label: "Weigh-ins", num: true },
      { key: "trend_change_kg", label: "Trend Δ kg", num: true, fmt: function (r) { return signed(r.trend_change_kg, 2); } },
      { key: "status", label: "Status", sortable: false, fmt: function (r) { return r.status_label; } },
    ], weeks, { sortKey: "week_start", sortDir: -1, empty: "No data yet." })]);
  }

  function checkinsPanel(rows) {
    var hasExtra = rows.some(function (r) { return r.score != null || r.note; });
    return TK.panel("Check-ins", "Last 60 days of weigh-ins with how you felt — from the phone check-in card and your Google Sheet.", [TK.table([
      { key: "date", label: "Day", fmt: function (r) { return fmtDate(r.date); } },
      { key: "slot", label: "When", fmt: function (r) { return (r.slot === "morning" ? "Morning" : "Evening") + (r.time ? " " + r.time : ""); } },
      { key: "weight_kg", label: "kg", num: true, fmt: function (r) { return r.weight_kg.toFixed(1); } },
      { key: "score", label: "Score", num: true, fmt: function (r) { return r.score != null ? String(r.score) : "—"; } },
      { key: "note", label: "Note", sortable: false, fmt: function (r) { return r.note || ""; } },
    ], rows, { sortKey: "date", sortDir: -1, empty: "No weigh-ins yet." })].concat(
      rows.length && !hasExtra ? [el("p", { class: "panel-sub", text: "No scores or notes yet — add them on the phone or in the sheet." })] : []));
  }

  function fmtWhen(iso) {
    return iso ? new Date(iso).toLocaleString(undefined, { weekday: "short", hour: "2-digit", minute: "2-digit" }) : "";
  }

  var FIELD_LABEL = { weight_kg: "weight", score: "score", note: "note", time: "time" };
  function fmtVal(f, v) { return f === "weight_kg" ? v.toFixed(1) + " kg" : f === "note" ? "“" + v + "”" : String(v); }

  /* Weigh-ins where the sheet says something else than the app: the app's
     value is kept until you pick. */
  function conflictList(conflicts) {
    function resolve(body, btn) {
      btn.disabled = true;
      TK.api("/api/nutrition/sheet/conflicts/resolve", { method: "POST", body: body }).then(load)
        .catch(function (e) { btn.disabled = false; alert(e.message); });
    }
    var rows = conflicts.map(function (c) {
      var useSheet = el("button", { type: "button", class: "btn-ghost", text: "Use sheet" });
      var keepApp = el("button", { type: "button", class: "btn-ghost", text: "Keep app" });
      useSheet.onclick = function () { resolve({ use: "sheet", date: c.date, slot: c.slot }, useSheet); };
      keepApp.onclick = function () { resolve({ use: "app", date: c.date, slot: c.slot }, keepApp); };
      return el("li", { "data-test": "conflict" }, [
        el("b", { text: fmtDate(c.date) + " " + c.slot + ": " }),
        c.fields.map(function (f) {
          return FIELD_LABEL[f.field] + " — app " + fmtVal(f.field, f.app) + ", sheet " + fmtVal(f.field, f.sheet);
        }).join("; ") + " ", useSheet, " ", keepApp]);
    });
    var allSheet = el("button", { type: "button", class: "btn-primary", text: "Use sheet for all" });
    var allApp = el("button", { type: "button", class: "btn-primary", text: "Keep app for all" });
    allSheet.onclick = function () { resolve({ use: "sheet" }, allSheet); };
    allApp.onclick = function () { resolve({ use: "app" }, allApp); };
    return el("div", { class: "sheet-conflicts" }, [
      el("p", { class: "nut-advice", text: conflicts.length + (conflicts.length === 1 ? " weigh-in differs" : " weigh-ins differ") +
        " between the sheet and the app. The app's value is kept until you choose." }),
      el("ul", null, rows),
      el("div", { class: "plan-actions" }, [allSheet, allApp]),
    ]);
  }

  /* Google Sheet import settings: the service-account e-mail to share the
     sheet with, the sheet link, the last result and Import now. */
  function sheetPanel(st) {
    var kids = [];
    var msg = el("p", { class: "panel-sub", "data-test": "sheet-msg" });
    if (!st.key_present) {
      kids.push(el("p", { class: "nut-advice", text: "Not set up yet: save the Google service-account JSON key as " + st.key_path +
        " (see docs/google-sheet-import.md), then reload this tab." }));
    } else if (st.key_error) {
      kids.push(el("p", { class: "tk-msg err", text: st.key_error }));
    } else {
      kids.push(el("p", null, ["Share your sheet (Share → Viewer) with ", el("b", { class: "mono", text: st.service_account_email }), " ",
        el("button", { type: "button", class: "btn-ghost", text: "Copy", onclick: function (e) {
          navigator.clipboard.writeText(st.service_account_email).then(function () { e.target.textContent = "Copied"; });
        } })]));
    }
    var url = el("input", { type: "text", placeholder: "https://docs.google.com/spreadsheets/d/…", value: st.sheet_url || "", "aria-label": "Sheet link" });
    var tab = el("input", { type: "text", placeholder: "Tab (optional)", value: st.tab || "", "aria-label": "Tab name", class: "sheet-tab" });
    var save = el("button", { type: "button", class: "btn-primary", text: "Save link", onclick: function () {
      msg.className = "panel-sub"; msg.textContent = "Saving…";
      TK.api("/api/nutrition/sheet", { method: "PUT", body: { url: url.value, tab: tab.value || null } }).then(function () {
        return runImport();
      }).catch(function (e) { msg.className = "tk-msg err"; msg.textContent = e.message; });
    } });
    var imp = el("button", { type: "button", class: "btn-primary", text: "Import now", disabled: !st.configured || !st.key_present,
      onclick: function () { runImport(); } });
    function runImport() {
      imp.disabled = true; msg.className = "panel-sub"; msg.textContent = "Importing…";
      return TK.api("/api/nutrition/sheet/import", { method: "POST" }).then(function (r) {
        if (r.result.ok) load(); else { showLast(r.result); imp.disabled = false; }
      }).catch(function (e) { msg.className = "tk-msg err"; msg.textContent = e.message; imp.disabled = false; });
    }
    function showLast(last) {
      if (!last) { msg.textContent = st.configured ? "Not imported yet." : ""; return; }
      msg.className = last.ok ? "panel-sub" : "tk-msg err";
      msg.textContent = (last.ok ? "Last import " : "Last import failed ") + fmtWhen(last.finished_at) +
        (last.source === "auto" ? " (auto)" : "") + ": " + last.message;
    }
    kids.push(el("div", { class: "plan-actions sheet-form" }, [url, tab, save, imp]));
    showLast(st.last);
    kids.push(msg);

    // When the sheet and the app hold different values for the same weigh-in.
    var modes = [["ask", "Ask me"], ["sheet", "Always use the sheet"], ["app", "Always keep the app's value"]];
    var modeSel = el("select", { "aria-label": "When the sheet and the app differ", "data-test": "conflict-mode",
      onchange: function () {
        TK.api("/api/nutrition/sheet/conflict-mode", { method: "PUT", body: { mode: modeSel.value } }).then(load)
          .catch(function (e) { msg.className = "tk-msg err"; msg.textContent = e.message; });
      } }, modes.map(function (m) { return el("option", { value: m[0], text: m[1], selected: st.conflict_mode === m[0] }); }));
    kids.push(el("div", { class: "plan-actions sheet-mode" }, [el("span", { text: "When the sheet and the app differ:" }), modeSel]));
    if (st.conflicts && st.conflicts.length) kids.push(conflictList(st.conflicts));
    var last = st.last;
    if (last && last.ok && last.skipped && last.skipped.length) {
      kids.push(el("details", null, [el("summary", { text: last.skipped_count + " rows skipped" }),
        el("ul", null, last.skipped.map(function (x) { return el("li", { text: "Row " + x.row + ": " + x.reason }); }))]));
    }
    if (last && last.ok && last.columns) {
      kids.push(el("p", { class: "panel-sub", text: "Columns used: " + Object.keys(last.columns).map(function (k) {
        return k + " = “" + last.columns[k] + "”"; }).join(", ") + "." }));
    }
    return TK.panel("Google Sheet import", "Reads date, time, weight, morning score and note from your private sheet" +
      (st.import_times && st.import_times.length ? ", automatically at " + st.import_times.join(" and ") +
        " (catches up if the laptop was off)" : "") +
      ". New and changed rows are added; nothing in the app is deleted.", kids, { "data-test": "sheet-panel" });
  }

  tabBtn.addEventListener("click", load);
})();
