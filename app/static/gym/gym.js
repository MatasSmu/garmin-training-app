/* Gym logger -- offline-first. Everything the athlete does is written to
   IndexedDB on every tap (an in-progress workout survives the app being
   closed or the phone locking) and pushed to /api/gym/sync whenever the server
   answers: on load, on the `online` event, every 30 s and shortly after a
   change. The plan, locations and last numbers come from /api/gym/bootstrap and
   are cached for offline use. Session/set/location ids are UUIDs made here, and
   the server upserts by them, so re-sending never duplicates anything.

   Every API call has a short timeout: with the laptop off in a backpack the
   phone is usually *online* (mobile data) but the server just isn't there, so
   navigator.onLine can't be trusted. */
(function () {
  "use strict";

  var API_TIMEOUT_MS = 6000;
  var SYNC_TIMEOUT_MS = 12000;
  var SYNC_EVERY_MS = 30000;
  var REOPEN_FINISHED_MS = 3 * 3600 * 1000;
  var STEP = { reps: 1, weight_kg: 2.5, duration_sec: 5 };
  var WD = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  var main = document.getElementById("main");
  var BOOT = null;            // cached /api/gym/bootstrap
  var SESSIONS = [];          // every session stored on this phone
  var LOCS = [];              // locations added on this phone: {id, name, synced}
  var VIEW = { day: null, finishedId: null, editId: null, addingLoc: false };
  var draft = null;           // not-yet-started session for the selected day
  var sync = { busy: false, error: null, timer: null };

  // ================================================================ IndexedDB
  var dbPromise = null;
  function db() {
    if (!dbPromise) {
      dbPromise = new Promise(function (resolve, reject) {
        var req = indexedDB.open("gym-tracker", 1);
        req.onupgradeneeded = function () {
          var d = req.result;
          d.createObjectStore("kv");
          d.createObjectStore("sessions", { keyPath: "id" });
          d.createObjectStore("locations", { keyPath: "id" });
        };
        req.onsuccess = function () { resolve(req.result); };
        req.onerror = function () { reject(req.error); };
      });
    }
    return dbPromise;
  }
  function store(name, mode, fn) {
    return db().then(function (d) {
      return new Promise(function (resolve, reject) {
        var t = d.transaction(name, mode);
        var req = fn(t.objectStore(name));
        t.oncomplete = function () { resolve(req ? req.result : undefined); };
        t.onerror = t.onabort = function () { reject(t.error); };
      });
    });
  }
  var idb = {
    get: function (name, key) { return store(name, "readonly", function (s) { return s.get(key); }); },
    all: function (name) { return store(name, "readonly", function (s) { return s.getAll(); }); },
    put: function (name, value, key) {
      return store(name, "readwrite", function (s) { return key === undefined ? s.put(value) : s.put(value, key); });
    },
    del: function (name, key) { return store(name, "readwrite", function (s) { return s.delete(key); }); },
  };

  // ================================================================ helpers
  function uuid() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    var b = crypto.getRandomValues(new Uint8Array(16));
    b[6] = (b[6] & 15) | 64; b[8] = (b[8] & 63) | 128;
    var h = Array.prototype.map.call(b, function (x) { return (x + 256).toString(16).slice(1); }).join("");
    return h.slice(0, 8) + "-" + h.slice(8, 12) + "-" + h.slice(12, 16) + "-" + h.slice(16, 20) + "-" + h.slice(20);
  }
  function esc(s) {
    return String(s === null || s === undefined ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function num(v) { return v === null || v === undefined || v === "" ? "" : String(Math.round(v * 100) / 100); }
  function nowIso() { return new Date().toISOString(); }
  function weekdayIdx(d) { return (d.getDay() + 6) % 7; }
  function fmtDate(iso) {
    var d = new Date(iso);
    return WD[weekdayIdx(d)] + " " + d.getDate() + " " + MONTHS[d.getMonth()];
  }
  function fmtTime(iso) {
    var d = new Date(iso);
    return String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
  }
  function toLocalInput(iso) {
    if (!iso) return "";
    var d = new Date(iso);
    var p = function (n) { return String(n).padStart(2, "0"); };
    return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) + "T" + p(d.getHours()) + ":" + p(d.getMinutes());
  }
  function fromLocalInput(v) { return v ? new Date(v).toISOString() : null; }
  function minutesBetween(a, b) { return a && b ? Math.round((new Date(b) - new Date(a)) / 60000) : null; }
  function parseNum(v) {
    if (v === null || v === undefined) return null;
    var n = parseFloat(String(v).replace(",", "."));
    return isNaN(n) ? null : n;
  }

  function api(path, opts, ms) {
    opts = opts || {};
    var ctl = new AbortController();
    var timer = setTimeout(function () { ctl.abort(); }, ms || API_TIMEOUT_MS);
    return fetch(path, {
      method: opts.method || "GET",
      headers: opts.body ? { "Content-Type": "application/json" } : {},
      body: opts.body ? JSON.stringify(opts.body) : undefined,
      signal: ctl.signal,
      credentials: "same-origin",
      cache: "no-store",
    }).then(function (r) {
      if (!r.ok) {
        var err = new Error(r.status === 401 ? "sign-in needed" : "HTTP " + r.status);
        err.status = r.status;
        throw err;
      }
      return r.json();
    }).finally(function () { clearTimeout(timer); });
  }

  // ================================================================ plan lookups
  function days() { return (BOOT && BOOT.days) || []; }
  function dayByCode(code) { return days().find(function (d) { return d.code === code; }) || null; }
  function dayById(id) { return days().find(function (d) { return d.id === id; }) || null; }
  function exercise(id) { return (BOOT && BOOT.exercises[id]) || { id: id, name_lt: id, type: "weighted" }; }
  function allLocations() {
    var list = ((BOOT && BOOT.locations) || []).slice();
    LOCS.forEach(function (l) { if (!list.some(function (x) { return x.id === l.id; })) list.push(l); });
    return list;
  }
  function locationName(id) {
    var l = allLocations().find(function (x) { return x.id === id; });
    return l ? l.name : "—";
  }
  function todaysDay() {
    var today = weekdayIdx(new Date());
    var best = null, bestGap = 8;
    days().forEach(function (d) {
      var gap = (d.weekday - today + 7) % 7;
      if (gap < bestGap) { best = d; bestGap = gap; }
    });
    return best;
  }

  /* Which inputs a set has, by exercise type. kg: "req" | "opt" | false. */
  function fieldsFor(ex) {
    switch (ex.type) {
      case "timed": return { reps: false, sec: true, kg: false };
      case "timed_weighted": return { reps: false, sec: true, kg: "req" };
      case "bodyweight": return { reps: true, sec: false, kg: "opt" };
      case "bodyweight_plus": return { reps: true, sec: false, kg: "opt" };
      default: return { reps: true, sec: false, kg: "req" };
    }
  }

  function targetText(item, ex) {
    function rng(lo, hi, unit) {
      if (lo == null && hi == null) return null;
      if (lo == null || hi == null || lo === hi) return num(lo != null ? lo : hi) + (unit || "");
      return num(lo) + "–" + num(hi) + (unit || "");
    }
    var kg = rng(item.weight_kg_min, item.weight_kg_max, " kg") || (item.weight_kg ? num(item.weight_kg) + " kg" : null);
    var body;
    if (item.reps_min != null || item.reps_max != null) body = rng(item.reps_min, item.reps_max) + (ex.per_side ? " /leg" : "");
    else body = rng(item.duration_sec_min, item.duration_sec_max, " s") || (item.duration_sec ? item.duration_sec + " s" : "");
    return item.sets + " × " + body + (kg ? " @ " + kg : "");
  }

  /* Most recent logged sets of an exercise: the server's numbers or a newer
     session still on this phone, whichever is later. */
  function lastFor(exId, excludeId) {
    var best = BOOT && BOOT.last && BOOT.last[exId] && BOOT.last[exId].session_id !== excludeId ? BOOT.last[exId] : null;
    SESSIONS.forEach(function (s) {
      if (s.id === excludeId || !s.started_at) return;
      if (best && best.started_at >= s.started_at) return;
      var sets = doneSets(s).filter(function (x) { return x.exercise_id === exId; });
      if (sets.length) best = { session_id: s.id, started_at: s.started_at, day_code: s.day_code, sets: sets };
    });
    return best;
  }
  function lastText(last, ex) {
    if (!last) return "No history yet";
    var parts = last.sets.map(function (x) {
      if (x.duration_sec != null && x.reps == null) return x.duration_sec + " s" + (x.weight_kg ? " @" + num(x.weight_kg) : "");
      return (x.reps != null ? x.reps : "?") + (x.weight_kg ? "×" + num(x.weight_kg) : "");
    });
    var unit = last.sets.some(function (x) { return x.weight_kg && x.reps != null; }) ? " kg" : "";
    return "Last (" + (last.day_code || "–") + ", " + fmtDate(last.started_at) + "): " + parts.join(" · ") + unit;
  }

  /* Plan values a set starts with: top of the rep range, the planned kg (low
     end of a range) or, when the plan has none, the last logged kg. */
  function prefill(item, ex, idx, last) {
    var f = fieldsFor(ex);
    var lastSet = last && (last.sets[idx] || last.sets[last.sets.length - 1]);
    var kg = item.weight_kg != null ? item.weight_kg : item.weight_kg_min;
    if (kg == null && f.kg) kg = lastSet && lastSet.weight_kg != null ? lastSet.weight_kg : (f.kg === "opt" ? 0 : null);
    return {
      reps: f.reps ? (item.reps_max != null ? item.reps_max : item.reps_min) : null,
      weight_kg: f.kg ? kg : null,
      duration_sec: f.sec ? (item.duration_sec != null ? item.duration_sec : item.duration_sec_min) : null,
    };
  }

  // ================================================================ sessions
  function newSession(day) {
    return {
      id: uuid(), plan_day_id: day.id, day_code: day.code, location_id: day.default_location_id,
      started_at: null, ended_at: null, notes: "", updated_at: nowIso(), rev: 0, synced_rev: 0,
      slots: {}, extra: {}, rpe: {}, rest: null,
    };
  }
  function liveSession() {
    return SESSIONS.find(function (s) { return s.started_at && !s.ended_at; }) || null;
  }
  function byId(id) { return SESSIONS.find(function (s) { return s.id === id; }) || null; }
  function slotKey(pos, idx) { return pos + ":" + idx; }

  /* Persist a change. A draft (not started yet) only lives in memory. */
  function save(s) {
    if (!s.started_at) return Promise.resolve();
    s.rev = (s.rev || 0) + 1;
    s.updated_at = nowIso();
    if (!byId(s.id)) SESSIONS.push(s);
    var p = idb.put("sessions", s).catch(function (e) { showError("Couldn't save on the phone: " + e.message); });
    scheduleSync();
    renderSync();
    return p;
  }

  /* The sets a session has ticked, in the shape the server stores them. Each
     slot carries its own exercise and set number, so a later change to the
     plan can't drop logged sets from a re-sync. */
  function doneSets(s) {
    return Object.keys(s.slots).map(function (k) {
      var p = k.split(":");
      return { pos: +p[0], idx: +p[1], sl: s.slots[k] };
    }).filter(function (x) { return x.sl.done; }).sort(function (a, b) {
      return a.pos - b.pos || a.idx - b.idx;
    }).map(function (x) {
      var sl = x.sl;
      return {
        id: sl.id, exercise_id: sl.exercise_id, set_number: sl.set_number,
        reps: sl.reps, weight_kg: sl.weight_kg, duration_sec: sl.duration_sec,
        rpe: (s.rpe && s.rpe[sl.exercise_id]) || null,
        completed_as_planned: !!sl.as_planned, done_at: sl.done_at,
      };
    });
  }
  function payload(s) {
    return {
      id: s.id, plan_day_id: s.plan_day_id, location_id: s.location_id, started_at: s.started_at,
      ended_at: s.ended_at, notes: s.notes || null, updated_at: s.updated_at, sets: doneSets(s),
    };
  }
  function pending() {
    return SESSIONS.filter(function (s) { return s.started_at && s.rev > (s.synced_rev || 0); });
  }

  /* The session shown for the selected day. */
  function shown() {
    var live = liveSession();
    if (live && live.day_code === VIEW.day) return live;
    if (VIEW.finishedId) {
      var f = byId(VIEW.finishedId);
      if (f && f.day_code === VIEW.day) return f;
    }
    if (!draft || draft.day_code !== VIEW.day) {
      var day = dayByCode(VIEW.day);
      draft = day ? newSession(day) : null;
    }
    return draft;
  }

  // ================================================================ sync
  function scheduleSync(ms) {
    clearTimeout(sync.timer);
    sync.timer = setTimeout(syncNow, ms === undefined ? 2500 : ms);
  }

  function syncNow() {
    if (sync.busy) return Promise.resolve();
    var dirty = pending();
    var locs = LOCS.filter(function (l) { return !l.synced; });
    if (!dirty.length && !locs.length) {
      // Nothing to send: still refresh the plan/last numbers when we can.
      return refreshBoot();
    }
    sync.busy = true;
    renderSync();
    var sent = dirty.map(function (s) { return { s: s, rev: s.rev }; });
    var body = {
      locations: locs.map(function (l) { return { id: l.id, name: l.name }; }),
      sessions: dirty.map(payload),
    };
    return api("/api/gym/sync", { method: "POST", body: body }, SYNC_TIMEOUT_MS).then(function (res) {
      var writes = [];
      sent.forEach(function (x) {
        if (x.s.rev === x.rev) { x.s.synced_rev = x.rev; writes.push(idb.put("sessions", x.s)); }
      });
      // The server had a newer copy (edited on the laptop): take its session fields.
      (res.server_copies || []).forEach(function (c) {
        var s = byId(c.id);
        if (!s) return;
        ["plan_day_id", "location_id", "started_at", "ended_at", "notes", "updated_at"].forEach(function (k) { s[k] = c[k]; });
        s.notes = s.notes || "";
        var d = dayById(s.plan_day_id);
        if (d) s.day_code = d.code;
        s.synced_rev = s.rev;
        writes.push(idb.put("sessions", s));
      });
      locs.forEach(function (l) { l.synced = true; writes.push(idb.put("locations", l)); });
      if (BOOT) {
        BOOT.locations = res.locations;
        BOOT.last = res.last;
        writes.push(idb.put("kv", BOOT, "boot"));
      }
      sync.error = null;
      return Promise.all(writes);
    }).catch(function (e) {
      sync.error = e;
    }).finally(function () {
      sync.busy = false;
      renderSync();
      if (pending().length && !sync.error) scheduleSync(500);  // changed while sending
    });
  }

  var lastBootAt = 0;
  function refreshBoot(force) {
    if (!force && BOOT && Date.now() - lastBootAt < 120000) return Promise.resolve();
    return api("/api/gym/bootstrap").then(function (fresh) {
      var old = BOOT;
      BOOT = fresh;
      lastBootAt = Date.now();
      sync.error = null;
      // Re-render only when the plan or locations changed: a periodic refresh
      // must not rebuild the page under the athlete's thumb.
      var changed = !old || JSON.stringify(old.days) !== JSON.stringify(fresh.days) ||
        JSON.stringify(old.locations) !== JSON.stringify(fresh.locations);
      return idb.put("kv", fresh, "boot").then(function () {
        if (changed && !isTyping()) render();
        else renderSync();
      });
    }).catch(function (e) {
      sync.error = e;
      renderSync();
    });
  }

  function renderSync() {
    var el = document.getElementById("syncStatus");
    var n = pending().length + LOCS.filter(function (l) { return !l.synced; }).length;
    var nSess = pending().length;
    el.className = "sync-pill";
    if (sync.busy) {
      el.textContent = "Syncing…";
      el.classList.add("busy");
    } else if (n) {
      var what = nSess ? nSess + (nSess === 1 ? " session" : " sessions") : n + " change" + (n === 1 ? "" : "s");
      el.textContent = what + " waiting to sync" + (sync.error && sync.error.status === 401 ? " · sign in" : "");
      el.classList.add("wait");
    } else {
      el.textContent = "All synced";
      el.classList.add("ok");
    }
    el.title = sync.error ? "Server not reachable (" + sync.error.message + ") — tap to retry" : "Tap to sync now";
  }

  // ================================================================ rendering
  function isTyping() {
    var a = document.activeElement;
    return a && (a.tagName === "INPUT" || a.tagName === "TEXTAREA" || a.tagName === "SELECT");
  }

  function showError(msg) {
    var p = document.createElement("p");
    p.className = "gmsg err";
    p.textContent = msg;
    main.prepend(p);
  }

  function renderDays() {
    var live = liveSession();
    document.getElementById("dayTabs").innerHTML = days().map(function (d) {
      return '<button type="button" class="day-tab' + (d.code === VIEW.day ? " active" : "") +
        (live && live.day_code === d.code ? " live" : "") + '" data-act="day" data-day="' + d.code + '">' +
        esc(d.code) + "<small>" + WD[d.weekday] + "</small></button>";
    }).join("");
    var wk = document.getElementById("planWeek");
    if (BOOT && BOOT.plan) {
      var start = new Date(BOOT.plan.start_date + "T00:00:00");
      var w = Math.floor((new Date() - start) / (7 * 864e5)) + 1;
      wk.textContent = w < 1 ? "Plan starts " + fmtDate(start) : w > BOOT.plan.weeks ? "Plan finished" : "Week " + w + " / " + BOOT.plan.weeks;
    }
  }

  function render() {
    renderSync();
    if (!BOOT || !days().length) {
      main.innerHTML = '<p class="gmsg">' + (BOOT ? "No plan loaded on the server yet." :
        "Open this page once while the server is reachable — after that it works offline.") + "</p>";
      return;
    }
    if (!VIEW.day || !dayByCode(VIEW.day)) {
      var live = liveSession();
      VIEW.day = live ? live.day_code : todaysDay().code;
    }
    renderDays();
    var s = shown();
    var day = dayByCode(VIEW.day);
    var live2 = liveSession();
    var readOnly = !!(live2 && live2 !== s);
    var html = [];
    if (readOnly) {
      html.push('<div class="banner">Day ' + esc(live2.day_code) + " workout is in progress." +
        '<button type="button" class="btn" data-act="day" data-day="' + esc(live2.day_code) + '">Back to ' + esc(live2.day_code) + "</button></div>");
    }
    html.push(sessionCard(s, day, readOnly));
    if (!s.ended_at) {
      html.push(exerciseList(s, day, readOnly));
      if (!readOnly) {
        html.push('<div class="card"><label class="field">Session note<textarea data-act="note" rows="3" placeholder="How did it go?">' +
          esc(s.notes) + "</textarea></label>" +
          (s.started_at ? '<button type="button" class="big-btn finish" id="finishBtn" data-act="finish">Finish workout</button>' : "") +
          "</div>");
      }
    }
    html.push(historyCard());
    main.innerHTML = html.join("");
    renderRest();
  }

  function locationField(s, disabled) {
    var opts = allLocations().map(function (l) {
      return '<option value="' + esc(l.id) + '"' + (l.id === s.location_id ? " selected" : "") + ">" + esc(l.name) + "</option>";
    }).join("");
    var html = '<label class="field">Location<select data-act="location"' + (disabled ? " disabled" : "") + ">" + opts +
      '<option value="__new">+ Add new location…</option></select></label>';
    if (VIEW.addingLoc && !disabled) {
      html += '<div class="row" style="margin-top:8px"><input type="text" id="newLocName" maxlength="80" placeholder="Gym name" ' +
        'style="min-height:48px;border:1px solid var(--border);border-radius:10px;padding:0 10px;background:var(--surface)">' +
        '<button type="button" class="btn primary" data-act="add-loc" style="flex:none">Save</button>' +
        '<button type="button" class="btn" data-act="cancel-loc" style="flex:none">✕</button></div>';
    }
    return html;
  }

  function sessionCard(s, day, readOnly) {
    var head = '<div class="sess-head"><h2>' + esc(day.name || "Day " + day.code) + "</h2>" +
      '<span class="sess-sub">' + esc([day.focus, day.default_time, day.duration_min ? day.duration_min + " min" : null].filter(Boolean).join(" · ")) + "</span></div>";
    if (s.ended_at) return finishedCard(s, head);
    var body = locationField(s, readOnly);
    if (!s.started_at) {
      body += readOnly ? "" : '<button type="button" class="big-btn" id="startBtn" data-act="start">Start workout</button>';
    } else {
      body += '<div class="row" style="margin-top:12px"><div><div class="sess-sub">Started</div>' +
        '<div class="clock mono" id="startedClock">' + fmtTime(s.started_at) + "</div></div>" +
        '<div><div class="sess-sub">Elapsed</div><div class="clock mono" id="elapsed">' + elapsedText(s) + "</div></div>" +
        '<div><div class="sess-sub">Sets</div><div class="clock mono" id="setCount">' + doneSets(s).length + "</div></div></div>";
    }
    return '<div class="card" id="sessionCard">' + head + body + "</div>";
  }

  function elapsedText(s) {
    var m = Math.max(0, Math.floor((Date.now() - new Date(s.started_at)) / 60000));
    return Math.floor(m / 60) + ":" + String(m % 60).padStart(2, "0");
  }

  function editFields(s) {
    return locationField(s, false) +
      '<div class="row"><label class="field">Start<input type="datetime-local" data-act="edit-start" value="' + toLocalInput(s.started_at) + '"></label>' +
      '<label class="field">End<input type="datetime-local" data-act="edit-end" value="' + toLocalInput(s.ended_at) + '"></label></div>' +
      '<label class="field">Note<textarea data-act="note" rows="2">' + esc(s.notes) + "</textarea></label>";
  }

  function finishedCard(s, head) {
    var n = doneSets(s).length;
    return '<div class="card" id="sessionCard">' + head +
      '<p class="sess-sub" data-test="summary">Finished · <b data-test="summary-sets">' + n + (n === 1 ? " set" : " sets") + "</b> · " +
      minutesBetween(s.started_at, s.ended_at) + " min · " + esc(locationName(s.location_id)) + "</p>" +
      editFields(s) +
      '<button type="button" class="big-btn" data-act="new">New workout</button>' +
      '<button type="button" class="big-btn ghost" data-act="resume">Resume this workout</button></div>';
  }

  function exerciseList(s, day, readOnly) {
    var out = [], i = 0, items = day.items;
    while (i < items.length) {
      var g = items[i].superset_group;
      if (g) {
        var group = [];
        while (i < items.length && items[i].superset_group === g) group.push(items[i++]);
        out.push('<div class="superset"><div class="superset-label">Superset ' + esc(g) + " — back to back, then rest</div>" +
          group.map(function (it) { return exerciseCard(s, it, readOnly); }).join("") + "</div>");
      } else {
        out.push(exerciseCard(s, items[i++], readOnly));
      }
    }
    return out.join("");
  }

  function exerciseCard(s, item, readOnly) {
    var ex = exercise(item.exercise_id);
    var last = lastFor(item.exercise_id, s.id);
    var n = item.sets + ((s.extra && s.extra[item.position]) || 0);
    var done = 0, rows = [];
    for (var i = 0; i < n; i++) {
      var sl = s.slots[slotKey(item.position, i)];
      if (sl && sl.done) done++;
      rows.push(setRow(s, item, ex, i, last, readOnly));
    }
    var feel = s.rpe && s.rpe[item.exercise_id];
    var notes = [ex.notes, item.notes].filter(Boolean).join(" · ");
    return '<div class="card ex-card" id="ex-' + item.position + '" data-pos="' + item.position + '">' +
      '<p class="ex-name">' + esc(ex.name_lt) + "</p>" +
      '<div class="ex-en">' + esc(ex.name_en || "") + (ex.weight_mode === "total" ? " · kg = both dumbbells" : "") + "</div>" +
      '<div class="ex-meta"><span class="ex-target">' + esc(targetText(item, ex)) + "</span>" +
      (item.rest_sec ? '<span class="ex-last">rest ' + item.rest_sec + " s</span>" : "") +
      '<span class="ex-done-count">' + done + "/" + item.sets + "</span></div>" +
      '<div class="ex-last" data-test="last">' + esc(lastText(last, ex)) + "</div>" +
      (notes ? '<div class="ex-note">' + esc(notes) + "</div>" : "") +
      '<div class="sets">' + rows.join("") + "</div>" +
      (readOnly ? "" :
        '<div class="feel">Feel' + [1, 2, 3, 4, 5].map(function (k) {
          return '<button type="button" data-act="feel" data-ex="' + esc(item.exercise_id) + '" data-v="' + k + '"' +
            (feel === k ? ' class="on"' : "") + ">" + k + "</button>";
        }).join("") +
        '<span style="flex:1"></span><button type="button" class="btn" data-act="add-set" data-pos="' + item.position + '">+ Set</button></div>') +
      "</div>";
  }

  function slotValues(s, item, ex, idx, last) {
    var sl = s.slots[slotKey(item.position, idx)];
    return sl || Object.assign({ done: false }, prefill(item, ex, idx, last));
  }

  function setRow(s, item, ex, idx, last, readOnly) {
    var f = fieldsFor(ex);
    var v = slotValues(s, item, ex, idx, last);
    var key = slotKey(item.position, idx);
    var changed = v.done && !v.as_planned;
    function stepper(field, label, mode) {
      return '<div class="stepper"><button type="button" data-act="step" data-slot="' + key + '" data-f="' + field + '" data-d="-1"' +
        (readOnly ? " disabled" : "") + ' aria-label="less">−</button>' +
        '<input type="text" inputmode="' + mode + '" class="in-' + field + '" data-act="val" data-slot="' + key + '" data-f="' + field +
        '" value="' + num(v[field]) + '"' + (readOnly ? " disabled" : "") + ' aria-label="' + esc(label) + '">' +
        '<span class="unit">' + esc(label) + "</span>" +
        '<button type="button" data-act="step" data-slot="' + key + '" data-f="' + field + '" data-d="1"' +
        (readOnly ? " disabled" : "") + ' aria-label="more">+</button></div>';
    }
    var fields = "";
    if (f.reps) fields += stepper("reps", ex.per_side ? "reps/leg" : "reps", "numeric");
    if (f.sec) fields += stepper("duration_sec", "s", "numeric");
    if (f.kg === "opt" && !(v.weight_kg > 0) && !v.show_kg) {
      // Added weight on a bodyweight move is the exception: a small chip until asked for.
      fields += '<button type="button" class="kg-chip" data-act="show-kg" data-slot="' + key + '"' + (readOnly ? " disabled" : "") + ">+kg</button>";
    } else if (f.kg) {
      fields += stepper("weight_kg", f.kg === "opt" ? "+kg" : "kg", "decimal");
    }
    return '<div class="set-row' + (v.done ? " done" : "") + (changed ? " changed" : "") + '" data-slot="' + key + '">' +
      '<span class="set-no">' + (idx + 1) + "</span>" +
      '<div class="set-fields">' + fields + "</div>" +
      '<button type="button" class="set-check" data-act="check" data-slot="' + key + '"' + (readOnly ? " disabled" : "") +
      ' aria-pressed="' + (v.done ? "true" : "false") + '" aria-label="Set ' + (idx + 1) + ' done">✓</button></div>';
  }

  function historyCard() {
    var list = SESSIONS.filter(function (s) { return s.ended_at; })
      .sort(function (a, b) { return a.started_at < b.started_at ? 1 : -1; }).slice(0, 10);
    if (!list.length) return "";
    return '<div class="card hist"><h2>On this phone</h2>' + list.map(function (s) {
      var waiting = s.rev > (s.synced_rev || 0);
      var editing = VIEW.editId === s.id;
      return '<div class="hist-row" data-session="' + esc(s.id) + '"><div><b>' + esc(s.day_code) + "</b> · " + fmtDate(s.started_at) + " " +
        fmtTime(s.started_at) + '<div class="hist-meta">' + doneSets(s).length + " sets · " + minutesBetween(s.started_at, s.ended_at) + " min · " +
        esc(locationName(s.location_id)) + "</div></div>" +
        '<span class="tag ' + (waiting ? "wait" : "ok") + '">' + (waiting ? "waiting" : "synced") + "</span>" +
        '<button type="button" class="btn" data-act="edit" data-id="' + esc(s.id) + '">' + (editing ? "Close" : "Edit") + "</button></div>" +
        (editing ? '<div class="hist-edit" data-edit="' + esc(s.id) + '">' + editFields(s) + "</div>" : "");
    }).join("") + "</div>";
  }

  function updateCard(s, pos) {
    var day = dayById(s.plan_day_id);
    var item = day && day.items.find(function (it) { return it.position === pos; });
    var card = document.getElementById("ex-" + pos);
    if (!item || !card) return render();
    var tmp = document.createElement("div");
    tmp.innerHTML = exerciseCard(s, item, false);
    card.replaceWith(tmp.firstChild);
    var c = document.getElementById("setCount");
    if (c) c.textContent = doneSets(s).length;
  }

  // ================================================================ rest timer
  var restAlerted = false;
  function startRest(s, item) {
    if (!item.rest_sec) return;
    s.rest = { ends: Date.now() + item.rest_sec * 1000, total: item.rest_sec, label: "Rest" };
    restAlerted = false;
  }
  function renderRest() {
    var bar = document.getElementById("restBar");
    var s = liveSession();
    if (!s || !s.rest) { bar.hidden = true; return; }
    var left = Math.ceil((s.rest.ends - Date.now()) / 1000);
    bar.hidden = false;
    document.getElementById("restLabel").textContent = left > 0 ? s.rest.label : "Rest over — go!";
    document.getElementById("restTime").textContent = left > 0 ? Math.floor(left / 60) + ":" + String(left % 60).padStart(2, "0") : "0:00";
    document.getElementById("restFill").style.width = Math.min(100, Math.max(0, 100 * (1 - left / s.rest.total))) + "%";
    bar.classList.toggle("over", left <= 0);
    if (left <= 0 && !restAlerted) {
      restAlerted = true;
      if (navigator.vibrate) navigator.vibrate([400, 150, 400]);
    }
    if (left <= -4) {  // banner shown for a few seconds, then gone
      s.rest = null;
      bar.hidden = true;
      idb.put("sessions", s);
    }
  }
  setInterval(function () {
    renderRest();
    var s = liveSession();
    var el = document.getElementById("elapsed");
    if (s && el) el.textContent = elapsedText(s);
  }, 250);
  document.getElementById("restSkip").addEventListener("click", function () {
    var s = liveSession();
    if (s) { s.rest = null; idb.put("sessions", s); renderRest(); }
  });
  document.getElementById("restPlus").addEventListener("click", function () {
    var s = liveSession();
    if (s && s.rest) {
      s.rest.ends = Math.max(s.rest.ends, Date.now()) + 15000;
      s.rest.total += 15;
      restAlerted = false;
      idb.put("sessions", s);
      renderRest();
    }
  });

  // ================================================================ actions
  function start(s) {
    s.started_at = nowIso();
    if (s === draft) draft = null;
    VIEW.finishedId = null;
    idb.put("kv", VIEW.finishedId, "finishedId");
    return save(s);
  }

  function slotFor(s, key) {
    if (s.slots[key]) return s.slots[key];
    var parts = key.split(":");
    var pos = +parts[0], idx = +parts[1];
    var items = dayById(s.plan_day_id).items;
    var item = items.find(function (it) { return it.position === pos; });
    var ex = exercise(item.exercise_id);
    // Set numbers run across the day per exercise (in case it appears twice).
    var before = items.filter(function (it) { return it.position < pos && it.exercise_id === item.exercise_id; })
      .reduce(function (n, it) { return n + it.sets + ((s.extra && s.extra[it.position]) || 0); }, 0);
    var planned = prefill(item, ex, idx, lastFor(item.exercise_id, s.id));
    var sl = Object.assign({
      id: uuid(), exercise_id: item.exercise_id, set_number: before + idx + 1, done: false, planned: planned,
    }, planned);
    s.slots[key] = sl;
    return sl;
  }

  function isAsPlanned(sl) {
    var p = sl.planned || {};
    return ["reps", "weight_kg", "duration_sec"].every(function (k) { return (sl[k] == null ? null : +sl[k]) === (p[k] == null ? null : +p[k]); });
  }

  function itemOf(s, key) {
    var pos = +key.split(":")[0];
    return dayById(s.plan_day_id).items.find(function (it) { return it.position === pos; });
  }

  function toggleCheck(s, key) {
    if (!s.started_at) start(s);
    var sl = slotFor(s, key);
    sl.done = !sl.done;
    if (sl.done) {
      sl.done_at = nowIso();
      sl.as_planned = isAsPlanned(sl);
      startRest(s, itemOf(s, key));
    } else {
      sl.done_at = null;
    }
    save(s);
    if (document.getElementById("finishBtn")) updateCard(s, +key.split(":")[0]);
    else render();  // first tap started the workout: header and finish button appear
    renderRest();
  }

  function setValue(s, key, field, value, rerender) {
    var sl = slotFor(s, key);
    sl[field] = value;
    if (sl.done) sl.as_planned = isAsPlanned(sl);
    if (s.started_at || s === draft) save(s);
    if (rerender) {
      var input = main.querySelector('input[data-slot="' + key + '"][data-f="' + field + '"]');
      if (input) input.value = num(value);
    }
    var row = main.querySelector('.set-row[data-slot="' + key + '"]');
    if (row) row.classList.toggle("changed", !!(sl.done && !sl.as_planned));
  }

  function sessionForEvent(target) {
    var edit = target.closest("[data-edit]");
    if (edit) return byId(edit.dataset.edit);
    return shown();
  }

  main.addEventListener("click", function (e) {
    var t = e.target.closest("[data-act]");
    if (!t || t.disabled) return;
    var s = sessionForEvent(t);
    switch (t.dataset.act) {
      case "start": start(s).then(render); break;
      case "check": toggleCheck(s, t.dataset.slot); break;
      case "step": {
        var sl = s.slots[t.dataset.slot] || slotFor(s, t.dataset.slot);
        var f = t.dataset.f;
        var v = Math.max(0, (parseNum(sl[f]) || 0) + STEP[f] * +t.dataset.d);
        setValue(s, t.dataset.slot, f, Math.round(v * 100) / 100, true);
        break;
      }
      case "feel": {
        var k = +t.dataset.v;
        s.rpe = s.rpe || {};
        s.rpe[t.dataset.ex] = s.rpe[t.dataset.ex] === k ? null : k;
        save(s);
        updateCard(s, +t.closest(".ex-card").dataset.pos);
        break;
      }
      case "show-kg":
        slotFor(s, t.dataset.slot).show_kg = true;
        save(s);
        updateCard(s, +t.closest(".ex-card").dataset.pos);
        break;
      case "add-set": {
        var pos = +t.dataset.pos;
        s.extra = s.extra || {};
        s.extra[pos] = (s.extra[pos] || 0) + 1;
        save(s);
        updateCard(s, pos);
        break;
      }
      case "finish":
        s.ended_at = nowIso();
        s.rest = null;
        VIEW.finishedId = s.id;
        idb.put("kv", s.id, "finishedId");
        save(s).then(function () { render(); scheduleSync(0); });
        break;
      case "resume":
        s.ended_at = null;
        VIEW.finishedId = null;
        idb.put("kv", null, "finishedId");
        save(s).then(render);
        break;
      case "new":
        VIEW.finishedId = null;
        draft = null;
        idb.put("kv", null, "finishedId");
        render();
        window.scrollTo(0, 0);
        break;
      case "add-loc": {
        var input = document.getElementById("newLocName");
        var name = input && input.value.trim();
        if (!name) return;
        var loc = { id: uuid(), name: name, synced: false };
        LOCS.push(loc);
        idb.put("locations", loc);
        VIEW.addingLoc = false;
        var target = VIEW.editId ? byId(VIEW.editId) : shown();
        target.location_id = loc.id;
        save(target).then(render);
        scheduleSync();
        break;
      }
      case "cancel-loc": VIEW.addingLoc = false; render(); break;
      case "edit": VIEW.editId = VIEW.editId === t.dataset.id ? null : t.dataset.id; VIEW.addingLoc = false; render(); break;
    }
  });

  var noteTimer = null;
  main.addEventListener("input", function (e) {
    var t = e.target;
    var s = sessionForEvent(t);
    if (t.dataset.act === "val") {
      setValue(s, t.dataset.slot, t.dataset.f, parseNum(t.value), false);
    } else if (t.dataset.act === "note") {
      s.notes = t.value;
      clearTimeout(noteTimer);
      noteTimer = setTimeout(function () { if (s.started_at || s === draft) save(s); }, 400);
    }
  });

  main.addEventListener("change", function (e) {
    var t = e.target;
    var s = sessionForEvent(t);
    if (t.dataset.act === "location") {
      if (t.value === "__new") {
        VIEW.addingLoc = true;
        if (t.closest("[data-edit]")) VIEW.editId = s.id;
        render();
        var inp = document.getElementById("newLocName");
        if (inp) inp.focus();
        return;
      }
      s.location_id = t.value;
      save(s);
    } else if (t.dataset.act === "edit-start" || t.dataset.act === "edit-end") {
      var iso = fromLocalInput(t.value);
      if (!iso) return;
      s[t.dataset.act === "edit-start" ? "started_at" : "ended_at"] = iso;
      save(s).then(render);
    }
  });

  document.getElementById("dayTabs").addEventListener("click", function (e) {
    var t = e.target.closest("[data-act=day]");
    if (!t) return;
    var live = liveSession();
    // A started workout with nothing ticked yet just moves to the picked day.
    if (live && live.day_code !== t.dataset.day && !doneSets(live).length) {
      var d = dayByCode(t.dataset.day);
      live.plan_day_id = d.id;
      live.day_code = d.code;
      live.location_id = d.default_location_id || live.location_id;
      live.slots = {}; live.extra = {}; live.rpe = {};
      save(live);
    }
    VIEW.day = t.dataset.day;
    VIEW.addingLoc = false;
    render();
    window.scrollTo(0, 0);
  });
  main.addEventListener("click", function (e) {
    var t = e.target.closest("[data-act=day]");
    if (t) document.querySelector('.day-tab[data-day="' + t.dataset.day + '"]').click();
  });

  document.getElementById("syncStatus").addEventListener("click", function () { syncNow(); });

  // ================================================================ start-up
  function flush() {
    clearTimeout(noteTimer);
    var s = liveSession();
    if (s) idb.put("sessions", s);
  }
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "hidden") flush();
    else syncNow();
  });
  window.addEventListener("pagehide", flush);
  window.addEventListener("online", function () { syncNow(); });
  setInterval(function () { syncNow(); }, SYNC_EVERY_MS);

  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/gym/sw.js", { scope: "/gym/" }).catch(function (e) {
      console.warn("service worker not registered", e);
    });
  }

  Promise.all([
    idb.get("kv", "boot"), idb.all("sessions"), idb.all("locations"), idb.get("kv", "finishedId"),
  ]).then(function (r) {
    BOOT = r[0] || null;
    SESSIONS = (r[1] || []).filter(function (s) { return s.started_at; });
    LOCS = r[2] || [];
    // Reopen on a workout finished in the last few hours (its summary and
    // edits); otherwise the app opens on today's plan day.
    var fin = r[3] && byId(r[3]);
    if (fin && fin.ended_at && Date.now() - new Date(fin.ended_at) < REOPEN_FINISHED_MS) {
      VIEW.finishedId = fin.id;
      VIEW.day = fin.day_code;
    }
    render();
    return refreshBoot(true).then(syncNow);
  }).catch(function (e) {
    main.innerHTML = '<p class="gmsg err">This browser storage is not available: ' + esc(e.message) + "</p>";
  });
})();
