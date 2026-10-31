/* Gym tab: history of logged workouts (planned vs done, Garmin HR), session
   detail with editable location/times, per-exercise progress (top weight and
   Epley e1RM) and weekly volume. Logging itself happens on the phone page
   /gym/ (offline-first); this tab only reads and edits. Uses the TK helpers
   from tracks-core.js. */
(function () {
  "use strict";

  var TK = window.TK, el = TK.el;
  var root = document.getElementById("gymRoot");
  var tabBtn = document.querySelector('.tab-btn[data-tab="gym"]');
  var charts = [];
  var state = { sessions: [], locations: [], exercises: [], selected: null, exercise: null };

  function fmtDate(iso) {
    if (!iso) return "—";
    var d = new Date(iso);
    return d.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" });
  }
  function fmtClock(iso) {
    if (!iso) return "—";
    return new Date(iso).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  }
  function toLocalInput(iso) {
    if (!iso) return "";
    var d = new Date(iso), p = function (n) { return String(n).padStart(2, "0"); };
    return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) + "T" + p(d.getHours()) + ":" + p(d.getMinutes());
  }
  function kg(v) { return v === null || v === undefined ? "—" : String(Math.round(v * 10) / 10); }
  function ratio(done, target) { return target ? done + " / " + target : String(done); }
  function setText(x) {
    if (x.duration_sec != null && x.reps == null) return x.duration_sec + " s" + (x.weight_kg ? " @ " + kg(x.weight_kg) : "");
    return (x.reps != null ? x.reps : "?") + (x.weight_kg ? " × " + kg(x.weight_kg) : "");
  }

  function load() {
    TK.destroyCharts(charts);
    charts = [];
    root.textContent = "";
    root.appendChild(el("p", { class: "tk-msg", text: "Loading…" }));
    Promise.all([
      TK.api("/api/gym/sessions?limit=200"),
      TK.api("/api/gym/volume"),
      TK.api("/api/gym/exercises"),
      TK.api("/api/gym/locations"),
    ]).then(function (r) {
      state.sessions = r[0];
      state.locations = r[3];
      state.exercises = r[2].filter(function (e) { return e.sessions > 0; });
      root.textContent = "";
      root.appendChild(headPanel(r[1]));
      var checks = state.sessions.filter(function (x) { return x.garmin_suggestion; });
      if (checks.length) {
        root.appendChild(TK.panel("Check workout times", "Your watch recorded these workouts at different times than " +
          "they were logged. Nothing is changed until you choose.", checks.map(function (x) { return garminBox(x, true); })));
      }
      root.appendChild(historyPanel());
      var detail = el("div", { id: "gymDetail" });
      root.appendChild(detail);
      root.appendChild(volumePanel(r[1]));
      root.appendChild(exercisePanel());
      if (state.selected) showSession(state.selected);
    }).catch(function (e) {
      root.textContent = "";
      root.appendChild(el("p", { class: "tk-msg err", text: "Couldn't load gym data: " + e.message }));
    });
  }

  function headPanel(vol) {
    var plan = vol.plan;
    var sub = plan ? plan.name + " · from " + plan.start_date + " · " + plan.weeks + " weeks" : "No plan imported yet — run python -m app.gym.plan_import";
    var p = TK.panel("Gym", sub, [
      el("p", { class: "panel-sub", text: "Log workouts on your phone (works offline, syncs when the laptop is reachable). Here: history, progress and edits." }),
      el("a", { class: "btn-primary", href: "/gym/", target: "_blank", rel: "noopener", text: "Open the gym logger" }),
    ]);
    return p;
  }

  /* Garmin's start/end for a workout, applied only when the athlete says so. */
  function garminBox(s, withTitle) {
    var g = s.garmin_suggestion;
    var msg = el("span", { class: "tk-msg" });
    function choose(keep) {
      buttons.forEach(function (b) { b.disabled = true; });
      msg.textContent = "Saving…";
      TK.api("/api/gym/sessions/" + encodeURIComponent(s.id) + "/garmin-time" + (keep ? "/dismiss" : ""),
             { method: "POST", body: { activity_id: g.activity_id } })
        .then(function () { state.selected = s.id; load(); })
        .catch(function (e) {
          msg.textContent = "Couldn't save: " + e.message;
          buttons.forEach(function (b) { b.disabled = false; });
        });
    }
    var buttons = [
      el("button", { type: "button", class: "btn-primary", text: "Use Garmin time", onclick: function () { choose(false); } }),
      el("button", { type: "button", class: "chat-tool-btn", text: "Keep mine", onclick: function () { choose(true); } }),
    ];
    var hr = g.avg_hr ? ", HR " + Math.round(g.avg_hr) + (g.max_hr ? " / " + Math.round(g.max_hr) : "") : "";
    var mine = s.duration_min != null ? s.duration_min + " min" : "unfinished";
    return el("div", { class: "gym-garmin" }, [
      withTitle ? el("b", { text: fmtDate(s.started_at) + " · day " + (s.day_code || "—") + (s.location ? " · " + s.location : "") }) : null,
      el("div", { class: "gym-garmin-times" }, [
        el("span", null, ["Logged: ", el("b", { text: fmtClock(s.started_at) + "–" + fmtClock(s.ended_at) }), " (" + mine + ")"]),
        el("span", null, ["Garmin “" + (g.name || "Strength") + "”: ",
          el("b", { text: fmtDate(g.started_at) + " " + fmtClock(g.started_at) + "–" + fmtClock(g.ended_at) }),
          " (" + g.duration_min + " min" + hr + ")"]),
      ]),
      el("div", { class: "gym-edit-row" }, buttons.concat([msg])),
    ]);
  }

  function historyPanel() {
    var cols = [
      { key: "started_at", label: "Date", fmt: function (r) {
        return fmtDate(r.started_at) + " " + fmtClock(r.started_at) + (r.garmin_suggestion ? " ⚠ check time" : "");
      } },
      { key: "day_code", label: "Day" },
      { key: "plan_week", label: "Week", num: true },
      { key: "location", label: "Location" },
      { key: "duration_min", label: "Min", num: true },
      { key: "sets_done", label: "Sets done / plan", num: true, fmt: function (r) { return ratio(r.sets_done, r.sets_planned); } },
      { key: "reps_done", label: "Reps done / target", num: true, fmt: function (r) { return ratio(r.reps_done, r.reps_target); } },
      { key: "volume_kg", label: "Volume kg", num: true },
      { key: "garmin", label: "Garmin HR avg / max", num: true, sortable: false,
        fmt: function (r) { return r.garmin ? Math.round(r.garmin.avg_hr) + " / " + Math.round(r.garmin.max_hr) : "—"; } },
    ];
    var table = TK.table(cols, state.sessions, {
      sortKey: "started_at", sortDir: -1, empty: "No workouts synced yet.",
      onRow: function (r) { showSession(r.id); },
    });
    return TK.panel("Workouts", "Click a row for the sets, planned vs done and edits.", [table]);
  }

  function showSession(id) {
    state.selected = id;
    var box = document.getElementById("gymDetail");
    if (!box) return;
    TK.api("/api/gym/sessions/" + encodeURIComponent(id)).then(function (s) {
      box.textContent = "";
      var stats = [
        "Day " + (s.day_code || "—"), s.location || "—",
        fmtDate(s.started_at) + " " + fmtClock(s.started_at) + "–" + fmtClock(s.ended_at),
        (s.duration_min != null ? s.duration_min + " min" : "in progress"),
        "sets " + ratio(s.sets_done, s.sets_planned), "reps " + ratio(s.reps_done, s.reps_target),
        s.volume_kg + " kg volume",
      ];
      if (s.garmin) {
        stats.push("Garmin: " + s.garmin.duration_min + " min, HR " + Math.round(s.garmin.avg_hr) + " avg / " + Math.round(s.garmin.max_hr) + " max");
      }
      var rows = s.exercises.map(function (e) {
        var reps = e.sets.reduce(function (n, x) { return n + (x.reps || 0); }, 0);
        var feel = e.sets.map(function (x) { return x.rpe; }).filter(Boolean)[0];
        return {
          name: e.name_lt, en: e.name_en, target: e.target || "not in plan", sets: e.sets,
          done: e.sets.length, planned: e.sets_planned, reps: reps, reps_target: e.reps_min ? e.reps_min * e.sets_planned : null,
          feel: feel, superset: e.superset_group,
        };
      });
      var table = TK.table([
        { key: "name", label: "Exercise", sortable: false, fmt: function (r) {
          return el("span", null, [r.name, el("br"), el("small", { class: "panel-sub", text: r.en + (r.superset ? " · superset " + r.superset : "") })]);
        } },
        { key: "target", label: "Plan", sortable: false },
        { key: "sets", label: "Logged sets", sortable: false, fmt: function (r) {
          return r.sets.length ? r.sets.map(function (x) { return setText(x) + (x.completed_as_planned ? "" : "*"); }).join(", ") : "—";
        } },
        { key: "done", label: "Sets", num: true, sortable: false, fmt: function (r) { return ratio(r.done, r.planned); } },
        { key: "reps", label: "Reps", num: true, sortable: false, fmt: function (r) { return r.reps_target ? ratio(r.reps, r.reps_target) : r.reps || "—"; } },
        { key: "feel", label: "Feel 1–5", num: true, sortable: false },
      ], rows, { rowClass: function (r) { return r.done < r.planned ? "gym-short" : null; } });
      box.appendChild(TK.panel("Workout " + fmtDate(s.started_at), stats.join(" · "), [
        table,
        el("p", { class: "panel-sub", text: "* = changed from the plan's pre-filled numbers." }),
        s.notes ? el("p", { class: "gym-note", text: "Note: " + s.notes }) : null,
        s.garmin_suggestion ? garminBox(s, false) : null,
        editForm(s),
      ]));
      box.scrollIntoView({ behavior: "smooth", block: "start" });
    }).catch(function (e) {
      box.textContent = "";
      box.appendChild(el("p", { class: "tk-msg err", text: "Couldn't load the workout: " + e.message }));
    });
  }

  function editForm(s) {
    var loc = el("select", null, state.locations.map(function (l) {
      return el("option", { value: l.id, selected: l.id === s.location_id, text: l.name });
    }));
    var start = el("input", { type: "datetime-local", value: toLocalInput(s.started_at) });
    var end = el("input", { type: "datetime-local", value: toLocalInput(s.ended_at) });
    var notes = el("textarea", { rows: 2 });
    notes.value = s.notes || "";
    var msg = el("span", { class: "tk-msg" });
    var save = el("button", { type: "button", class: "btn-primary", text: "Save changes", onclick: function () {
      var body = { location_id: loc.value, notes: notes.value };
      if (start.value) body.started_at = new Date(start.value).toISOString();
      if (end.value) body.ended_at = new Date(end.value).toISOString();
      msg.textContent = "Saving…";
      TK.api("/api/gym/sessions/" + encodeURIComponent(s.id), { method: "PATCH", body: body }).then(function () {
        msg.textContent = "Saved.";
        load();
      }).catch(function (e) { msg.textContent = "Couldn't save: " + e.message; });
    } });
    function field(label, input) { return el("label", { class: "gym-field" }, [el("span", { text: label }), input]); }
    return el("div", { class: "gym-edit" }, [
      el("h3", { text: "Edit" }),
      el("div", { class: "gym-edit-row" }, [field("Location", loc), field("Start", start), field("End", end)]),
      field("Note", notes),
      el("div", { class: "gym-edit-row" }, [save, msg]),
    ]);
  }

  function volumePanel(vol) {
    var p = TK.panel("Weekly volume", "Σ reps × kg per plan week (per-leg exercises count both legs). Bodyweight and timed sets add no kg.", []);
    if (!vol.weeks.length) {
      p.appendChild(el("p", { class: "tk-msg", text: "Nothing logged yet." }));
      return p;
    }
    var box = el("div", { class: "gym-chart" });
    p.appendChild(box);
    TK.bars(box, vol.weeks.map(function (w) {
      return {
        label: "W" + w.week, value: w.volume_kg, color: TK.css(w.in_plan ? "--series-1" : "--muted-ink"),
        title: "Week " + w.week + " (from " + w.week_start + ")",
        tip: [{ label: "Volume", value: w.volume_kg + " kg" }, { label: "Sessions", value: String(w.sessions) },
              { label: "Sets", value: String(w.sets) }, { label: "Reps", value: String(w.reps) }],
      };
    }), { aria: "Weekly training volume", height: 200 });
    p.appendChild(TK.table([
      { key: "week", label: "Week", num: true },
      { key: "week_start", label: "From" },
      { key: "sessions", label: "Sessions", num: true },
      { key: "sets", label: "Sets", num: true },
      { key: "reps", label: "Reps", num: true },
      { key: "volume_kg", label: "Volume kg", num: true },
    ], vol.weeks, { sortKey: "week", sortDir: 1 }));
    return p;
  }

  function exercisePanel() {
    var p = TK.panel("Exercise progress", "Top weight and estimated 1RM (Epley: kg × (1 + reps/30)) per workout.", []);
    if (!state.exercises.length) {
      p.appendChild(el("p", { class: "tk-msg", text: "Nothing logged yet." }));
      return p;
    }
    if (!state.exercise || !state.exercises.some(function (e) { return e.id === state.exercise; })) {
      // Default to the most-logged exercise with kg to chart.
      state.exercise = state.exercises.slice().sort(function (a, b) {
        return (b.type === "weighted") - (a.type === "weighted") || b.sessions - a.sessions;
      })[0].id;
    }
    var pick = el("select", { "aria-label": "Exercise" }, state.exercises.map(function (e) {
      return el("option", { value: e.id, selected: e.id === state.exercise, text: e.name_lt + " — " + e.name_en + " (" + e.sessions + ")" });
    }));
    var body = el("div");
    pick.addEventListener("change", function () { state.exercise = pick.value; drawExercise(body); });
    p.appendChild(pick);
    p.appendChild(body);
    drawExercise(body);
    return p;
  }

  function drawExercise(body) {
    TK.destroyCharts(charts);
    charts = [];
    body.textContent = "";
    TK.api("/api/gym/exercises/" + encodeURIComponent(state.exercise) + "/history").then(function (h) {
      var rows = h.history;
      var weighted = rows.some(function (r) { return r.top_weight_kg; });
      if (weighted) {
        var box = el("div", { class: "gym-chart" });
        body.appendChild(box);
        var x = rows.map(function (r) { return Math.floor(new Date(r.date + "T12:00:00").getTime() / 1000); });
        charts.push(TK.trendChart(box, x, [
          { label: "Top weight", color: TK.css("--series-1"), data: rows.map(function (r) { return r.top_weight_kg; }), unit: " kg" },
          { label: "e1RM", color: TK.css("--series-2"), data: rows.map(function (r) { return r.e1rm_kg; }), unit: " kg" },
        ], { height: 220 }));
      }
      body.appendChild(TK.table([
        { key: "date", label: "Date", fmt: function (r) { return fmtDate(r.started_at); } },
        { key: "day_code", label: "Day" },
        { key: "sets", label: "Sets", sortable: false, fmt: function (r) { return r.sets.map(setText).join(", "); } },
        { key: "top_weight_kg", label: "Top kg", num: true },
        { key: "e1rm_kg", label: "e1RM kg", num: true },
        { key: "total_reps", label: "Reps", num: true },
        { key: "volume_kg", label: "Volume kg", num: true },
        { key: "rpe", label: "Feel", num: true },
      ], rows, { sortKey: "date", sortDir: -1 }));
    }).catch(function (e) {
      body.appendChild(el("p", { class: "tk-msg err", text: "Couldn't load history: " + e.message }));
    });
  }

  tabBtn.addEventListener("click", load);
})();
