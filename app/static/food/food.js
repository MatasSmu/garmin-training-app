/* Food & weight phone page -- offline-first, the same mechanism as the gym
   logger (gym.js). Every change is written to IndexedDB at once and pushed to
   /api/nutrition/sync whenever the server answers: on load, on the `online`
   event, every 30 s and shortly after a change. Logs, foods and saved meals
   get UUIDs here and weights are keyed by date + slot, and the server upserts
   by those keys, so re-sending never duplicates anything. Deletes are sent as
   tombstones. /api/nutrition/bootstrap (targets, foods, saved meals, recent
   logs, weights and the server's trend analysis) is cached for offline use.

   Barcode lookup and the quick-text estimate need the server (Open Food Facts
   and DeepSeek are called from there); everything else works offline. Every
   API call has a short timeout, because with the laptop off the phone is
   usually online but the server simply isn't there. */
(function () {
  "use strict";

  var API_TIMEOUT_MS = 6000;
  var SYNC_TIMEOUT_MS = 12000;
  var ESTIMATE_TIMEOUT_MS = 45000;
  var SYNC_EVERY_MS = 30000;
  var KEEP_LOG_DAYS = 40;
  var WD = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  var MEALS_ORDER = ["breakfast", "lunch", "snack", "dinner"];
  var MEAL_LABEL = { breakfast: "Breakfast", lunch: "Lunch", snack: "Snack", dinner: "Dinner" };

  var main = document.getElementById("main");
  var sheetEl = document.getElementById("sheet");
  var sheetBack = document.getElementById("sheetBack");
  var BOOT = null;                         // cached /api/nutrition/bootstrap
  var LOCAL = { logs: {}, foods: {}, meals: {}, weights: {} };
  var VIEW = { view: "today", date: null, week: null, draft: {}, ci: null };
  var SHEET = null;
  var sync = { busy: false, error: null, timer: null };

  // ================================================================ IndexedDB
  var STORES = { logs: "id", foods: "id", meals: "id", weights: "key" };
  var dbPromise = null;
  function db() {
    if (!dbPromise) {
      dbPromise = new Promise(function (resolve, reject) {
        var req = indexedDB.open("food-tracker", 1);
        req.onupgradeneeded = function () {
          var d = req.result;
          d.createObjectStore("kv");
          Object.keys(STORES).forEach(function (n) { d.createObjectStore(n, { keyPath: STORES[n] }); });
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
  function nowIso() { return new Date().toISOString(); }
  function pad(n) { return String(n).padStart(2, "0"); }
  function dateStr(d) { return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()); }
  function today() { return dateStr(new Date()); }
  function parseDate(s) { var p = s.split("-"); return new Date(+p[0], +p[1] - 1, +p[2]); }
  function addDays(s, n) { var d = parseDate(s); d.setDate(d.getDate() + n); return dateStr(d); }
  function mondayOf(s) { var d = parseDate(s); return addDays(s, -((d.getDay() + 6) % 7)); }
  function fmtDay(s) { var d = parseDate(s); return WD[(d.getDay() + 6) % 7] + " " + d.getDate() + " " + MONTHS[d.getMonth()]; }
  function fmtShort(s) { var d = parseDate(s); return d.getDate() + " " + MONTHS[d.getMonth()]; }
  function nowHHMM() { var d = new Date(); return pad(d.getHours()) + ":" + pad(d.getMinutes()); }
  function parseNum(v) {
    if (v === null || v === undefined) return null;
    var s = String(v).trim().replace(",", ".");
    if (s === "") return null;
    var n = Number(s);
    return isNaN(n) ? NaN : n;
  }
  function r0(v) { return v == null ? "—" : String(Math.round(v)); }
  function r1(v) { return v == null ? "—" : String(Math.round(v * 10) / 10); }
  function kcalFmt(v) { return Math.round(v || 0).toLocaleString("en-US"); }
  function round1(v) { return v == null ? null : Math.round(v * 10) / 10; }
  function signed(v, nd) { return (v > 0 ? "+" : v < 0 ? "−" : "±") + Math.abs(v).toFixed(nd); }

  function defaultMeal(hhmm) {
    var p = hhmm.split(":"), t = +p[0] * 60 + +p[1];
    if (t < 630) return "breakfast";
    if (t < 900) return "lunch";
    if (t < 1050) return "snack";
    if (t < 1290) return "dinner";
    return "snack";
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
        return r.json().catch(function () { return {}; }).then(function (b) {
          var err = new Error(r.status === 401 ? "sign-in needed" : (b && b.detail && typeof b.detail === "string" ? b.detail : "HTTP " + r.status));
          err.status = r.status;
          throw err;
        });
      }
      return r.json();
    }).catch(function (e) {
      if (e.name === "AbortError") { var t = new Error("server not reachable"); t.status = 0; throw t; }
      if (e.status === undefined) e.status = 0;
      throw e;
    }).finally(function () { clearTimeout(timer); });
  }

  // ================================================================ data
  function targets() { return (BOOT && BOOT.targets) || {}; }

  function foodsById() {
    var out = {};
    ((BOOT && BOOT.foods) || []).forEach(function (f) { out[f.id] = f; });
    Object.keys(LOCAL.foods).forEach(function (id) {
      var f = LOCAL.foods[id];
      if (f.deleted) delete out[id];
      else out[id] = Object.assign({}, out[id] || {}, f);
    });
    return out;
  }

  function scale(food, grams) {
    function v(x) { return x == null ? null : Math.round(x * grams / 10) / 10; }
    return { kcal: v(food.kcal_100g) || 0, protein_g: v(food.protein_100g) || 0, carbs_g: v(food.carbs_100g), fat_g: v(food.fat_100g) };
  }
  function sum(items) {
    var t = { kcal: 0, protein_g: 0, carbs_g: 0, fat_g: 0 };
    items.forEach(function (x) { Object.keys(t).forEach(function (k) { t[k] += +(x[k] || 0); }); });
    Object.keys(t).forEach(function (k) { t[k] = Math.round(t[k] * 10) / 10; });
    return t;
  }
  /* Same rule as calc.meal_count on the server: breakfast / lunch / dinner
     count once each, snacks once per group logged within an hour. */
  function mealCount(logs) {
    var main = {}, times = [], untimed = false;
    logs.forEach(function (l) {
      if (l.meal === "snack") {
        var m = /^(\d{1,2}):(\d{2})/.exec(l.time || "");
        if (m) times.push(+m[1] * 60 + +m[2]); else untimed = true;
      } else if (l.meal) main[l.meal] = true;
    });
    times.sort(function (a, b) { return a - b; });
    var snacks = times.filter(function (t, i) { return i === 0 || t - times[i - 1] >= 60; }).length;
    if (!times.length && untimed) snacks = 1;
    return { meals: Object.keys(main).length, snacks: snacks };
  }
  function mealsText(c) {
    if (!c.meals && !c.snacks) return "—";
    return c.meals + (c.meals === 1 ? " meal" : " meals") + (c.snacks ? " + " + c.snacks + (c.snacks === 1 ? " snack" : " snacks") : "");
  }
  /* Share of energy from protein / carbs / fat (4 / 4 / 9 kcal per g). */
  function split(t) {
    var p = t.protein_g * 4, c = t.carbs_g * 4, f = t.fat_g * 9, all = p + c + f;
    if (!all) return null;
    return { p: Math.round(p / all * 100), c: Math.round(c / all * 100), f: Math.round(f / all * 100) };
  }
  function macroCard(tot, logs) {
    var s = split(tot), mc = mealCount(logs);
    return '<div class="macro-row" data-test="macros">' +
      '<span class="mc-p"><b>' + r0(tot.protein_g) + " g</b>Protein" + (s ? " · " + s.p + "%" : "") + "</span>" +
      '<span class="mc-c"><b>' + r0(tot.carbs_g) + " g</b>Carbs" + (s ? " · " + s.c + "%" : "") + "</span>" +
      '<span class="mc-f"><b>' + r0(tot.fat_g) + " g</b>Fat" + (s ? " · " + s.f + "%" : "") + "</span>" +
      '<span class="mc-m"><b data-test="meal-count">' + (mc.meals + mc.snacks) + "</b>" + esc(mealsText(mc)) + "</span></div>" +
      (s ? '<div class="split" aria-label="Energy split: protein ' + s.p + "%, carbs " + s.c + "%, fat " + s.f + '%">' +
        '<i class="sp-p" style="width:' + s.p + '%"></i><i class="sp-c" style="width:' + s.c + '%"></i><i class="sp-f" style="width:' + s.f + '%"></i></div>' : "");
  }
  /* A warning when kcal and the macros clearly disagree (protein and carbs 4,
     fat 9 kcal per g), e.g. 54 kcal with 54 g fat. */
  function macroGap(x) {
    if (x.carbs_g == null && x.fat_g == null) return null;
    var fromMacros = (x.protein_g || 0) * 4 + (x.carbs_g || 0) * 4 + (x.fat_g || 0) * 9, kcal = +x.kcal || 0;
    return Math.abs(fromMacros - kcal) > Math.max(80, 0.3 * Math.max(kcal, fromMacros)) ? Math.round(fromMacros) : null;
  }
  function macroCheck(x) {
    var g = macroGap(x);
    return g === null ? "" : '<p class="warn-note" data-test="macro-gap">These don\'t add up: the protein, carbs and fat make about ' + g +
      " kcal, not " + r0(x.kcal) + ". Check the numbers.</p>";
  }
  function pcf(t) { return r0(t.protein_g) + " P · " + r0(t.carbs_g) + " C · " + r0(t.fat_g) + " F"; }

  function logsList() {
    return Object.keys(LOCAL.logs).map(function (k) { return LOCAL.logs[k]; });
  }
  function dayLogs(day) {
    return logsList().filter(function (l) { return !l.deleted && l.date === day; })
      .sort(function (a, b) { return (a.time || "") < (b.time || "") ? -1 : (a.time || "") > (b.time || "") ? 1 : 0; });
  }
  function byRecent(a, b) {
    var ka = a.date + " " + (a.time || "") + " " + a.updated_at, kb = b.date + " " + (b.time || "") + " " + b.updated_at;
    return ka < kb ? 1 : ka > kb ? -1 : 0;
  }

  function mealUses() {
    var since = addDays(today(), -30), uses = {};
    logsList().forEach(function (l) {
      if (!l.deleted && l.saved_meal_id && l.date >= since) uses[l.saved_meal_id] = (uses[l.saved_meal_id] || 0) + 1;
    });
    return uses;
  }
  function savedMeals() {
    var byId = {};
    ((BOOT && BOOT.saved_meals) || []).forEach(function (m) { byId[m.id] = m; });
    Object.keys(LOCAL.meals).forEach(function (id) {
      var m = LOCAL.meals[id];
      if (m.deleted) delete byId[id];
      else byId[id] = Object.assign({}, m, { totals: sum(m.items) });
    });
    var uses = mealUses();
    return Object.keys(byId).map(function (id) { return byId[id]; }).sort(function (a, b) {
      return (uses[b.id] || 0) - (uses[a.id] || 0) || a.name.localeCompare(b.name);
    });
  }
  function mealById(id) { return savedMeals().find(function (m) { return m.id === id; }) || null; }

  function lastGramsFor(food) {
    var mine = logsList().filter(function (l) { return !l.deleted && l.food_id === food.id && l.grams; }).sort(byRecent)[0];
    return (mine && mine.grams) || food.last_grams || food.serving_g || 100;
  }

  /* Recent distinct things logged (not saved meals -- those have their own buttons). */
  function recents() {
    var since = addDays(today(), -30), seen = {}, out = [];
    var foods = foodsById();
    logsList().filter(function (l) { return !l.deleted && l.source !== "saved_meal" && l.date >= since; })
      .sort(byRecent).forEach(function (l) {
        var key = l.food_id && foods[l.food_id] ? "f:" + l.food_id
          : "t:" + (l.description || "").toLowerCase() + "|" + l.kcal + "|" + l.protein_g;
        if (key in seen) {   // prefer a copy that still knows its weight (old re-adds dropped it)
          if (!out[seen[key]].grams && l.grams) out[seen[key]] = l;
          return;
        }
        if (out.length >= 10) return;
        seen[key] = out.length;
        out.push(l);
      });
    return out;
  }

  function weightsMap() {
    var out = {};
    ((BOOT && BOOT.weights) || []).forEach(function (w) { out[w.date + "|" + w.slot] = w.weight_kg; });
    Object.keys(LOCAL.weights).forEach(function (k) {
      var w = LOCAL.weights[k];
      if (w.weight_kg == null) delete out[k];
      else out[k] = w.weight_kg;
    });
    return out;
  }

  // ================================================================ saving
  function isPending(x) { return (x.rev || 0) > (x.synced_rev || 0); }
  function pendingCount() {
    return ["logs", "foods", "meals", "weights"].reduce(function (n, s) {
      return n + Object.keys(LOCAL[s]).filter(function (k) { return isPending(LOCAL[s][k]); }).length;
    }, 0);
  }

  function put(storeName, rec) {
    rec.rev = (rec.rev || 0) + 1;
    rec.updated_at = nowIso();
    LOCAL[storeName][rec[STORES[storeName]]] = rec;
    var p = idb.put(storeName, rec).catch(function (e) { toast("Couldn't save on the phone: " + e.message); });
    scheduleSync();
    renderSync();
    return p;
  }

  function newLog(fields) {
    var time = VIEW.date === today() ? nowHHMM() : "12:00";
    return Object.assign({
      id: uuid(), date: VIEW.date, time: time, meal: defaultMeal(time),
      kcal: 0, protein_g: 0, carbs_g: null, fat_g: null,
      food_id: null, grams: null, saved_meal_id: null, description: null, source: "manual", deleted: false,
    }, fields);
  }
  function addLog(fields, msg) {
    var l = newLog(fields);
    put("logs", l);
    render();
    toast(msg || ("Added " + (l.description || "entry")), [
      { label: "Edit", fn: function () { openSheet({ type: "edit", id: l.id }); } },
      { label: "Undo", fn: function () { l.deleted = true; put("logs", l); render(); } },
    ]);
    return l;
  }
  function logFood(food, grams) {
    return addLog(Object.assign(scale(food, grams), {
      food_id: food.id, grams: grams, description: food.name, source: "food",
    }), "Added " + food.name + " " + r0(grams) + " g");
  }
  function logSavedMeal(m) {
    var t = m.totals || sum(m.items || []);
    return addLog({ kcal: t.kcal, protein_g: t.protein_g, carbs_g: t.carbs_g, fat_g: t.fat_g,
                    saved_meal_id: m.id, description: m.name, source: "saved_meal" }, "Added " + m.name);
  }
  /* A recent item per 100 g: from the food's label, or worked out from the
     entry's own numbers and weight. null = it was logged without a weight,
     so the pick sheet works in portions instead. */
  function per100Of(l) {
    var food = l.food_id && foodsById()[l.food_id];
    if (food) {
      return { food: food, grams: lastGramsFor(food),
        per100: { kcal_100g: food.kcal_100g, protein_100g: food.protein_100g, carbs_100g: food.carbs_100g, fat_100g: food.fat_100g } };
    }
    if (l.grams > 0) {
      var f = function (x) { return x == null ? null : x / l.grams * 100; };
      return { food: null, grams: l.grams,
        per100: { kcal_100g: f(l.kcal), protein_100g: f(l.protein_g), carbs_100g: f(l.carbs_g), fat_100g: f(l.fat_g) } };
    }
    return null;
  }
  function pickValues(s) {
    if (s.base) return scale(s.base.per100, s.grams);
    var k = s.portions, v = function (x) { return x == null ? null : Math.round(x * k * 10) / 10; };
    return { kcal: v(s.log.kcal) || 0, protein_g: v(s.log.protein_g) || 0, carbs_g: v(s.log.carbs_g), fat_g: v(s.log.fat_g) };
  }
  function macroGrid(kcal, p, c, f) {
    var cell = function (v, label, nd) { return "<span><b>" + (v == null ? "—" : nd ? r1(v) : r0(v)) + "</b>" + label + "</span>"; };
    return '<div class="macro-grid">' + cell(kcal, "kcal") + cell(p, "protein g", 1) + cell(c, "carbs g", 1) + cell(f, "fat g", 1) + "</div>";
  }
  function pickCalcHtml(s) {
    var m = pickValues(s);
    return (s.base ? "For " + r0(s.grams) + " g" : "For " + r1(s.portions) + (s.portions === 1 ? " portion" : " portions")) + ": <b>" +
      r0(m.kcal) + " kcal</b> · " + r1(m.protein_g) + " g protein · " + (m.carbs_g == null ? "—" : r1(m.carbs_g)) + " g carbs · " +
      (m.fat_g == null ? "—" : r1(m.fat_g)) + " g fat";
  }
  function recentSub(l) {
    var b = per100Of(l);
    if (b) {
      var p = b.per100;
      return "per 100 g: " + r0(p.kcal_100g) + " kcal · " + r0(p.protein_100g) + " P · " + (p.carbs_100g == null ? "—" : r0(p.carbs_100g)) +
        " C · " + (p.fat_100g == null ? "—" : r0(p.fat_100g)) + " F · last " + r0(b.grams) + " g";
    }
    return "1 portion: " + r0(l.kcal) + " kcal · " + pcf(l);
  }

  function relog(l) {
    var food = l.food_id && foodsById()[l.food_id];
    if (food) return logFood(food, lastGramsFor(food));
    return addLog({ kcal: l.kcal, protein_g: l.protein_g, carbs_g: l.carbs_g, fat_g: l.fat_g, grams: l.grams || null,
                    description: l.description, source: l.source === "estimate" ? "estimate" : "manual" },
                  "Added " + (l.description || "entry") + (l.grams ? " " + r0(l.grams) + " g" : ""));
  }

  // ================================================================ sync
  function scheduleSync(ms) {
    clearTimeout(sync.timer);
    sync.timer = setTimeout(syncNow, ms === undefined ? 2000 : ms);
  }

  function strip(x) {
    var o = Object.assign({}, x);
    delete o.rev; delete o.synced_rev; delete o.key; delete o.totals; delete o.last_grams; delete o.last_used; delete o.uses_30d;
    return o;
  }

  function syncNow() {
    if (sync.busy) return Promise.resolve();
    var sent = [];
    var body = { foods: [], saved_meals: [], logs: [], weights: [] };
    var map = { foods: "foods", meals: "saved_meals", logs: "logs", weights: "weights" };
    Object.keys(map).forEach(function (s) {
      Object.keys(LOCAL[s]).forEach(function (k) {
        var x = LOCAL[s][k];
        if (!isPending(x)) return;
        sent.push({ s: s, x: x, rev: x.rev });
        body[map[s]].push(strip(x));
      });
    });
    if (!sent.length) return refreshBoot();
    sync.busy = true;
    renderSync();
    return api("/api/nutrition/sync", { method: "POST", body: body }, SYNC_TIMEOUT_MS).then(function () {
      sync.error = null;
      return Promise.all(sent.map(function (e) {
        if (e.x.rev !== e.rev) return null;  // changed again while sending
        e.x.synced_rev = e.rev;
        return idb.put(e.s, e.x);
      }));
    }).then(function () {
      sync.busy = false;
      return refreshBoot(true);
    }).catch(function (e) {
      sync.error = e;
    }).finally(function () {
      sync.busy = false;
      renderSync();
      if (pendingCount() && !sync.error) scheduleSync(500);
    });
  }

  /* Take the server's copy: logs the phone hasn't changed since its last sync
     are replaced (picks up edits and deletes made elsewhere), synced foods,
     meals and weights are dropped locally because the bootstrap now has them. */
  function mergeBoot() {
    var writes = [];
    ((BOOT && BOOT.logs) || []).forEach(function (s) {
      var l = LOCAL.logs[s.id];
      if (l && isPending(l)) return;
      if (l && l.updated_at > s.updated_at) return;
      var copy = Object.assign({}, s, { rev: 1, synced_rev: 1 });
      LOCAL.logs[s.id] = copy;
      writes.push(idb.put("logs", copy));
    });
    var oldest = addDays(today(), -KEEP_LOG_DAYS);
    Object.keys(LOCAL.logs).forEach(function (id) {
      var l = LOCAL.logs[id];
      if (!isPending(l) && l.date < oldest) { delete LOCAL.logs[id]; writes.push(idb.del("logs", id)); }
    });
    ["foods", "meals", "weights"].forEach(function (s) {
      Object.keys(LOCAL[s]).forEach(function (k) {
        if (isPending(LOCAL[s][k])) return;
        delete LOCAL[s][k];
        writes.push(idb.del(s, k));
      });
    });
    return Promise.all(writes);
  }

  var lastBootAt = 0;
  function refreshBoot(force) {
    if (!force && BOOT && Date.now() - lastBootAt < 60000) return Promise.resolve();
    return api("/api/nutrition/bootstrap").then(function (fresh) {
      BOOT = fresh;
      lastBootAt = Date.now();
      sync.error = null;
      return Promise.all([idb.put("kv", fresh, "boot"), mergeBoot()]);
    }).then(function () {
      if (!isTyping()) render();
      renderSync();
    }).catch(function (e) {
      sync.error = e;
      renderSync();
    });
  }

  function renderSync() {
    var el = document.getElementById("syncStatus");
    var n = pendingCount();
    el.className = "sync-pill";
    if (sync.busy) {
      el.textContent = "Syncing…";
      el.classList.add("busy");
    } else if (n) {
      el.textContent = n + (n === 1 ? " change" : " changes") + " waiting to sync" + (sync.error && sync.error.status === 401 ? " · sign in" : "");
      el.classList.add("wait");
    } else if (!BOOT) {
      el.textContent = sync.error ? "Offline" : "…";
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
    return a && main.contains(a) && (a.tagName === "INPUT" || a.tagName === "TEXTAREA" || a.tagName === "SELECT");
  }

  function render() {
    document.querySelectorAll(".view-tab").forEach(function (b) { b.classList.toggle("active", b.dataset.view === VIEW.view); });
    document.getElementById("title").textContent = VIEW.view === "today" ? "Food" : VIEW.view === "weight" ? "Weight" : "This week";
    if (!BOOT && !logsList().length && VIEW.view !== "today") {
      main.innerHTML = '<p class="fmsg">Open this page once while the server is reachable to load your data.</p>';
      return;
    }
    main.innerHTML = VIEW.view === "today" ? todayView() : VIEW.view === "weight" ? weightView() : weekView();
  }

  // ---------------------------------------------------------------- today
  /* Progress toward a target (min) or a band (min-max); without targets
     (nothing loaded yet) just the number. */
  function bar(label, value, unit, min, max, test) {
    var top = (max || min || value || 1) * 1.15;
    var pct = Math.min(100, (value / top) * 100);
    var cls = min && value >= min ? (max && value > max * 1.1 ? "over" : "hit") : "";
    var band = !min ? "" : '<div class="bar-band" style="left:' + (min / top * 100) + "%;width:" + (max ? (max - min) / top * 100 : 0) + '%"></div>';
    var fmt = unit === "kcal" ? kcalFmt : r0;
    var goal = !min ? "" : " / " + (max ? kcalFmt(min) + "–" + kcalFmt(max) : r0(min));
    return '<div class="bar-row"><div class="bar-label"><span>' + label + '</span><span><b class="mono" data-test="' + test + '">' +
      fmt(value) + "</b>" + goal + " " + unit +
      '</span></div><div class="bar">' + band + '<div class="bar-fill ' + cls + '" style="width:' + pct + '%"></div></div></div>';
  }

  function todayView() {
    var t = targets(), day = VIEW.date, logs = dayLogs(day), tot = sum(logs);
    var isToday = day === today();
    var h = [];
    h.push('<div class="day-nav"><button type="button" class="nav-btn" data-act="day" data-d="-1" aria-label="Previous day">‹</button><b>' +
      (isToday ? "Today, " : "") + fmtDay(day) + '</b><button type="button" class="nav-btn" data-act="day" data-d="1" aria-label="Next day"' +
      (isToday ? " disabled" : "") + ">›</button></div>");
    h.push('<div class="card">' + bar("Protein", tot.protein_g, "g", t.protein_g, null, "protein-total") +
      bar("Calories", tot.kcal, "kcal", t.kcal_min, t.kcal_max, "kcal-total") +
      (t.carbs_g ? bar("Carbs", tot.carbs_g, "g", t.carbs_g, null, "carbs-total") : "") +
      (t.fat_g ? bar("Fat", tot.fat_g, "g", t.fat_g, null, "fat-total") : "") + macroCard(tot, logs) + "</div>");

    var meals = savedMeals();
    h.push('<div class="card"><div class="card-head"><h2>Saved meals</h2>' +
      (meals.length ? '<button type="button" class="link-btn" data-act="meals">Manage</button>' : "") + "</div>");
    if (meals.length) {
      h.push('<div class="chips">' + meals.map(function (m) {
        var tt = m.totals || sum(m.items || []);
        return '<button type="button" class="chip" data-act="log-meal" data-id="' + esc(m.id) + '"><b>' + esc(m.name) +
          "</b><small>" + r0(tt.kcal) + " kcal · " + r0(tt.protein_g) + " g protein</small></button>";
      }).join("") + "</div>");
    } else {
      h.push('<p class="muted">Log a meal, then tap “Save as meal” under it — next time it\'s one tap.</p>');
    }
    h.push("</div>");

    h.push('<div class="add-row"><button type="button" class="add-btn" data-act="sheet" data-sheet="text">Type it</button>' +
      '<button type="button" class="add-btn" data-act="sheet" data-sheet="scan">Scan</button>' +
      '<button type="button" class="add-btn alt" data-act="sheet" data-sheet="manual">Manual</button></div>');

    var rec = recents();
    if (rec.length) {
      h.push('<div class="card" style="margin-top:12px"><h2>Recent</h2><div class="list">' + rec.map(function (l, i) {
        // Tapping the item opens it (per 100 g + how much you ate); + adds it again as last time.
        return '<div class="item rec-item" data-test="recent"><button type="button" class="rec-open" data-act="pick" data-i="' + i + '">' +
          '<span class="item-main"><span class="item-name">' + esc(l.description || "Entry") +
          (l.source === "estimate" ? '<span class="tag">est</span>' : "") + '</span><span class="item-sub">' + esc(recentSub(l)) +
          '</span></span></button><button type="button" class="plus" data-act="relog" data-i="' + i + '" aria-label="Add ' +
          esc(l.description || "entry") + ' again">+</button></div>';
      }).join("") + "</div></div>");
    }

    h.push('<div class="card" style="margin-top:12px"><h2>' + (isToday ? "Today" : fmtDay(day)) + " · " + logs.length + (logs.length === 1 ? " entry" : " entries") + "</h2>");
    if (!logs.length) h.push('<p class="muted">Nothing logged yet.</p>');
    MEALS_ORDER.forEach(function (meal) {
      var group = logs.filter(function (l) { return l.meal === meal; });
      if (!group.length) return;
      var gt = sum(group);
      h.push('<div class="meal-head"><span>' + MEAL_LABEL[meal] + " · " + r0(gt.kcal) + " kcal<small>" + pcf(gt) + " g</small></span>" +
        '<button type="button" class="link-btn" data-act="save-meal" data-meal="' + meal + '">Save as meal</button></div><div class="list">' +
        group.map(function (l) {
          return '<button type="button" class="item" data-act="edit" data-id="' + esc(l.id) + '"><span class="item-main"><span class="item-name">' +
            esc(l.description || "Entry") + (l.source === "estimate" ? '<span class="tag">est</span>' : "") +
            (isPending(l) ? '<span class="tag wait">waiting</span>' : "") +
            (macroGap(l) !== null ? '<span class="tag check" title="kcal and macros don\'t add up — tap to fix">check</span>' : "") +
            '</span><span class="item-sub">' + esc(l.time || "") +
            (l.grams ? " · " + r0(l.grams) + " g" : "") + '</span></span><span class="item-num mono"><b>' + r0(l.kcal) + "</b>" + pcf(l) + "</span></button>";
        }).join("") + "</div>");
    });
    h.push("</div>");
    return h.join("");
  }

  // ---------------------------------------------------------------- weight
  function weightView() {
    var w = (BOOT && BOOT.weight) || {};
    var h = [];
    var pendingW = Object.keys(LOCAL.weights).some(function (k) { return isPending(LOCAL.weights[k]); });
    h.push('<div class="card"><div class="stats">' +
      '<div class="stat"><b class="mono" data-test="trend">' + (w.trend_kg != null ? r1(w.trend_kg) + " kg" : "—") + "</b><span>Trend weight" +
      (w.trend_date ? " · " + fmtShort(w.trend_date) : "") + "</span></div>" +
      '<div class="stat"><b class="mono" data-test="rate">' + (w.rate_kg_week != null ? signed(w.rate_kg_week, 2) + " kg/wk" : "—") +
      "</b><span>Weekly rate · target " + (w.target_rate_kg_week != null ? signed(w.target_rate_kg_week, 1) : "—") + "</span></div>" +
      '<div class="stat"><b><span class="status ' + esc(w.status || "") + '" data-test="status">' + esc(w.status_label || "Not enough data") +
      "</span></b><span>vs the plan</span></div>" +
      '<div class="stat"><b class="mono">' + (w.goal_weight_kg != null ? w.goal_weight_kg + " kg" : "—") + "</b><span>" +
      (w.status === "reached" ? "Goal reached" : w.projected_date ? "Projected " + fmtShort(w.projected_date) : "Goal") +
      (w.goal_date ? " · plan " + fmtShort(w.goal_date) : "") + "</span></div></div>" +
      (w.evening_diff_kg != null ? '<p class="muted">Evening is ' + signed(w.evening_diff_kg, 1) + " kg vs morning on average (info only; the trend uses mornings).</p>" : "") +
      (pendingW ? '<p class="muted">New weights are saved on the phone; the trend updates after they sync.</p>' : "") + "</div>");
    var chart = window.WeightChart && w.days && w.days.length ? WeightChart.svg(w, { today: today(), width: 400, height: 230 }) : "";
    h.push(checkinCard());
    if (chart) h.push('<div class="card">' + chart + "</div>");
    h.push(gridCard());
    return h.join("");
  }

  /* One weigh-in with how you feel: weight, score and note (the same fields
     the Google Sheet import fills). Sent with the weights; a grid save only
     sends the weight, so it never clears a score or note. */
  function weightRec(key) {
    var out = null;
    ((BOOT && BOOT.weights) || []).forEach(function (w) { if (w.date + "|" + w.slot === key) out = w; });
    if (LOCAL.weights[key]) out = Object.assign({}, out || {}, LOCAL.weights[key]);
    return out && out.weight_kg != null ? out : null;
  }
  function nowHHMM() {
    var d = new Date();
    return ("0" + d.getHours()).slice(-2) + ":" + ("0" + d.getMinutes()).slice(-2);
  }
  function ciLoad(d, slot) {
    var r = weightRec(d + "|" + slot) || {};
    return { date: d, slot: slot, w: r.weight_kg != null ? String(r.weight_kg) : "", score: r.score != null ? String(r.score) : "",
      note: r.note || "", time: r.time || null };
  }
  function checkinCard() {
    if (!VIEW.ci) VIEW.ci = ciLoad(today(), new Date().getHours() < 14 ? "morning" : "evening");
    var c = VIEW.ci, saved = weightRec(c.date + "|" + c.slot);
    return '<div class="card" data-test="checkin"><div class="card-head"><h2>Check-in</h2>' +
      '<input type="date" class="ci-date" data-ci="date" max="' + today() + '" value="' + esc(c.date) + '" aria-label="Check-in date"></div>' +
      '<div class="seg">' + ["morning", "evening"].map(function (sl) {
        return '<button type="button" class="seg-btn' + (c.slot === sl ? " active" : "") + '" data-act="ci-slot" data-slot="' + sl + '">' +
          (sl === "morning" ? "Morning" : "Evening") + "</button>";
      }).join("") + "</div>" +
      '<div class="ci-row"><label>Weight kg<input type="text" inputmode="decimal" autocomplete="off" data-ci="w" value="' + esc(c.w) + '"></label>' +
      '<label>Score<input type="text" inputmode="decimal" autocomplete="off" data-ci="score" value="' + esc(c.score) + '"></label></div>' +
      '<label class="ci-note">Note<textarea data-ci="note" rows="2" maxlength="1000">' + esc(c.note) + "</textarea></label>" +
      '<p class="muted" id="ciMsg">' + (saved ? "Saved" + (saved.time ? " at " + esc(saved.time) : "") + " — saving again updates it."
        : "Score and note are optional.") + "</p>" +
      '<button type="button" class="big-btn" data-act="ci-save">Save check-in</button></div>';
  }
  function saveCheckin() {
    var c = VIEW.ci, msg = document.getElementById("ciMsg");
    var w = parseNum(c.w), sc = parseNum(c.score);
    if (w === null || isNaN(w) || w < 25 || w > 250) { msg.textContent = "Type a weight between 25 and 250 kg."; msg.className = "err"; return; }
    if (sc !== null && (isNaN(sc) || Math.abs(sc) > 1000)) { msg.textContent = "The score must be a number."; msg.className = "err"; return; }
    var key = c.date + "|" + c.slot;
    var rec = LOCAL.weights[key] || { key: key, date: c.date, slot: c.slot };
    rec.weight_kg = Math.round(w * 100) / 100;
    rec.score = sc;
    rec.note = c.note.trim() || null;
    rec.time = c.time || (c.date === today() ? nowHHMM() : null);
    put("weights", rec);
    VIEW.ci = ciLoad(c.date, c.slot);
    render();
    toast("Check-in saved");
    scheduleSync(0);
  }

  function gridCard() {
    var wk = VIEW.week, vals = weightsMap(), t = today();
    var h = ['<div class="card"><div class="grid-nav"><button type="button" class="nav-btn" data-act="week" data-d="-7" aria-label="Previous week">‹</button><b>' +
      fmtShort(wk) + " – " + fmtShort(addDays(wk, 6)) + '</b><button type="button" class="nav-btn" data-act="week" data-d="7" aria-label="Next week"' +
      (addDays(wk, 7) > t ? " disabled" : "") + ">›</button></div>"];
    h.push('<table class="wgrid"><thead><tr><th>Day</th><th>Morning</th><th>Evening</th></tr></thead><tbody>');
    for (var i = 0; i < 7; i++) {
      var d = addDays(wk, i), future = d > t;
      h.push("<tr><td>" + WD[i] + "<small>" + fmtShort(d) + "</small></td>");
      ["morning", "evening"].forEach(function (slot) {
        var key = d + "|" + slot;
        var v = key in VIEW.draft ? VIEW.draft[key] : (vals[key] != null ? String(vals[key]) : "");
        var changed = key in VIEW.draft && parseNum(VIEW.draft[key]) !== (vals[key] != null ? vals[key] : null);
        h.push('<td><input type="text" inputmode="decimal" enterkeyhint="next" autocomplete="off" class="w-in' + (changed ? " changed" : "") +
          '" data-key="' + key + '" aria-label="' + WD[i] + " " + slot + '" value="' + esc(v) + '"' + (future ? " disabled" : "") + "></td>");
      });
      h.push("</tr>");
    }
    h.push('</tbody></table><p class="muted" id="gridMsg">Type from your paper notes; Next jumps to the following cell. Empty is fine.</p>' +
      '<button type="button" class="big-btn" id="saveWeek" data-act="save-week">Save week</button></div>');
    return h.join("");
  }

  function saveWeek() {
    var vals = weightsMap(), changes = [], bad = false;
    main.querySelectorAll(".w-in").forEach(function (inp) {
      inp.classList.remove("bad");
      if (inp.disabled) return;
      var n = parseNum(inp.value);
      if (n !== null && (isNaN(n) || n < 25 || n > 250)) { inp.classList.add("bad"); bad = true; return; }
      var key = inp.dataset.key, cur = vals[key] != null ? vals[key] : null;
      if (n !== null) n = Math.round(n * 100) / 100;
      if (n !== cur) changes.push({ key: key, n: n });
    });
    var msg = document.getElementById("gridMsg");
    if (bad) { msg.textContent = "Check the red cells (kg between 25 and 250)."; msg.className = "err"; return; }
    VIEW.draft = {};
    if (!changes.length) { msg.textContent = "Nothing changed."; msg.className = "muted"; return; }
    changes.forEach(function (c) {
      var p = c.key.split("|");
      var rec = LOCAL.weights[c.key] || { key: c.key, date: p[0], slot: p[1] };
      rec.weight_kg = c.n;
      put("weights", rec);
    });
    render();
    toast("Saved " + changes.length + (changes.length === 1 ? " weight" : " weights"));
    scheduleSync(0);
  }

  // ---------------------------------------------------------------- week
  function weekView() {
    var t = targets(), h = [];
    var days = [];
    var hit = function (d) { return t.protein_g && d.protein_g >= t.protein_g; };
    for (var i = 6; i >= 0; i--) {
      var d = addDays(today(), -i), dl = dayLogs(d), tot = sum(dl), mc = mealCount(dl);
      days.push({ date: d, kcal: tot.kcal, protein_g: tot.protein_g, carbs_g: tot.carbs_g, fat_g: tot.fat_g,
        times: mc.meals + mc.snacks, mc: mc, n: dl.length });
    }
    var logged = days.filter(function (d) { return d.n; });
    var avg = function (k) { return logged.length ? logged.reduce(function (s, d) { return s + d[k]; }, 0) / logged.length : null; };
    h.push('<div class="card"><h2>Last 7 days</h2><div class="wk-nums">' +
      "<span><b>" + (avg("kcal") != null ? kcalFmt(avg("kcal")) : "—") + "</b>avg kcal</span>" +
      "<span><b>" + r0(avg("protein_g")) + "</b>avg protein g</span>" +
      "<span><b>" + r0(avg("carbs_g")) + "</b>avg carbs g</span>" +
      "<span><b>" + r0(avg("fat_g")) + "</b>avg fat g</span>" +
      "<span><b>" + (avg("times") != null ? r1(avg("times")) : "—") + "</b>eating times / day</span>" +
      "<span><b>" + days.filter(hit).length + "/7</b>protein days</span>" +
      "<span><b>" + logged.length + "/7</b>days logged</span></div>" +
      '<div class="list" style="margin-top:8px">' + days.slice().reverse().map(function (d) {
        return '<div class="item"><span class="item-main"><span class="item-name">' + fmtDay(d.date) + '</span><span class="item-sub">' +
          (d.n ? esc(mealsText(d.mc)) : "") + '</span></span><span class="item-num mono"><b>' +
          (d.n ? kcalFmt(d.kcal) + " kcal" : "—") + "</b>" + (d.n ? pcf(d) + (hit(d) ? " ✓" : "") : "") + "</span></div>";
      }).join("") + "</div></div>");

    var e = BOOT && BOOT.expenditure;
    if (e) {
      var body;
      if (e.basis === "intake_and_weight") {
        body = "<p>Estimated expenditure <b class=\"mono\">~" + kcalFmt(e.expenditure_kcal) + " kcal/day</b> (estimate from " + e.days_with_both +
          " days of food + weight).</p><p>To gain " + signed(t.target_rate_kg_week || 0, 1) + " kg/week eat about <b class=\"mono\">" +
          kcalFmt(e.suggested_intake_kcal) + " kcal/day</b> (" + (e.suggested_change_kcal >= 0 ? "+" : "−") + kcalFmt(Math.abs(e.suggested_change_kcal)) + " vs now).</p>";
      } else {
        body = '<p class="muted">Expenditure estimate needs ' + e.days_needed + " days with both a morning weight and a full food log (≥ " +
          kcalFmt(e.food_day_min_kcal) + " kcal) in the last " + e.window_days + " days — you have " + e.days_with_both + ".</p>";
      }
      if (e.advice) body += '<p data-test="advice"><b>' + esc(e.advice.charAt(0).toUpperCase() + e.advice.slice(1)) + ".</b></p>";
      h.push('<div class="card"><h2>Energy</h2>' + body + "</div>");
    }

    var weeks = (BOOT && BOOT.weeks) || [];
    h.push('<div class="card"><h2>Weeks</h2>' + (weeks.length ? "" : '<p class="muted">No data yet.</p>') + weeks.map(function (w) {
      return '<div class="wk"><div class="wk-head"><b>' + fmtShort(w.week_start) + " – " + fmtShort(w.week_end) + '</b><span class="status ' + esc(w.status) + '">' +
        esc(w.status_label) + '</span></div><div class="wk-nums">' +
        "<span><b>" + (w.avg_kcal != null ? kcalFmt(w.avg_kcal) : "—") + "</b>avg kcal</span>" +
        "<span><b>" + r0(w.avg_protein_g) + "</b>avg protein</span>" +
        "<span><b>" + r0(w.avg_carbs_g) + "</b>avg carbs</span>" +
        "<span><b>" + r0(w.avg_fat_g) + "</b>avg fat</span>" +
        "<span><b>" + (w.avg_eating_times != null ? w.avg_eating_times : "—") + "</b>eating times / day</span>" +
        "<span><b>" + w.protein_days + "/" + w.food_days + "</b>protein days</span>" +
        "<span><b>" + (w.trend_change_kg != null ? signed(w.trend_change_kg, 2) : "—") + "</b>trend kg</span></div></div>";
    }).join("") + "</div>");
    return h.join("");
  }

  // ================================================================ sheets
  function openSheet(s) {
    stopScanner();
    SHEET = s;
    renderSheet();
    sheetEl.hidden = false;
    sheetBack.hidden = false;
    if (s.type === "scan") startScanner();
    var f = sheetEl.querySelector("[autofocus]");
    if (f) f.focus();
  }
  function closeSheet() {
    stopScanner();
    SHEET = null;
    sheetEl.hidden = true;
    sheetBack.hidden = true;
    sheetEl.innerHTML = "";
  }
  function head(title) {
    return '<div class="sheet-head"><h2>' + esc(title) + '</h2><button type="button" class="x-btn" data-act="close" aria-label="Close">×</button></div>';
  }
  function mealSelect(sel) {
    return '<label class="field">Meal<select name="meal">' + MEALS_ORDER.map(function (m) {
      return '<option value="' + m + '"' + (m === sel ? " selected" : "") + ">" + MEAL_LABEL[m] + "</option>";
    }).join("") + "</select></label>";
  }
  function val(name) { var el = sheetEl.querySelector('[name="' + name + '"]'); return el ? el.value : ""; }

  function renderSheet() {
    var s = SHEET, h = [];
    if (!s) return;
    if (s.type === "text") {
      h.push(head("Type what you ate"));
      if (!s.items) {
        h.push('<label class="field">e.g. “2 burgers and 500 ml milk”<textarea name="text" autofocus>' + esc(s.text || "") + "</textarea></label>");
        if (s.error) h.push('<p class="err">' + esc(s.error) + "</p>");
        h.push('<button type="button" class="big-btn" data-act="estimate"' + (s.busy ? " disabled" : "") + ">" + (s.busy ? "Estimating…" : "Estimate") + "</button>");
      } else {
        h.push('<p class="muted">AI estimate — check and fix the numbers, then add. Nothing is saved until you tap Add.</p>');
        h.push('<div class="est-head"><span>kcal</span><span>Protein g</span><span>Carbs g</span><span>Fat g</span><span></span></div>');
        s.items.forEach(function (it, i) {
          h.push('<div class="est-row" data-row="' + i + '"><input class="est-name" name="n' + i + '" value="' + esc(it.name) + '" aria-label="Item">' +
            '<label class="est-grams"><input name="g' + i + '" inputmode="decimal" autocomplete="off" value="' + esc(it.grams ? r0(it.grams) : "") +
            '" placeholder="?" aria-label="grams"><span>g</span></label>' +
            '<input name="k' + i + '" inputmode="decimal" value="' + esc(r0(it.kcal)) + '" aria-label="kcal">' +
            '<input name="p' + i + '" inputmode="decimal" value="' + esc(r1(it.protein_g)) + '" aria-label="protein g">' +
            '<input name="c' + i + '" inputmode="decimal" value="' + esc(it.carbs_g != null ? r1(it.carbs_g) : "") + '" aria-label="carbs g">' +
            '<input name="f' + i + '" inputmode="decimal" value="' + esc(it.fat_g != null ? r1(it.fat_g) : "") + '" aria-label="fat g">' +
            '<button type="button" class="x-btn" data-act="est-del" data-i="' + i + '" aria-label="Remove">×</button>' +
            macroCheck(it) + "</div>");
        });
        if (s.note) h.push('<p class="muted">' + esc(s.note) + "</p>");
        h.push(mealSelect(defaultMeal(nowHHMM())));
        h.push('<button type="button" class="big-btn" data-act="est-save">Add ' + s.items.length + (s.items.length === 1 ? " item" : " items") + "</button>");
        h.push('<button type="button" class="big-btn ghost" data-act="est-back">Back</button>');
      }
    } else if (s.type === "manual") {
      h.push(head("Manual entry"));
      h.push('<label class="field">What<input name="name" autocomplete="off" autofocus></label>' +
        '<div class="row"><label class="field">kcal<input name="kcal" inputmode="decimal"></label>' +
        '<label class="field">Protein g<input name="protein" inputmode="decimal"></label></div>' +
        '<div class="row"><label class="field">Carbs g (optional)<input name="carbs" inputmode="decimal"></label>' +
        '<label class="field">Fat g (optional)<input name="fat" inputmode="decimal"></label></div>' + mealSelect(defaultMeal(nowHHMM())));
      if (s.error) h.push('<p class="err">' + esc(s.error) + "</p>");
      h.push('<button type="button" class="big-btn" data-act="manual-save">Add</button>');
    } else if (s.type === "scan") {
      h.push(head("Scan a barcode"));
      if (!s.food && !s.notFound) {
        h.push('<video class="scan-video" id="scanVideo" playsinline muted' + (s.camera ? "" : " hidden") + "></video>");
        h.push('<p class="muted" id="scanMsg">' + esc(s.cameraMsg || "") + "</p>");
        h.push('<div class="row"><label class="field">Barcode<input name="code" inputmode="numeric" autocomplete="off" value="' + esc(s.code || "") + '"></label></div>');
        if (s.error) h.push('<p class="err">' + esc(s.error) + "</p>");
        h.push('<button type="button" class="big-btn" data-act="lookup"' + (s.busy ? " disabled" : "") + ">" + (s.busy ? "Looking up…" : "Look up") + "</button>");
      } else if (s.food) {
        var f = s.food, g = s.grams, m = scale(f, g);
        h.push('<p><b>' + esc(f.name) + "</b>" + (f.brand ? " · " + esc(f.brand) : "") + '</p><p class="muted">Per 100 g: ' + r0(f.kcal_100g) +
          " kcal · " + r1(f.protein_100g) + " g protein" + (f.source === "openfoodfacts" ? " (Open Food Facts)" : "") + "</p>");
        h.push('<label class="field">Grams<input name="grams" inputmode="decimal" value="' + esc(r0(g)) + '" data-act="grams"></label>');
        h.push('<p class="mono" id="scanCalc">' + r0(m.kcal) + " kcal · " + r1(m.protein_g) + " g protein</p>");
        h.push('<button type="button" class="big-btn" data-act="scan-add">Add</button>');
      } else {
        h.push('<p>' + (s.offline ? "Couldn't reach the server to look it up." : "Not in Open Food Facts.") +
          " Enter the label once (per 100 g) and it's remembered for this barcode.</p>");
        h.push('<label class="field">Name<input name="name" autocomplete="off" autofocus></label>' +
          '<div class="row"><label class="field">kcal / 100 g<input name="kcal" inputmode="decimal"></label>' +
          '<label class="field">Protein / 100 g<input name="protein" inputmode="decimal"></label></div>' +
          '<div class="row"><label class="field">Carbs / 100 g<input name="carbs" inputmode="decimal"></label>' +
          '<label class="field">Fat / 100 g<input name="fat" inputmode="decimal"></label></div>' +
          '<label class="field">Grams eaten<input name="grams" inputmode="decimal" value="100"></label>');
        if (s.error) h.push('<p class="err">' + esc(s.error) + "</p>");
        h.push('<button type="button" class="big-btn" data-act="label-save">Save food & add</button>');
      }
    } else if (s.type === "pick") {
      var pl = s.log, pb = s.base;
      h.push(head(pl.description || "Entry"));
      if (pb) {
        var p100 = pb.per100;
        h.push('<p class="muted pick-label">Per 100 g' + (pb.food ? "" : " (from what you logged before)") + "</p>" +
          macroGrid(p100.kcal_100g, p100.protein_100g, p100.carbs_100g, p100.fat_100g));
        h.push('<label class="field">How much did you eat? (g)<input name="amount" inputmode="decimal" autocomplete="off" value="' +
          esc(r0(s.grams)) + '" data-act="pick-amount" autofocus></label>');
      } else {
        h.push('<p class="muted pick-label">One portion (it was logged without a weight)</p>' +
          macroGrid(pl.kcal, pl.protein_g, pl.carbs_g, pl.fat_g));
        h.push('<label class="field">Portions<input name="amount" inputmode="decimal" autocomplete="off" value="' +
          esc(r1(s.portions)) + '" data-act="pick-amount" autofocus></label>');
      }
      h.push('<p class="pick-total" id="pickCalc" data-test="pick-total">' + pickCalcHtml(s) + "</p>");
      h.push(mealSelect(defaultMeal(VIEW.date === today() ? nowHHMM() : "12:00")));
      if (s.error) h.push('<p class="err">' + esc(s.error) + "</p>");
      h.push('<button type="button" class="big-btn" data-act="pick-add">Add</button>');
    } else if (s.type === "edit") {
      var l = LOCAL.logs[s.id];
      if (!l) { closeSheet(); return; }
      if (s.base === undefined) s.base = per100Of(l);     // from the entry as it was opened
      h.push(head(l.description || "Entry"));
      h.push('<label class="field">What<input name="name" value="' + esc(l.description || "") + '"></label>');
      // How much of it: changing the grams recalculates kcal / protein / carbs / fat (per 100 g below).
      h.push('<label class="field">How much (g)<input name="grams" inputmode="decimal" autocomplete="off" data-act="edit-grams" value="' +
        esc(l.grams ? r0(l.grams) : "") + '" placeholder="weight in grams"></label>');
      if (s.base) {
        var b100 = s.base.per100;
        h.push('<p class="muted">Per 100 g: ' + r0(b100.kcal_100g) + " kcal · " + r1(b100.protein_100g) + " P · " +
          (b100.carbs_100g == null ? "—" : r1(b100.carbs_100g)) + " C · " + (b100.fat_100g == null ? "—" : r1(b100.fat_100g)) +
          " F — change the grams and the numbers below follow, or type them yourself.</p>");
      } else {
        h.push('<p class="muted">Logged without a weight: type the grams to keep them (from then on it works per 100 g).</p>');
      }
      h.push('<div class="row"><label class="field">kcal<input name="kcal" inputmode="decimal" value="' + esc(r0(l.kcal)) + '"></label>' +
        '<label class="field">Protein g<input name="protein" inputmode="decimal" value="' + esc(r1(l.protein_g)) + '"></label></div>' +
        '<div class="row"><label class="field">Carbs g<input name="carbs" inputmode="decimal" value="' + esc(l.carbs_g != null ? r1(l.carbs_g) : "") + '"></label>' +
        '<label class="field">Fat g<input name="fat" inputmode="decimal" value="' + esc(l.fat_g != null ? r1(l.fat_g) : "") + '"></label></div>' +
        macroCheck(l));
      h.push('<div class="row">' + mealSelect(l.meal) + '<label class="field">Time<input name="time" type="time" value="' + esc(l.time || "") + '"></label></div>');
      if (s.error) h.push('<p class="err">' + esc(s.error) + "</p>");
      h.push('<button type="button" class="big-btn" data-act="edit-save">Save</button>' +
        '<button type="button" class="big-btn danger" data-act="edit-del">Delete entry</button>');
    } else if (s.type === "save-meal") {
      var logs = dayLogs(VIEW.date);
      h.push(head("Save as meal"));
      h.push('<label class="field">Name<input name="mname" autocomplete="off" value="' + esc(s.name) + '" autofocus></label>');
      h.push('<p class="muted">Pick what goes in it:</p>' + logs.map(function (x) {
        return '<label class="check"><input type="checkbox" name="pick" value="' + esc(x.id) + '"' + (x.meal === s.meal ? " checked" : "") + "> " +
          esc(x.description || "Entry") + (x.grams ? " · " + r0(x.grams) + " g" : "") + " · " + r0(x.kcal) + " kcal</label>";
      }).join(""));
      if (s.error) h.push('<p class="err">' + esc(s.error) + "</p>");
      h.push('<button type="button" class="big-btn" data-act="meal-create">Save meal</button>');
    } else if (s.type === "meals") {
      h.push(head("Saved meals"));
      h.push('<div class="list">' + savedMeals().map(function (m) {
        var tt = m.totals || sum(m.items || []);
        return '<div class="item"><span class="item-main"><span class="item-name">' + esc(m.name) + '</span><span class="item-sub">' +
          (m.items || []).map(function (it) { return esc(it.name || it.description || "item") + (it.grams ? " " + r0(it.grams) + " g" : ""); }).join(", ") +
          " · " + r0(tt.kcal) + " kcal · " + r0(tt.protein_g) + ' g P</span></span><button type="button" class="x-btn" data-act="meal-del" data-id="' +
          esc(m.id) + '" aria-label="Delete">🗑</button></div>';
      }).join("") + "</div>");
    }
    sheetEl.innerHTML = h.join("");
  }

  // ---------------------------------------------------------------- barcode camera
  var scanner = { stream: null, timer: null };
  function startScanner() {
    var s = SHEET;
    if (!("BarcodeDetector" in window) || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      s.cameraMsg = window.isSecureContext ? "This browser can't scan barcodes — type the number." :
        "Camera scanning needs HTTPS (Tailscale serve) — type the number for now.";
      renderSheet();
      return;
    }
    var detector;
    BarcodeDetector.getSupportedFormats().then(function (formats) {
      var want = ["ean_13", "ean_8", "upc_a", "upc_e", "code_128"].filter(function (f) { return formats.indexOf(f) !== -1; });
      detector = new BarcodeDetector(want.length ? { formats: want } : undefined);
      return navigator.mediaDevices.getUserMedia({ video: { facingMode: { ideal: "environment" } }, audio: false });
    }).then(function (stream) {
      if (SHEET !== s) { stream.getTracks().forEach(function (t) { t.stop(); }); return; }
      scanner.stream = stream;
      s.camera = true;
      s.cameraMsg = "Point the camera at the barcode.";
      renderSheet();
      var video = document.getElementById("scanVideo");
      video.srcObject = stream;
      return video.play().then(function () {
        (function tick() {
          scanner.timer = setTimeout(function () {
            if (SHEET !== s || !scanner.stream) return;
            detector.detect(video).then(function (codes) {
              if (codes.length && SHEET === s) {
                stopScanner();
                s.code = codes[0].rawValue;
                lookup(s.code);
              } else tick();
            }).catch(tick);
          }, 200);
        })();
      });
    }).catch(function (e) {
      if (SHEET !== s) return;
      s.cameraMsg = "Camera not available (" + e.message + ") — type the number.";
      renderSheet();
    });
  }
  function stopScanner() {
    clearTimeout(scanner.timer);
    if (scanner.stream) scanner.stream.getTracks().forEach(function (t) { t.stop(); });
    scanner.stream = null;
  }

  function lookup(code) {
    var s = SHEET;
    code = String(code || "").replace(/\D/g, "");
    if (code.length < 6) { s.error = "That doesn't look like a barcode."; renderSheet(); return; }
    s.code = code; s.error = null;
    var known = Object.values(foodsById()).find(function (f) { return f.barcode === code; });
    if (known) { showFood(s, known); return; }
    s.busy = true;
    renderSheet();
    api("/api/nutrition/barcode/" + code, {}, 15000).then(function (r) {
      if (SHEET !== s) return;
      s.busy = false;
      if (r.found) {
        BOOT && BOOT.foods && BOOT.foods.push(r.food);
        showFood(s, r.food);
      } else { s.notFound = true; renderSheet(); }
    }).catch(function (e) {
      if (SHEET !== s) return;
      s.busy = false;
      if (e.status === 0 || e.status === 503) { s.notFound = true; s.offline = true; } else s.error = e.message;
      renderSheet();
    });
  }
  function showFood(s, food) {
    s.food = food;
    s.grams = lastGramsFor(food);
    renderSheet();
  }

  // ---------------------------------------------------------------- sheet actions
  function num(name) { return parseNum(val(name)); }
  function ok(n, lo, hi) { return n !== null && !isNaN(n) && n >= lo && n <= hi; }
  function opt(n) { return n === null || isNaN(n) ? null : n; }

  function sheetAction(act, t) {
    var s = SHEET;
    if (act === "close") return closeSheet();
    if (act === "estimate") {
      s.text = val("text").trim();
      if (s.text.length < 2) { s.error = "Type what you ate."; return renderSheet(); }
      s.busy = true; s.error = null;
      renderSheet();
      api("/api/nutrition/estimate", { method: "POST", body: { text: s.text } }, ESTIMATE_TIMEOUT_MS).then(function (r) {
        if (SHEET !== s) return;
        s.busy = false; s.items = r.items; s.note = r.note;
        s.items.forEach(function (it) {     // per 100 g, so changing an item's grams rescales it
          var gr = +it.grams, f = function (x) { return x == null ? null : x / gr * 100; };
          it.base = gr > 0 ? { kcal_100g: f(it.kcal), protein_100g: f(it.protein_g), carbs_100g: f(it.carbs_g), fat_100g: f(it.fat_g) } : null;
        });
        renderSheet();
      }).catch(function (e) {
        if (SHEET !== s) return;
        s.busy = false;
        s.error = e.status === 0 ? "Needs the server (it asks DeepSeek) — try again when connected, or use Manual." : "Couldn't estimate: " + e.message;
        renderSheet();
      });
      return;
    }
    if (act === "est-del") {
      readEstimate(s);
      s.items.splice(+t.dataset.i, 1);
      if (!s.items.length) s.items = null;
      return renderSheet();
    }
    if (act === "est-back") { s.items = null; return renderSheet(); }
    if (act === "est-save") {
      readEstimate(s);
      var meal = val("meal");
      var bad = s.items.some(function (it) {
        return !ok(it.kcal, 0, 20000) || !ok(it.protein_g, 0, 1000) || !it.name ||
          (it.carbs_g !== null && !ok(it.carbs_g, 0, 3000)) || (it.fat_g !== null && !ok(it.fat_g, 0, 2000)) ||
          (it.grams !== null && !ok(it.grams, 1, 5000));
      });
      if (bad) { s.error = "Check the numbers."; return renderSheet(); }
      var n = s.items.length;
      s.items.forEach(function (it) {
        put("logs", newLog({ kcal: it.kcal, protein_g: it.protein_g, carbs_g: it.carbs_g, fat_g: it.fat_g, grams: it.grams,
                             description: it.name, source: "estimate", meal: meal }));
      });
      closeSheet(); render();
      return toast("Added " + n + (n === 1 ? " estimated item" : " estimated items"));
    }
    if (act === "manual-save") {
      var name = val("name").trim(), kcal = num("kcal"), prot = num("protein");
      if (!name) { s.error = "What was it?"; return renderSheet(); }
      if (!ok(kcal, 0, 20000)) { s.error = "kcal is needed."; return renderSheet(); }
      var l = { kcal: kcal, protein_g: opt(prot) || 0, carbs_g: opt(num("carbs")), fat_g: opt(num("fat")), description: name, source: "manual", meal: val("meal") };
      closeSheet();
      return addLog(l, "Added " + name);
    }
    if (act === "lookup") return lookup(val("code"));
    if (act === "scan-add") {
      var g = num("grams");
      if (!ok(g, 1, 5000)) return;
      var food = s.food;
      closeSheet();
      return logFood(food, g);
    }
    if (act === "label-save") {
      var fname = val("name").trim(), k100 = num("kcal"), p100 = num("protein"), grams = num("grams");
      if (!fname || !ok(k100, 0, 1000) || !ok(grams, 1, 5000)) { s.error = "Name, kcal and grams are needed."; return renderSheet(); }
      var f = { id: uuid(), name: fname, kcal_100g: k100, protein_100g: opt(p100) || 0, carbs_100g: opt(num("carbs")),
                fat_100g: opt(num("fat")), barcode: s.code || null, source: "manual", deleted: false };
      put("foods", f);
      closeSheet();
      return logFood(f, grams);
    }
    if (act === "pick-add") {
      var amount = num("amount");
      if (s.base ? !ok(amount, 1, 5000) : !ok(amount, 0.1, 20)) {
        s.error = s.base ? "How many grams? (1–5000)" : "How many portions? (0.1–20)"; return renderSheet();
      }
      if (s.base) s.grams = amount; else s.portions = amount;
      var pv = pickValues(s), src = s.log;
      var fields = Object.assign(pv, {
        meal: val("meal"), description: src.description,
        grams: s.base ? amount : null, food_id: s.base && s.base.food ? s.base.food.id : null,
        source: s.base && s.base.food ? "food" : src.source === "estimate" ? "estimate" : "manual",
      });
      fields.protein_g = fields.protein_g || 0;
      closeSheet();
      return addLog(fields, "Added " + (src.description || "entry") + (s.base ? " " + r0(amount) + " g" : amount !== 1 ? " × " + r1(amount) : ""));
    }
    if (act === "edit-save") {
      var e = LOCAL.logs[s.id];
      var kk = num("kcal"), pp = num("protein"), cc = num("carbs"), ff = num("fat"), gg = num("grams");
      if (!ok(kk, 0, 20000)) { s.error = "kcal is needed."; return renderSheet(); }
      if ((pp !== null && !ok(pp, 0, 1000)) || (cc !== null && !ok(cc, 0, 3000)) || (ff !== null && !ok(ff, 0, 2000))) {
        s.error = "Check protein, carbs and fat (grams)."; return renderSheet();
      }
      if (gg !== null && !ok(gg, 1, 5000)) { s.error = "Weight: 1–5000 g (or leave it empty)."; return renderSheet(); }
      // The boxes already hold the recalculated numbers when the grams were changed.
      e.kcal = kk; e.protein_g = opt(pp) || 0; e.carbs_g = opt(cc); e.fat_g = opt(ff); e.grams = gg;
      e.description = val("name").trim() || e.description;
      e.meal = val("meal");
      if (val("time")) e.time = val("time");
      put("logs", e);
      closeSheet();
      return render();
    }
    if (act === "edit-del") {
      var d = LOCAL.logs[s.id];
      d.deleted = true;
      put("logs", d);
      closeSheet(); render();
      return toast("Deleted " + (d.description || "entry"), [{ label: "Undo", fn: function () { d.deleted = false; put("logs", d); render(); } }]);
    }
    if (act === "meal-create") {
      var mname = val("mname").trim();
      var picked = Array.prototype.map.call(sheetEl.querySelectorAll('[name="pick"]:checked'), function (x) { return LOCAL.logs[x.value]; });
      if (!mname) { s.error = "Give it a name."; return renderSheet(); }
      if (!picked.length) { s.error = "Pick at least one entry."; return renderSheet(); }
      var items = [];
      picked.forEach(function (x) {
        var sm = x.saved_meal_id && mealById(x.saved_meal_id);
        if (sm) { (sm.items || []).forEach(function (it) { items.push(Object.assign({}, it)); }); return; }
        items.push({ food_id: x.food_id || null, grams: x.food_id ? x.grams : null, name: x.description, description: x.description,
                     kcal: x.kcal, protein_g: x.protein_g, carbs_g: x.carbs_g, fat_g: x.fat_g });
      });
      put("meals", { id: uuid(), name: mname, items: items, deleted: false });
      closeSheet(); render();
      return toast("Saved “" + mname + "” — one tap from now on");
    }
    if (act === "meal-del") {
      var m = mealById(t.dataset.id);
      if (!m) return;
      put("meals", Object.assign({}, LOCAL.meals[m.id] || strip(m), { deleted: true, items: m.items || [] }));
      render();
      return renderSheet();
    }
  }

  function readEstimate(s) {
    s.items.forEach(function (it, i) {
      it.name = val("n" + i).trim();
      it.grams = opt(parseNum(val("g" + i)));
      it.kcal = parseNum(val("k" + i));
      it.protein_g = parseNum(val("p" + i));
      it.carbs_g = opt(parseNum(val("c" + i)));
      it.fat_g = opt(parseNum(val("f" + i)));
    });
  }

  // ================================================================ toast
  var toastTimer = null;
  function toast(msg, actions) {
    var el = document.getElementById("toast");
    el.innerHTML = "<span>" + esc(msg) + "</span><span>" + (actions || []).map(function (a, i) {
      return '<button type="button" data-i="' + i + '">' + esc(a.label) + "</button>";
    }).join("") + "</span>";
    el.hidden = false;
    el.onclick = function (e) {
      var b = e.target.closest("button");
      if (!b) return;
      el.hidden = true;
      actions[+b.dataset.i].fn();
    };
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { el.hidden = true; }, 5000);
  }

  // ================================================================ events
  main.addEventListener("click", function (e) {
    var t = e.target.closest("[data-act]");
    if (!t) return;
    var act = t.dataset.act;
    if (act === "day") {
      var nd = addDays(VIEW.date, +t.dataset.d);
      if (nd <= today()) { VIEW.date = nd; render(); }
    } else if (act === "week") {
      collectDraft();
      VIEW.week = addDays(VIEW.week, +t.dataset.d);
      render();
    } else if (act === "log-meal") {
      var m = mealById(t.dataset.id);
      if (m) logSavedMeal(m);
    } else if (act === "relog") {
      var r = recents()[+t.dataset.i];
      if (r) relog(r);
    } else if (act === "pick") {
      var pr = recents()[+t.dataset.i];
      if (pr) {
        var base = per100Of(pr);
        openSheet({ type: "pick", log: pr, base: base, grams: base ? base.grams : null, portions: 1 });
      }
    } else if (act === "sheet") {
      openSheet({ type: t.dataset.sheet });
    } else if (act === "edit") {
      openSheet({ type: "edit", id: t.dataset.id });
    } else if (act === "save-meal") {
      openSheet({ type: "save-meal", meal: t.dataset.meal, name: MEAL_LABEL[t.dataset.meal] + " " + fmtShort(VIEW.date) });
    } else if (act === "meals") {
      openSheet({ type: "meals" });
    } else if (act === "save-week") {
      saveWeek();
    } else if (act === "ci-slot") {
      VIEW.ci = ciLoad(VIEW.ci.date, t.dataset.slot);
      render();
    } else if (act === "ci-save") {
      saveCheckin();
    }
  });

  function collectDraft() {
    var vals = weightsMap();
    main.querySelectorAll(".w-in").forEach(function (inp) {
      var cur = vals[inp.dataset.key] != null ? String(vals[inp.dataset.key]) : "";
      if (inp.value !== cur) VIEW.draft[inp.dataset.key] = inp.value;
      else delete VIEW.draft[inp.dataset.key];
    });
  }
  main.addEventListener("change", function (e) {
    if (e.target.dataset.ci === "date" && e.target.value && e.target.value <= today()) {
      VIEW.ci = ciLoad(e.target.value, VIEW.ci.slot);
      render();
    }
  });
  main.addEventListener("input", function (e) {
    var f = e.target.dataset && e.target.dataset.ci;
    if (f && f !== "date" && VIEW.ci) VIEW.ci[f] = e.target.value;
    if (e.target.classList.contains("w-in")) {
      var vals = weightsMap(), key = e.target.dataset.key, cur = vals[key] != null ? String(vals[key]) : "";
      if (e.target.value !== cur) VIEW.draft[key] = e.target.value; else delete VIEW.draft[key];
      e.target.classList.toggle("changed", e.target.value !== cur);
    }
  });
  main.addEventListener("keydown", function (e) {
    if (e.key !== "Enter" || !e.target.classList.contains("w-in")) return;
    e.preventDefault();
    var all = Array.prototype.filter.call(main.querySelectorAll(".w-in"), function (x) { return !x.disabled; });
    var i = all.indexOf(e.target);
    if (i < all.length - 1) { all[i + 1].focus(); all[i + 1].select(); } else document.getElementById("saveWeek").focus();
  });

  sheetEl.addEventListener("click", function (e) {
    var t = e.target.closest("[data-act]");
    if (t && t.tagName !== "INPUT") sheetAction(t.dataset.act, t);
  });
  function setBox(name, v, nd) {
    var el = sheetEl.querySelector('[name="' + name + '"]');
    if (el) el.value = v == null ? "" : nd ? r1(v) : r0(v);
  }
  sheetEl.addEventListener("input", function (e) {
    // Edit sheet: new grams -> kcal / protein / carbs / fat follow (per 100 g of the entry).
    if (e.target.dataset.act === "edit-grams" && SHEET && SHEET.type === "edit" && SHEET.base) {
      var g = parseNum(e.target.value);
      if (g && !isNaN(g) && g > 0) {
        var mv = scale(SHEET.base.per100, g);
        setBox("kcal", mv.kcal); setBox("protein", mv.protein_g, 1); setBox("carbs", mv.carbs_g, 1); setBox("fat", mv.fat_g, 1);
      }
      return;
    }
    // "Type it" preview: an item's grams -> its numbers follow.
    var em = /^g(\d+)$/.exec(e.target.name || "");
    if (em && SHEET && SHEET.type === "text" && SHEET.items) {
      var it = SHEET.items[+em[1]], gi = parseNum(e.target.value);
      if (it && it.base && gi && !isNaN(gi) && gi > 0) {
        var sv = scale(it.base, gi), i = em[1];
        setBox("k" + i, sv.kcal); setBox("p" + i, sv.protein_g, 1); setBox("c" + i, sv.carbs_g, 1); setBox("f" + i, sv.fat_g, 1);
      }
      return;
    }
    if (e.target.dataset.act === "pick-amount" && SHEET && SHEET.type === "pick") {
      var a = parseNum(e.target.value);
      if (a && !isNaN(a) && a > 0) {
        if (SHEET.base) SHEET.grams = a; else SHEET.portions = a;
        document.getElementById("pickCalc").innerHTML = pickCalcHtml(SHEET);
      }
      return;
    }
    if (e.target.dataset.act === "grams" && SHEET && SHEET.food) {
      var g = parseNum(e.target.value);
      if (g && !isNaN(g)) {
        SHEET.grams = g;
        var m = scale(SHEET.food, g);
        document.getElementById("scanCalc").textContent = r0(m.kcal) + " kcal · " + r1(m.protein_g) + " g protein";
      }
    }
  });
  sheetEl.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && e.target.name === "code") { e.preventDefault(); lookup(e.target.value); }
    if (e.key === "Enter" && e.target.name === "amount" && SHEET && SHEET.type === "pick") { e.preventDefault(); sheetAction("pick-add", e.target); }
  });
  sheetBack.addEventListener("click", closeSheet);

  document.getElementById("views").addEventListener("click", function (e) {
    var t = e.target.closest("[data-view]");
    if (!t) return;
    if (VIEW.view === "weight") collectDraft();
    VIEW.view = t.dataset.view;
    try { localStorage.setItem("food-view", VIEW.view); } catch (err) { /* private mode */ }
    render();
    window.scrollTo(0, 0);
  });

  document.getElementById("syncStatus").addEventListener("click", function () {
    if (sync.error && sync.error.status === 401) location.href = "/food-login";
    else syncNow();
  });

  // ================================================================ start-up
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") {
      if (VIEW.openedOn !== today() && !SHEET) {  // opened again the next day
        VIEW.openedOn = VIEW.date = today();
        VIEW.week = mondayOf(today());
        render();
      }
      syncNow();
    }
  });
  window.addEventListener("online", function () { syncNow(); });
  setInterval(function () { syncNow(); }, SYNC_EVERY_MS);

  /* New versions of this page: the service worker installs them in the
     background; check for one whenever the page comes back to the screen and
     reload into it (or offer a Reload button while something is being typed),
     so a page left open for hours doesn't keep running old code. */
  if ("serviceWorker" in navigator) {
    var hadController = !!navigator.serviceWorker.controller, reloading = false;
    navigator.serviceWorker.register("/food/sw.js", { scope: "/food/" }).then(function (reg) {
      document.addEventListener("visibilitychange", function () {
        if (document.visibilityState === "visible") reg.update().catch(function () { /* offline */ });
      });
    }).catch(function (e) {
      console.warn("service worker not registered", e);
    });
    navigator.serviceWorker.addEventListener("controllerchange", function () {
      if (!hadController) { hadController = true; return; }   // first install, not an update
      if (reloading) return;
      var reload = function () { reloading = true; window.location.reload(); };
      if (SHEET || isTyping() || Object.keys(VIEW.draft || {}).length) {
        toast("A new version of this page is ready.", [{ label: "Reload", fn: reload }]);
      } else {
        reload();
      }
    });
  }

  VIEW.openedOn = VIEW.date = today();
  VIEW.week = mondayOf(today());
  try { VIEW.view = localStorage.getItem("food-view") || "today"; } catch (err) { /* private mode */ }

  Promise.all([idb.get("kv", "boot"), idb.all("logs"), idb.all("foods"), idb.all("meals"), idb.all("weights")]).then(function (r) {
    BOOT = r[0] || null;
    r[1].forEach(function (x) { LOCAL.logs[x.id] = x; });
    r[2].forEach(function (x) { LOCAL.foods[x.id] = x; });
    r[3].forEach(function (x) { LOCAL.meals[x.id] = x; });
    r[4].forEach(function (x) { LOCAL.weights[x.key] = x; });
    render();
    renderSync();
    return syncNow();
  }).catch(function (e) {
    main.innerHTML = '<p class="fmsg err">This browser storage is not available: ' + esc(e.message) + "</p>";
  });
})();
