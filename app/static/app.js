(function () {
  "use strict";

  // ---- tab nav ----
  document.querySelectorAll(".tab-btn").forEach(function (btn) {
    btn.addEventListener("click", function () {
      document.querySelectorAll(".tab-btn").forEach(function (b) { b.classList.remove("active"); });
      document.querySelectorAll(".tab-section").forEach(function (s) { s.classList.remove("active"); });
      btn.classList.add("active");
      document.getElementById("tab-" + btn.dataset.tab).classList.add("active");
    });
  });

  function fmtMin(min) {
    if (min === undefined || min === null) return "—";
    return Math.round(min) + " min";
  }

  function escapeHtml(s) {
    return String(s || "").replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  // ---- dashboard ----
  function renderJournalList(el, entries) {
    if (!entries.length) {
      el.innerHTML = '<p class="empty-hint">No entries yet.</p>';
      return;
    }
    el.innerHTML = entries
      .map(function (e) {
        return (
          '<div class="journal-entry"><div class="je-date mono">' +
          escapeHtml(e.date) +
          '</div><div class="je-text">' +
          escapeHtml(e.text) +
          "</div></div>"
        );
      })
      .join("");
  }

  function loadDashboard() {
    fetch("/api/dashboard/summary")
      .then(function (r) { return r.json(); })
      .then(function (d) {
        document.getElementById("statLoad7").textContent = d.load_7d.session_count;
        document.getElementById("statMinutes7").textContent = fmtMin(d.load_7d.total_duration_min);
        var streak = d.current_streak;
        document.getElementById("statStreak").textContent = streak.days + "d " + streak.type;
        var sleep = d.sleep_trend.avg_sleep_h;
        document.getElementById("statSleep").textContent = sleep != null ? sleep.toFixed(1) + "h" : "—";
        renderJournalList(document.getElementById("dashboardJournal"), d.recent_journal);
      });
  }

  // ---- sessions ----
  var ALL_SESSIONS = [];

  // Sailing-type sessions can be opened in the Session map tab's GPS analysis.
  function isWaterSession(s) {
    var t = (s.activity_type + " " + (s.activity_name || "")).toLowerCase();
    return /windsurf|kiteboard|kitesurf|wingfoil|wing foil|windfoil|sailing|foil/.test(t);
  }

  function renderSessionsTable() {
    var type = document.getElementById("typeFilter").value;
    var rows = type ? ALL_SESSIONS.filter(function (s) { return s.activity_type === type; }) : ALL_SESSIONS;
    var body = document.getElementById("sessionsBody");
    if (!rows.length) {
      body.innerHTML = '<tr><td colspan="8" class="empty-hint">No sessions.</td></tr>';
      return;
    }
    body.innerHTML = rows
      .map(function (s) {
        var durMin = s.duration_s ? Math.round(s.duration_s / 60) : "—";
        var distKm = s.distance_m ? (s.distance_m / 1000).toFixed(1) : "—";
        var time = (s.start_time_local || "").slice(11, 16);
        return (
          "<tr><td>" + escapeHtml(s.start_date) + "</td><td>" + escapeHtml(time) +
          "</td><td>" + escapeHtml(s.activity_type) + '</td><td class="loc-cell">' +
          escapeHtml(s.location || "") + '</td><td class="num">' + durMin +
          '</td><td class="num">' + distKm + '</td><td class="num">' +
          (s.calories ? Math.round(s.calories) : "—") + "</td><td>" +
          (isWaterSession(s)
            ? '<button type="button" class="tk-btn tk-analyse" data-aid="' + escapeHtml(s.activity_id) + '">Analyse</button>'
            : "") + "</td></tr>"
        );
      })
      .join("");
  }

  function loadSessions() {
    fetch("/api/sessions?limit=500")
      .then(function (r) { return r.json(); })
      .then(function (sessions) {
        ALL_SESSIONS = sessions;
        var types = Array.from(new Set(sessions.map(function (s) { return s.activity_type; }))).sort();
        var select = document.getElementById("typeFilter");
        var current = select.value;
        while (select.options.length > 1) select.remove(1); // reloads (after a sync) rebuild the list
        types.forEach(function (t) {
          var opt = document.createElement("option");
          opt.value = t;
          opt.textContent = t;
          select.appendChild(opt);
        });
        if (types.indexOf(current) >= 0) select.value = current;
        renderSessionsTable();
      });
  }

  // ---- calendar ----
  var KIND_LABELS = { training: "Training", race: "Race", plan: "Plan", blocked: "Can't train" };
  var REASON_LABELS = { exam: "Exam", friends: "Friends", party: "Party", other: "Other" };
  var MONTHS = ["January", "February", "March", "April", "May", "June", "July",
    "August", "September", "October", "November", "December"];
  var WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

  var calMonth = new Date();
  calMonth.setDate(1);
  var calEvents = [];
  var calSessions = [];
  var calEditingId = null;

  // Local-date ISO string; toISOString() would shift the day in UTC.
  function isoDate(d) {
    return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" +
      String(d.getDate()).padStart(2, "0");
  }

  function addDays(d, n) {
    var r = new Date(d.getFullYear(), d.getMonth(), d.getDate());
    r.setDate(r.getDate() + n);
    return r;
  }

  function calRange() {
    var first = new Date(calMonth.getFullYear(), calMonth.getMonth(), 1);
    var last = new Date(calMonth.getFullYear(), calMonth.getMonth() + 1, 0);
    var start = addDays(first, -((first.getDay() + 6) % 7));
    var end = addDays(last, 6 - ((last.getDay() + 6) % 7));
    return { first: first, last: last, start: start, end: end };
  }

  function eventCovers(ev, day) {
    return ev.date <= day && (ev.end_date || ev.date) >= day;
  }

  function eventLabel(ev) {
    var label = ev.title;
    if (ev.time) label = ev.time + " " + label;
    return label;
  }

  function prettyType(t) {
    return String(t || "").replace(/_v\d+$/, "").replace(/_/g, " ");
  }

  function fmtDuration(s) {
    if (!s) return "";
    var m = Math.round(s / 60);
    return m >= 60 ? Math.floor(m / 60) + "h" + String(m % 60).padStart(2, "0") : m + "m";
  }

  function fmtDayLong(iso) {
    var p = iso.split("-");
    var d = new Date(+p[0], +p[1] - 1, +p[2]);
    return WEEKDAYS[(d.getDay() + 6) % 7] + " " + d.getDate() + " " + MONTHS[d.getMonth()].slice(0, 3);
  }

  function renderCalendar() {
    var r = calRange();
    var today = isoDate(new Date());
    document.getElementById("calTitle").textContent = MONTHS[calMonth.getMonth()] + " " + calMonth.getFullYear();

    var grid = document.getElementById("calGrid");
    grid.innerHTML = "";
    WEEKDAYS.forEach(function (w) {
      var h = document.createElement("div");
      h.className = "cal-weekday";
      h.textContent = w;
      grid.appendChild(h);
    });

    for (var d = r.start; d <= r.end; d = addDays(d, 1)) {
      var iso = isoDate(d);
      var dayEvents = calEvents.filter(function (ev) { return eventCovers(ev, iso); });
      var daySessions = calSessions.filter(function (s) { return s.start_date === iso; });

      var cell = document.createElement("div");
      cell.className = "cal-day";
      if (d.getMonth() !== calMonth.getMonth()) cell.classList.add("outside");
      if (iso === today) cell.classList.add("today");
      if (dayEvents.some(function (ev) { return ev.kind === "blocked"; })) cell.classList.add("blocked");
      cell.dataset.date = iso;
      cell.tabIndex = 0;
      cell.setAttribute("role", "button");
      cell.setAttribute("aria-label", "Add event on " + fmtDayLong(iso));

      var num = document.createElement("span");
      num.className = "cal-daynum mono";
      num.textContent = d.getDate();
      cell.appendChild(num);

      dayEvents.forEach(function (ev) {
        var chip = document.createElement("button");
        chip.type = "button";
        chip.className = "cal-chip k-" + ev.kind;
        chip.textContent = eventLabel(ev);
        chip.title = KIND_LABELS[ev.kind] + (ev.reason ? " · " + REASON_LABELS[ev.reason] : "") +
          ": " + ev.title + (ev.notes ? "\n" + ev.notes : "");
        chip.addEventListener("click", function (e) {
          e.stopPropagation();
          openEventForm(ev);
        });
        cell.appendChild(chip);
      });

      daySessions.forEach(function (s) {
        var done = document.createElement("span");
        done.className = "cal-done";
        done.textContent = "✓ " + prettyType(s.activity_type) + " " + fmtDuration(s.duration_s);
        done.title = (s.activity_name || prettyType(s.activity_type)) + (s.location ? " · " + s.location : "");
        cell.appendChild(done);
      });

      cell.addEventListener("click", (function (dayIso) {
        return function () { openEventForm(null, dayIso); };
      })(iso));
      cell.addEventListener("keydown", function (e) {
        if ((e.key === "Enter" || e.key === " ") && e.target === this) {
          e.preventDefault();
          openEventForm(null, this.dataset.date);
        }
      });
      grid.appendChild(cell);
    }
    renderAgenda(r);
  }

  function renderAgenda(r) {
    var first = isoDate(r.first), last = isoDate(r.last);
    var list = document.getElementById("calAgenda");
    var items = calEvents.filter(function (ev) {
      return ev.date <= last && (ev.end_date || ev.date) >= first;
    });
    list.innerHTML = "";
    if (!items.length) {
      list.innerHTML = '<p class="empty-hint">Nothing marked this month yet.</p>';
      return;
    }
    items.forEach(function (ev) {
      var row = document.createElement("button");
      row.type = "button";
      row.className = "cal-agenda-row";
      var when = fmtDayLong(ev.date) + (ev.end_date ? " – " + fmtDayLong(ev.end_date) : "") +
        (ev.time ? " · " + ev.time : "");
      var kind = KIND_LABELS[ev.kind] + (ev.reason ? " · " + REASON_LABELS[ev.reason] : "");
      row.innerHTML =
        '<i class="cal-swatch k-' + ev.kind + '"></i>' +
        '<span class="cal-agenda-when mono">' + escapeHtml(when) + "</span>" +
        '<span class="cal-agenda-main"><span class="cal-agenda-title">' + escapeHtml(ev.title) +
        '</span><span class="cal-agenda-kind">' + escapeHtml(kind) +
        (ev.notes ? " — " + escapeHtml(ev.notes) : "") + "</span></span>";
      row.addEventListener("click", function () { openEventForm(ev); });
      list.appendChild(row);
    });
  }

  function loadCalendar() {
    var r = calRange();
    var start = isoDate(r.start), end = isoDate(r.end);
    return Promise.all([
      fetch("/api/calendar?start=" + start + "&end=" + end).then(function (res) { return res.json(); }),
      fetch("/api/sessions?limit=500&start=" + start + "&end=" + end)
        .then(function (res) { return res.json(); })
        .catch(function () { return []; }),
    ]).then(function (results) {
      calEvents = results[0];
      calSessions = results[1];
      renderCalendar();
    });
  }

  function shiftMonth(n) {
    calMonth = new Date(calMonth.getFullYear(), calMonth.getMonth() + n, 1);
    loadCalendar();
  }

  // ---- calendar: add / edit form ----
  var calDialog = document.getElementById("calDialog");
  var calForm = document.getElementById("calForm");

  function checkedValue(name) {
    var el = calForm.querySelector('input[name="' + name + '"]:checked');
    return el ? el.value : null;
  }

  function setChecked(name, value) {
    var el = calForm.querySelector('input[name="' + name + '"][value="' + value + '"]');
    if (el) el.checked = true;
  }

  function syncKindFields() {
    var blocked = checkedValue("kind") === "blocked";
    document.getElementById("calReasons").hidden = !blocked;
    document.getElementById("calFTitle").placeholder = blocked
      ? "e.g. Maths exam — leave empty to use the reason"
      : { training: "e.g. Intervals 6×3 min", race: "e.g. Nationals, Nida", plan: "e.g. Taper week" }[checkedValue("kind")];
  }

  function showFormError(msg) {
    var el = document.getElementById("calError");
    el.textContent = msg || "";
    el.hidden = !msg;
  }

  function openEventForm(ev, dayIso) {
    calEditingId = ev ? ev.id : null;
    document.getElementById("calFormTitle").textContent = ev ? "Edit event" : "New event";
    setChecked("kind", ev ? ev.kind : "training");
    setChecked("reason", (ev && ev.reason) || "exam");
    document.getElementById("calFTitle").value = ev ? ev.title : "";
    document.getElementById("calFDate").value = ev ? ev.date : (dayIso || isoDate(new Date()));
    document.getElementById("calFEnd").value = (ev && ev.end_date) || "";
    document.getElementById("calFTime").value = (ev && ev.time) || "";
    document.getElementById("calFNotes").value = (ev && ev.notes) || "";
    document.getElementById("calDelete").hidden = !ev;
    showFormError("");
    syncKindFields();
    calDialog.showModal();
    document.getElementById("calFTitle").focus();
  }

  function closeEventForm() {
    calDialog.close();
    calEditingId = null;
  }

  function errorText(res) {
    return res.json().then(function (body) {
      if (Array.isArray(body.detail)) {
        return body.detail.map(function (d) { return String(d.msg).replace(/^Value error, /, ""); }).join("; ");
      }
      return body.detail || "Couldn't save — try again.";
    }, function () { return "Couldn't save — try again."; });
  }

  calForm.querySelectorAll('input[name="kind"]').forEach(function (el) {
    el.addEventListener("change", syncKindFields);
  });

  calForm.addEventListener("submit", function (e) {
    e.preventDefault();
    var kind = checkedValue("kind");
    var reason = kind === "blocked" ? checkedValue("reason") : null;
    var title = document.getElementById("calFTitle").value.trim();
    if (!title && kind === "blocked") title = REASON_LABELS[reason];
    if (!title) {
      showFormError("Give it a title.");
      return;
    }
    var payload = {
      kind: kind,
      reason: reason,
      title: title,
      date: document.getElementById("calFDate").value,
      end_date: document.getElementById("calFEnd").value || null,
      time: document.getElementById("calFTime").value || null,
      notes: document.getElementById("calFNotes").value,
    };
    if (payload.end_date && payload.end_date < payload.date) {
      showFormError("“Until” can't be before the start date.");
      return;
    }
    var url = "/api/calendar" + (calEditingId ? "/" + calEditingId : "");
    fetch(url, {
      method: calEditingId ? "PUT" : "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }).then(function (res) {
      if (!res.ok) return errorText(res).then(showFormError);
      closeEventForm();
      // Jump to the saved event's month so it's visible right away.
      var p = payload.date.split("-");
      calMonth = new Date(+p[0], +p[1] - 1, 1);
      loadCalendar();
    }).catch(function () { showFormError("Couldn't reach the app — try again."); });
  });

  document.getElementById("calDelete").addEventListener("click", function () {
    if (!calEditingId) return;
    var title = document.getElementById("calFTitle").value.trim() || "this event";
    if (!confirm('Delete "' + title + '"?')) return;
    fetch("/api/calendar/" + calEditingId, { method: "DELETE" }).then(function (res) {
      if (!res.ok && res.status !== 404) return errorText(res).then(showFormError);
      closeEventForm();
      loadCalendar();
    });
  });

  document.getElementById("calCancel").addEventListener("click", closeEventForm);
  calDialog.addEventListener("click", function (e) {
    if (e.target === calDialog) closeEventForm();  // backdrop click
  });
  document.getElementById("calPrev").addEventListener("click", function () { shiftMonth(-1); });
  document.getElementById("calNext").addEventListener("click", function () { shiftMonth(1); });
  document.getElementById("calToday").addEventListener("click", function () {
    calMonth = new Date();
    calMonth.setDate(1);
    loadCalendar();
  });
  document.getElementById("calAdd").addEventListener("click", function () {
    var now = new Date();
    var inView = now.getMonth() === calMonth.getMonth() && now.getFullYear() === calMonth.getFullYear();
    openEventForm(null, inView ? isoDate(now) : isoDate(calMonth));
  });

  // ---- journal ----
  function loadJournal() {
    fetch("/api/journal")
      .then(function (r) { return r.json(); })
      .then(function (entries) {
        renderJournalList(document.getElementById("journalList"), entries);
      });
  }

  document.getElementById("journalDate").valueAsDate = new Date();

  document.getElementById("journalForm").addEventListener("submit", function (e) {
    e.preventDefault();
    var date = document.getElementById("journalDate").value;
    var text = document.getElementById("journalText").value.trim();
    if (!text) return;
    fetch("/api/journal", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ date: date, text: text }),
    }).then(function () {
      document.getElementById("journalText").value = "";
      loadJournal();
      loadDashboard();
    });
  });

  // ---- coach chat ----
  function addChatMessage(role, text) {
    var win = document.getElementById("chatWindow");
    var div = document.createElement("div");
    div.className = "chat-msg " + role;
    div.textContent = text;
    win.appendChild(div);
    win.scrollTop = win.scrollHeight;
  }

  // What the coach just changed in the calendar, with a one-click undo. Undo
  // replays the inverse through the regular calendar API, newest change first.
  function calChangeText(c) {
    var ev = c.event || c.previous;
    var when = fmtDayLong(ev.date) + (ev.end_date ? " – " + fmtDayLong(ev.end_date) : "") +
      (ev.time ? " " + ev.time : "");
    var what = KIND_LABELS[ev.kind] + ": " + ev.title;
    if (c.action === "added") return "Added " + what + " · " + when;
    if (c.action === "deleted") return "Removed " + what + " · " + when;
    var prev = c.previous;
    var moved = prev.date !== ev.date || prev.end_date !== ev.end_date || prev.time !== ev.time;
    return "Changed " + what + " · " + (moved ? "now " : "") + when;
  }

  function eventPayload(ev) {
    return {
      kind: ev.kind, date: ev.date, end_date: ev.end_date, time: ev.time,
      title: ev.title, reason: ev.reason, notes: ev.notes,
    };
  }

  function undoCalendarChange(c) {
    var json = { "Content-Type": "application/json" };
    if (c.action === "added") {
      return fetch("/api/calendar/" + c.event.id, { method: "DELETE" });
    }
    if (c.action === "updated") {
      return fetch("/api/calendar/" + c.previous.id, {
        method: "PUT", headers: json, body: JSON.stringify(eventPayload(c.previous)),
      });
    }
    return fetch("/api/calendar", {
      method: "POST", headers: json, body: JSON.stringify(eventPayload(c.previous)),
    });
  }

  function addCalendarChangeNote(changes) {
    var win = document.getElementById("chatWindow");
    var note = document.createElement("div");
    note.className = "chat-cal-note";
    var head = document.createElement("div");
    head.className = "chat-cal-head";
    var label = document.createElement("span");
    label.textContent = "Calendar updated";
    var undo = document.createElement("button");
    undo.type = "button";
    undo.className = "chat-cal-undo";
    undo.textContent = "Undo";
    head.appendChild(label);
    head.appendChild(undo);
    var list = document.createElement("ul");
    changes.forEach(function (c) {
      var li = document.createElement("li");
      li.textContent = calChangeText(c);
      list.appendChild(li);
    });
    note.appendChild(head);
    note.appendChild(list);
    win.appendChild(note);
    win.scrollTop = win.scrollHeight;

    undo.addEventListener("click", function () {
      undo.disabled = true;
      undo.textContent = "Undoing…";
      changes.slice().reverse().reduce(function (p, c) {
        return p.then(function () { return undoCalendarChange(c); });
      }, Promise.resolve()).then(function () {
        label.textContent = "Calendar changes undone";
        note.classList.add("undone");
        undo.remove();
      }).catch(function () {
        undo.disabled = false;
        undo.textContent = "Undo failed — retry";
      }).then(loadCalendar);
    });
  }

  // Conversations live server-side, so every device sees the same chats.
  var currentConversationId = null;

  function clearChatWindow() {
    document.getElementById("chatWindow").innerHTML = "";
  }

  function loadConversationList() {
    return fetch("/api/coach/conversations")
      .then(function (r) { return r.json(); })
      .then(function (convs) {
        var list = document.getElementById("chatHistory");
        list.innerHTML = "";
        if (!convs.length) {
          list.innerHTML = '<p class="empty-hint">No past chats yet.</p>';
          return convs;
        }
        convs.forEach(function (c) {
          var row = document.createElement("div");
          row.className = "chat-history-row" + (c.id === currentConversationId ? " active" : "");

          var open = document.createElement("button");
          open.type = "button";
          open.className = "chat-history-open";
          var title = document.createElement("span");
          title.className = "chat-history-title";
          title.textContent = c.title;
          var meta = document.createElement("span");
          meta.className = "chat-history-meta mono";
          meta.textContent = c.updated_at.slice(0, 10) + " · " + c.message_count + " messages";
          open.appendChild(title);
          open.appendChild(meta);
          open.addEventListener("click", function () { openConversation(c.id); });

          var del = document.createElement("button");
          del.type = "button";
          del.className = "chat-history-del";
          del.textContent = "Delete";
          del.addEventListener("click", function () {
            if (!confirm('Delete chat "' + c.title + '"?')) return;
            fetch("/api/coach/conversations/" + c.id, { method: "DELETE" }).then(function () {
              if (c.id === currentConversationId) startNewChat();
              else loadConversationList();
            });
          });

          row.appendChild(open);
          row.appendChild(del);
          list.appendChild(row);
        });
        return convs;
      });
  }

  function openConversation(id) {
    fetch("/api/coach/conversations/" + id + "/messages")
      .then(function (r) {
        if (!r.ok) throw new Error("conversation not found");
        return r.json();
      })
      .then(function (msgs) {
        currentConversationId = id;
        clearChatWindow();
        msgs.forEach(function (m) {
          addChatMessage(m.role === "assistant" ? "coach" : "user", m.content);
        });
        loadConversationList();
      })
      .catch(startNewChat);
  }

  function startNewChat() {
    currentConversationId = null;
    clearChatWindow();
    loadConversationList();
  }

  function loadLatestConversation() {
    loadConversationList().then(function (convs) {
      if (convs.length) openConversation(convs[0].id);
    });
  }

  // An older version of this page kept the chat in browser storage. Move any
  // such chat into the server database once, then drop the browser copy.
  var LEGACY_CHAT_KEY = "coachChatHistory";

  function importLegacyChat() {
    var legacy;
    try {
      legacy = JSON.parse(localStorage.getItem(LEGACY_CHAT_KEY)) || [];
    } catch (e) {
      legacy = [];
    }
    if (!Array.isArray(legacy) || !legacy.length) return Promise.resolve();
    return fetch("/api/coach/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages: legacy }),
    }).then(function (r) {
      if (r.ok) {
        try { localStorage.removeItem(LEGACY_CHAT_KEY); } catch (e) {}
      }
    }).catch(function () {});
  }

  document.getElementById("chatNew").addEventListener("click", startNewChat);

  document.getElementById("chatHistoryToggle").addEventListener("click", function () {
    var panel = document.getElementById("chatHistory");
    panel.hidden = !panel.hidden;
    this.setAttribute("aria-expanded", String(!panel.hidden));
  });

  function askCoach(query) {
    addChatMessage("user", query);
    fetch("/api/coach/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query: query, conversation_id: currentConversationId }),
    })
      .then(function (r) {
        if (!r.ok) throw new Error("coach request failed");
        return r.json();
      })
      .then(function (result) {
        currentConversationId = result.conversation_id;
        addChatMessage("coach", result.text);
        if (result.calendar_changes && result.calendar_changes.length) {
          addCalendarChangeNote(result.calendar_changes);
          loadCalendar();
        }
        loadConversationList();
      })
      .catch(function () {
        addChatMessage("coach", "Couldn't reach the coach -- try again.");
      });
  }

  document.getElementById("chatScrollTop").addEventListener("click", function () {
    document.getElementById("chatWindow").scrollTo({ top: 0, behavior: "smooth" });
  });

  document.getElementById("chatForm").addEventListener("submit", function (e) {
    e.preventDefault();
    var input = document.getElementById("chatInput");
    var query = input.value.trim();
    if (!query) return;
    input.value = "";
    askCoach(query);
  });

  function loadChatChips() {
    fetch("/api/coach/intents")
      .then(function (r) { return r.json(); })
      .then(function (d) {
        var container = document.getElementById("chatChips");
        container.innerHTML = "";
        d.examples.forEach(function (q) {
          var btn = document.createElement("button");
          btn.type = "button";
          btn.className = "chat-chip";
          btn.textContent = q;
          btn.addEventListener("click", function () { askCoach(q); });
          container.appendChild(btn);
        });
      });
  }

  document.getElementById("typeFilter").addEventListener("change", renderSessionsTable);

  // ---- Garmin sync button ----
  var syncBtn = document.getElementById("syncBtn");
  var syncLabel = document.getElementById("syncLabel");
  var syncStatus = document.getElementById("syncStatus");
  var syncTimer = null;

  function fmtSynced(iso) {
    if (!iso) return "Not synced yet";
    var d = new Date(iso);
    var mins = Math.round((Date.now() - d.getTime()) / 60000);
    if (mins < 1) return "Updated just now";
    if (mins < 60) return "Updated " + mins + " min ago";
    var hm = String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
    var today = new Date();
    if (d.toDateString() === today.toDateString()) return "Updated today " + hm;
    var y = new Date(today.getTime() - 86400000);
    if (d.toDateString() === y.toDateString()) return "Updated yesterday " + hm;
    return "Updated " + d.toISOString().slice(0, 10) + " " + hm;
  }

  function showSync(st, justFinished) {
    var busy = !!st.running;
    syncBtn.disabled = busy;
    syncBtn.classList.toggle("busy", busy);
    syncBtn.setAttribute("aria-busy", busy ? "true" : "false");
    syncLabel.textContent = busy ? "Syncing…" : "Sync Garmin";
    syncStatus.classList.toggle("err", !busy && st.ok === false);
    if (busy) syncStatus.textContent = st.step || "Syncing…";
    else if (justFinished && st.message) syncStatus.textContent = st.message + " " + fmtSynced(st.last_synced) + ".";
    else if (st.ok === false && st.message) syncStatus.textContent = st.message;
    else syncStatus.textContent = fmtSynced(st.last_synced);
  }

  function pollSync() {
    fetch("/api/sync")
      .then(function (r) { return r.json(); })
      .then(function (st) {
        if (st.running) {
          showSync(st);
          syncTimer = setTimeout(pollSync, 1500);
          return;
        }
        syncTimer = null;
        showSync(st, true);
        if (st.ok) {
          loadDashboard();
          loadSessions();
          loadCalendar();
          loadJournal();
          document.dispatchEvent(new CustomEvent("garmin-synced"));
        }
      })
      .catch(function () {
        syncTimer = setTimeout(pollSync, 4000);
      });
  }

  syncBtn.addEventListener("click", function () {
    showSync({ running: true, step: "Starting…" });
    fetch("/api/sync", { method: "POST" })
      .then(function (r) { return r.json(); })
      .then(function () { if (!syncTimer) pollSync(); })
      .catch(function () {
        showSync({ running: false, ok: false, message: "Couldn't reach the app — try again." });
      });
  });

  fetch("/api/sync")
    .then(function (r) { return r.json(); })
    .then(function (st) { showSync(st); if (st.running) pollSync(); })
    .catch(function () {});

  loadDashboard();
  loadSessions();
  loadJournal();
  loadCalendar();
  importLegacyChat().then(loadLatestConversation);
  loadChatChips();
})();
