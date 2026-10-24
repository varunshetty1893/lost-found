/* Campus Lost & Found: progressive enhancement. Every page still works without this file. */
(function () {
  "use strict";
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };

  /* Mobile navigation */
  var toggle = $("[data-nav-toggle]"), nav = $("#site-nav");
  if (toggle && nav) {
    toggle.addEventListener("click", function () {
      var open = nav.classList.toggle("open");
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && nav.classList.contains("open")) { nav.classList.remove("open"); toggle.setAttribute("aria-expanded", "false"); toggle.focus(); }
    });
  }

  /* Close the account menu when clicking elsewhere */
  document.addEventListener("click", function (e) {
    $$("details.user-menu[open]").forEach(function (d) { if (!d.contains(e.target)) d.removeAttribute("open"); });
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") $$("details.user-menu[open]").forEach(function (d) { d.removeAttribute("open"); });
  });

  /* Toast notifications: every flash message shows bottom-right and disappears after ~3 seconds.
     Hovering pauses the timer. Without JavaScript the messages simply stay on the page. */
  var TOAST_MS = 3000;
  function dismiss(el) {
    if (!el.parentNode || el.classList.contains("leaving")) return;
    el.classList.add("leaving");
    setTimeout(function () { if (el.parentNode) el.remove(); }, 220);
  }
  function arm(el) {
    var close = $("[data-flash-close]", el), timer, left = TOAST_MS, started;
    el.style.setProperty("--toast-ms", TOAST_MS + "ms");
    function start() { started = Date.now(); timer = setTimeout(function () { dismiss(el); }, left); }
    function stop() { clearTimeout(timer); left = Math.max(800, left - (Date.now() - started)); }
    if (close) close.addEventListener("click", function () { clearTimeout(timer); dismiss(el); });
    el.addEventListener("mouseenter", stop);
    el.addEventListener("mouseleave", start);
    start();
  }
  $$("[data-flash]").forEach(arm);
  window.toast = function (message, type) {
    var box = $(".flashes");
    if (!box) { box = document.createElement("div"); box.className = "flashes"; document.body.appendChild(box); }
    var el = document.createElement("div");
    el.className = "flash flash-" + (type || "info");
    el.setAttribute("role", type === "error" ? "alert" : "status");
    el.setAttribute("data-flash", "");
    var text = document.createElement("span"); text.className = "flash-text"; text.textContent = message;
    el.appendChild(text); box.appendChild(el); arm(el);
  };

  /* Password show/hide */
  $$("[data-pw-toggle]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var input = document.getElementById(btn.getAttribute("data-pw-toggle"));
      if (!input) return;
      var show = input.type === "password";
      input.type = show ? "text" : "password";
      btn.setAttribute("aria-pressed", show ? "true" : "false");
      btn.setAttribute("aria-label", show ? "Hide password" : "Show password");
    });
  });

  /* Live password rule checklist */
  $$("[data-pw-rules]").forEach(function (list) {
    var input = document.getElementById(list.getAttribute("data-pw-rules"));
    if (!input) return;
    var tests = { length: function (v) { return v.length >= 8; }, letter: function (v) { return /[A-Za-z]/.test(v); }, number: function (v) { return /[0-9]/.test(v); } };
    var meter = $("[data-pw-meter=" + input.id + "]"), label = $("[data-pw-meter-label=" + input.id + "]");
    /* Strength: 1 bar = too short or missing a letter/number, 2 = okay, 3 = good, 4 = strong */
    var strength = function (v) {
      if (!v) return { bars: 0, text: "" };
      if (v.length < 8) return { bars: 1, text: "Too short" };
      if (!tests.letter(v) || !tests.number(v)) return { bars: 1, text: "Weak" };
      var bars = 2;
      if ((/[a-z]/.test(v) && /[A-Z]/.test(v)) || /[^A-Za-z0-9]/.test(v)) bars++;
      if (v.length >= 12) bars++;
      bars = Math.min(bars, 4);
      return { bars: bars, text: ["", "Weak", "Okay", "Good", "Strong"][bars] };
    };
    var update = function () {
      var v = input.value, st = strength(v);
      $$("li[data-rule]", list).forEach(function (li) { li.classList.toggle("ok", tests[li.getAttribute("data-rule")](v)); });
      if (meter) meter.setAttribute("data-level", st.bars);
      if (label) label.textContent = st.text;
    };
    input.addEventListener("input", update); update();
  });

  /* Character counters */
  $$("[data-counter]").forEach(function (out) {
    var input = document.getElementById(out.getAttribute("data-counter"));
    if (!input) return;
    var max = parseInt(input.getAttribute("maxlength"), 10), min = parseInt(input.getAttribute("minlength"), 10) || 0;
    var update = function () {
      var n = input.value.length;
      out.textContent = n + " / " + max;
      out.classList.toggle("low", n > 0 && n < min);
    };
    input.addEventListener("input", update); update();
  });

  /* Photo picker with preview and client-side checks (server still validates) */
  $$("[data-dropzone]").forEach(function (zone) {
    var input = $("input[type=file]", zone), empty = $("[data-dz-empty]", zone), preview = $("[data-dz-preview]", zone);
    var img = $("img", preview), meta = $("[data-dz-name]", preview), remove = $("[data-dz-remove]", preview), msg = $("[data-dz-error]", zone.parentNode);
    var types = (input.getAttribute("accept") || "").split(",");
    var maxBytes = 5 * 1024 * 1024, url = null;
    var fail = function (text) { if (msg) { msg.textContent = text; msg.hidden = !text; } };
    var reset = function () {
      input.value = ""; if (url) URL.revokeObjectURL(url); url = null;
      zone.classList.remove("has-file"); preview.hidden = true; empty.hidden = false;
    };
    input.addEventListener("change", function () {
      var f = input.files && input.files[0];
      fail("");
      if (!f) return reset();
      if (types.length && types[0] && types.indexOf(f.type) === -1) { reset(); return fail("Please choose a PNG, JPG, GIF or WEBP image."); }
      if (f.size > maxBytes) { reset(); return fail("That image is larger than 5 MB. Please choose a smaller one."); }
      if (url) URL.revokeObjectURL(url);
      url = URL.createObjectURL(f); img.src = url;
      meta.textContent = f.name + " (" + (f.size / 1024 < 1024 ? Math.round(f.size / 1024) + " KB" : (f.size / 1048576).toFixed(1) + " MB") + ")";
      zone.classList.add("has-file"); preview.hidden = false; empty.hidden = true;
    });
    remove.addEventListener("click", function () { fail(""); reset(); });
    ["dragenter", "dragover"].forEach(function (ev) { zone.addEventListener(ev, function () { zone.classList.add("drag"); }); });
    ["dragleave", "drop"].forEach(function (ev) { zone.addEventListener(ev, function () { zone.classList.remove("drag"); }); });
  });

  /* Report form: lost/found toggle changes wording and shows the custody field only for found items */
  var report = $("[data-report-form]");
  if (report) {
    var radios = $$("input[name=type]", report), custody = $("[data-custody]", report);
    var sync = function () {
      var kind = (radios.filter(function (r) { return r.checked; })[0] || {}).value || "lost";
      $$("[data-for]").forEach(function (el) { el.hidden = el.getAttribute("data-for") !== kind; });
      if (custody) { custody.hidden = kind !== "found"; $$("input", custody).forEach(function (i) { i.disabled = kind !== "found"; }); }
      document.title = document.title.replace(/Report a (lost|found) item/, "Report a " + kind + " item");
    };
    radios.forEach(function (r) { r.addEventListener("change", sync); }); sync();
  }

  /* Filters: apply select changes immediately (the Search button remains as a fallback) */
  $$("[data-autosubmit]").forEach(function (el) { el.addEventListener("change", function () { if (el.form) el.form.submit(); }); });

  /* Prevent double submits and show progress */
  $$("form[data-submit-once]").forEach(function (form) {
    form.addEventListener("submit", function (e) {
      if (form.dataset.busy === "1") { e.preventDefault(); return; }
      if (!form.checkValidity()) return;
      form.dataset.busy = "1"; form.setAttribute("aria-busy", "true");
      var btn = $("button[type=submit]", form);
      if (btn) { btn.classList.add("is-loading"); var l = $("[data-label]", btn); if (l && btn.dataset.loadingText) l.textContent = btn.dataset.loadingText; }
    });
  });
  window.addEventListener("pageshow", function (e) {
    if (!e.persisted) return;
    $$("form[data-submit-once]").forEach(function (f) { f.dataset.busy = "0"; f.removeAttribute("aria-busy"); $$(".is-loading", f).forEach(function (b) { b.classList.remove("is-loading"); }); });
  });

  /* Confirmation dialog (falls back to window.confirm) */
  $$("[data-confirm-dialog]").forEach(function (btn) {
    var dlg = document.getElementById(btn.getAttribute("data-confirm-dialog"));
    btn.addEventListener("click", function () {
      if (dlg && typeof dlg.showModal === "function") dlg.showModal();
      else if (window.confirm(btn.getAttribute("data-confirm-text") || "Are you sure?")) btn.form.submit();
    });
  });
  $$("dialog [data-dialog-close]").forEach(function (b) { b.addEventListener("click", function () { b.closest("dialog").close(); }); });

  /* Open a dialog or bottom sheet from any button: <button data-open-dialog="id"> */
  $$("[data-open-dialog]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var d = document.getElementById(btn.getAttribute("data-open-dialog"));
      if (d && typeof d.showModal === "function") d.showModal();
    });
  });
  /* Tapping the dark area outside a dialog closes it */
  $$("dialog").forEach(function (d) {
    d.addEventListener("click", function (e) { if (e.target === d) d.close(); });
  });

  /* Shared admin dialogs. A button carries data-modal (dialog id), data-action (form URL) and
     data-values (JSON). Matching form fields are filled, [data-fill] text is replaced and
     [data-show-if] blocks are shown only when that value is present. */
  $$("[data-modal]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var dlg = document.getElementById(btn.getAttribute("data-modal"));
      if (!dlg || typeof dlg.showModal !== "function") return;
      var form = $("form", dlg), v = {};
      try { v = JSON.parse(btn.getAttribute("data-values") || "{}"); } catch (err) { v = {}; }
      form.action = btn.getAttribute("data-action") || form.action;
      form.dataset.busy = "0"; form.removeAttribute("aria-busy");
      $$(".is-loading", form).forEach(function (b) { b.classList.remove("is-loading"); });
      $$("input, select, textarea", form).forEach(function (f) {
        if (f.name === "csrf_token" || f.type === "button") return;
        f.value = v[f.name] != null ? v[f.name] : (f.tagName === "SELECT" ? f.value : "");
      });
      $$("[data-fill]", dlg).forEach(function (el) { var k = el.getAttribute("data-fill"); el.textContent = v[k] != null ? v[k] : ""; });
      $$("[data-show-if]", dlg).forEach(function (el) { el.hidden = !v[el.getAttribute("data-show-if")]; });
      var cw = $("input[name=confirm_text]", dlg);
      if (cw) cw.required = !!v.confirm_word;
      var de = $("input[name=end_date]", dlg);
      if (de) de.required = !!v.date_label;
      var rs = $("select[name=role]", form);
      if (rs) rs.dispatchEvent(new Event("change", { bubbles: true }));
      var reasonBox = $("input[name=reason]", dlg);
      if (reasonBox) { reasonBox.required = !!v.reason_required; reasonBox.minLength = v.reason_min || 0; }
      var ok = $("[data-confirm-btn]", dlg);
      if (ok) ok.className = "btn " + (v.danger ? "btn-danger-solid" : "btn-primary");
      dlg.showModal();
    });
  });

  /* Course end date: only students have one, and for them it is required */
  $$("[data-end-field]").forEach(function (box) {
    var form = box.closest("form"), input = $("[data-end-input]", box), role = form && $("select[name=role]", form);
    if (!role || !input) return;
    function sync() {
      var student = role.value === "student";
      box.hidden = !student; input.required = student;
      if (!student) input.value = "";
    }
    role.addEventListener("change", sync); sync();
  });

  /* File picker: show the chosen file name */
  $$("[data-filepick]").forEach(function (box) {
    var input = $("input[type=file]", box), label = $("[data-file-label]", box), idle = label.textContent;
    input.addEventListener("change", function () {
      var f = input.files && input.files[0];
      label.textContent = f ? f.name : idle;
      box.classList.toggle("has-file", !!f);
    });
  });

  /* Confirm-password fields: <input data-match="password"> must equal the field with that id */
  $$("input[data-match]").forEach(function (confirm) {
    var first = document.getElementById(confirm.getAttribute("data-match"));
    var msg = $("[data-match-msg=" + confirm.id + "]");
    if (!first) return;
    function check() {
      var typed = confirm.value.length > 0, same = confirm.value === first.value;
      confirm.setCustomValidity(typed && !same ? "The two passwords do not match." : "");
      var wrap = confirm.closest(".pw-wrap");
      if (wrap) { wrap.classList.toggle("is-match", typed && same); wrap.classList.toggle("is-mismatch", typed && !same); }
      if (!msg) return;
      msg.textContent = !typed ? "" : (same ? "Passwords match." : "Passwords do not match yet.");
      msg.classList.toggle("is-ok", typed && same);
      msg.classList.toggle("is-bad", typed && !same);
    }
    confirm.addEventListener("input", check);
    first.addEventListener("input", check);
  });

  /* Department dropdown: students see academic departments only, staff also see administrative and support ones.
     "Other" reveals a text box. */
  $$("[data-dept-select]").forEach(function (sel) {
    var other = $("[data-dept-other]", sel.parentNode);
    var hint = $("[data-dept-hint]", sel.parentNode);
    var form = sel.form;
    var groups = $$("optgroup", sel).map(function (g) { return { node: g, audience: g.getAttribute("data-audience") || "all" }; });
    var otherOpt = $("option[value='__other__']", sel);
    var roleInputs = form ? $$("input[name=role]", form) : [];
    function role() {
      var on = roleInputs.filter(function (r) { return r.checked; })[0];
      return on ? on.value : "student";
    }
    function rebuild() {
      var isStaff = role() === "staff", keep = sel.value;
      groups.forEach(function (g) { if (g.node.parentNode) g.node.parentNode.removeChild(g.node); });
      groups.forEach(function (g) { if (g.audience === "all" || isStaff) sel.insertBefore(g.node, otherOpt); });
      var stillThere = keep && $$("option", sel).some(function (o) { return o.value === keep; });
      sel.value = stillThere ? keep : "";
      if (hint) hint.textContent = isStaff
        ? "Pick your academic, administrative or support department."
        : "MCA, BCA and BSc CS all come under Computer Science.";
      sync(false);
    }
    function sync(focus) {
      if (!other) return;
      var on = sel.value === "__other__";
      other.hidden = !on; other.required = on;
      if (on && focus) other.focus();
    }
    sel.addEventListener("change", function () { sync(true); });
    roleInputs.forEach(function (r) { r.addEventListener("change", rebuild); });
    rebuild();
  });

  /* Red * on every required field (labels, radio groups and the ID-card picker) */
  (function markRequired() {
    function star(el) {
      if (!el || el.querySelector(".req")) return;
      var s = document.createElement("span");
      s.className = "req"; s.setAttribute("aria-hidden", "true"); s.textContent = " *";
      el.appendChild(s);
    }
    $$("input[required], select[required], textarea[required]").forEach(function (f) {
      if (f.type === "hidden" || f.hasAttribute("data-dept-other")) return;
      if (f.type === "radio") { var fs = f.closest("fieldset"); if (fs) star($("legend", fs)); return; }
      if (f.type === "file") { var box = f.closest(".field"); if (box) star($(".field-label", box)); return; }
      var lab = f.id ? document.querySelector('label[for="' + f.id + '"]') : null;
      if (!lab) lab = f.closest("label");
      star(lab);
    });
  })();

  /* Phone: digits, spaces, + and - only. Browsers sometimes autofill an email here, so clean it out. */
  $$("input[data-phone]").forEach(function (el) {
    var clean = function () {
      var v = el.value.indexOf("@") > -1 ? "" : el.value.replace(/[^0-9+ \-]/g, "");
      if (v !== el.value) el.value = v;
    };
    el.addEventListener("input", clean); el.addEventListener("change", clean);
    [0, 300, 1200].forEach(function (t) { setTimeout(clean, t); });
  });

  /* Admin tables: copy column titles onto cells so phones can show them as stacked cards */
  $$("table.adm-table").forEach(function (table) {
    var heads = $$("thead th", table), primary = 0;
    heads.forEach(function (th, i) { if (th.hasAttribute("data-primary")) primary = i; });
    $$("tbody tr", table).forEach(function (tr) {
      $$("td", tr).forEach(function (td, i) {
        if (td.colSpan > 1 || !heads[i]) return;
        var th = heads[i];
        if (!th.querySelector(".sr-only") && th.textContent.trim()) td.setAttribute("data-label", th.textContent.trim());
        if (i === primary) td.classList.add("cell-primary");
      });
    });
  });

  /* Admin navigation drawer (tablet and phone) */
  var admBtn = $("[data-adm-menu]");
  function admMenu(open) {
    document.body.classList.toggle("adm-nav-open", open);
    if (admBtn) admBtn.setAttribute("aria-expanded", open ? "true" : "false");
  }
  if (admBtn) {
    admBtn.addEventListener("click", function () { admMenu(!document.body.classList.contains("adm-nav-open")); });
    $$("[data-adm-close]").forEach(function (el) { el.addEventListener("click", function () { admMenu(false); }); });
    document.addEventListener("keydown", function (e) { if (e.key === "Escape") admMenu(false); });
    window.addEventListener("resize", function () { if (window.innerWidth > 960) admMenu(false); });
  }

  /* Numeric code inputs */
  $$("[data-digits]").forEach(function (el) { el.addEventListener("input", function () { el.value = el.value.replace(/\D/g, "").slice(0, 6); }); });
})();
