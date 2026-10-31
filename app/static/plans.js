/* Plan tab: the coach's plan PDFs. Pages are shown as images rendered on the
   server (Chrome on Android can't show a PDF inside a page); the original
   PDF can be opened or downloaded. Newest plan first; older ones stay
   selectable. Uses the TK helpers from tracks-core.js. */
(function () {
  "use strict";

  var TK = window.TK, el = TK.el;
  var root = document.getElementById("planRoot");
  var tabBtn = document.querySelector('.tab-btn[data-tab="plan"]');
  var selected = null;

  function fmtDate(iso) {
    return new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
  }
  function fmtSize(b) { return b > 1048576 ? (b / 1048576).toFixed(1) + " MB" : Math.round(b / 1024) + " kB"; }

  function load() {
    root.textContent = "";
    root.appendChild(el("p", { class: "tk-msg", text: "Loading…" }));
    TK.api("/api/plans").then(function (list) {
      root.textContent = "";
      if (!list.length) {
        root.appendChild(TK.panel("Plan", "No plan yet — upload your coach's PDF below.", [uploadForm(true)]));
        return;
      }
      if (!selected || !list.some(function (p) { return p.id === selected; })) selected = list[0].id;
      var plan = list.filter(function (p) { return p.id === selected; })[0];
      root.appendChild(headPanel(plan, list));
      root.appendChild(pagesPanel(plan));
      root.appendChild(managePanel(plan));
    }).catch(function (e) {
      root.textContent = "";
      root.appendChild(el("p", { class: "tk-msg err", text: "Couldn't load the plan: " + e.message }));
    });
  }

  function headPanel(plan, list) {
    var pdf = "/api/plans/" + plan.id + "/pdf";
    var children = [
      el("div", { class: "plan-actions" }, [
        el("a", { class: "btn-primary", href: pdf, target: "_blank", rel: "noopener", text: "Open PDF" }),
        el("a", { class: "chat-tool-btn", href: pdf + "?download=true", text: "Download" }),
      ]),
    ];
    if (list.length > 1) {
      var pick = el("select", { "aria-label": "Plan" }, list.map(function (p) {
        return el("option", { value: p.id, selected: p.id === plan.id, text: p.title + " — " + fmtDate(p.uploaded_at) });
      }));
      pick.addEventListener("change", function () { selected = +pick.value; load(); });
      children.unshift(el("label", { class: "plan-pick" }, [el("span", { text: "Plan" }), pick]));
    }
    return TK.panel(plan.title, plan.pages + " page" + (plan.pages === 1 ? "" : "s") + " · " + fmtSize(plan.size_bytes) +
      " · added " + fmtDate(plan.uploaded_at), children, { id: "planHead" });
  }

  function pagesPanel(plan) {
    var wrap = el("div", { class: "plan-pages" });
    if (!plan.pages) {
      wrap.appendChild(el("p", { class: "tk-msg", text: "Page previews aren't available for this file — use Open PDF." }));
    }
    for (var n = 1; n <= plan.pages; n++) {
      wrap.appendChild(el("figure", { class: "plan-page" }, [
        el("img", { src: "/api/plans/" + plan.id + "/pages/" + n + ".png", alt: plan.title + ", page " + n,
          loading: n <= 2 ? "eager" : "lazy", "data-page": n }),
        el("figcaption", { text: "Page " + n + " of " + plan.pages }),
      ]));
    }
    return el("div", { class: "panel plan-pages-panel" }, [wrap]);
  }

  function uploadForm(first) {
    var file = el("input", { type: "file", accept: "application/pdf,.pdf", "aria-label": "Plan PDF" });
    var title = el("input", { type: "text", maxlength: 120, placeholder: "Title (optional, e.g. Winter plan Nov–Dec)" });
    var msg = el("span", { class: "tk-msg" });
    var btn = el("button", { type: "button", class: "btn-primary", text: first ? "Upload plan" : "Upload new plan", onclick: function () {
      if (!file.files.length) { msg.textContent = "Choose a PDF first."; return; }
      var fd = new FormData();
      fd.append("file", file.files[0]);
      if (title.value.trim()) fd.append("title", title.value.trim());
      btn.disabled = true;
      msg.textContent = "Uploading…";
      TK.api("/api/plans", { method: "POST", body: fd }).then(function (p) {
        selected = p.id;
        load();
      }).catch(function (e) { msg.textContent = "Couldn't upload: " + e.message; btn.disabled = false; });
    } });
    return el("div", { class: "plan-upload" }, [file, title, el("div", { class: "plan-actions" }, [btn, msg])]);
  }

  function managePanel(plan) {
    var title = el("input", { type: "text", maxlength: 120, value: plan.title, "aria-label": "Plan title" });
    var msg = el("span", { class: "tk-msg" });
    var save = el("button", { type: "button", class: "chat-tool-btn", text: "Rename", onclick: function () {
      if (!title.value.trim()) return;
      TK.api("/api/plans/" + plan.id, { method: "PATCH", body: { title: title.value.trim() } })
        .then(load).catch(function (e) { msg.textContent = e.message; });
    } });
    var del = el("button", { type: "button", class: "cal-delete", text: "Delete this plan", onclick: function () {
      if (!window.confirm("Delete \"" + plan.title + "\" and its PDF from the app?")) return;
      TK.api("/api/plans/" + plan.id, { method: "DELETE" }).then(function () { selected = null; load(); })
        .catch(function (e) { msg.textContent = e.message; });
    } });
    var d = el("details", { class: "panel plan-manage" }, [
      el("summary", { text: "Upload a new plan · rename · delete" }),
      el("h3", { text: "New plan from your coach" }),
      uploadForm(false),
      el("h3", { text: "This plan" }),
      el("div", { class: "plan-actions" }, [title, save, del, msg]),
    ]);
    return d;
  }

  tabBtn.addEventListener("click", load);
})();
