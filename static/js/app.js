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

  /* Detail page gallery thumbnail switcher */
  var detailMainWrap = $("#detail-photo-wrapper");
  if (detailMainWrap) {
    var mainImg = $("img", detailMainWrap);
    var thumbs = $$("[data-gallery-thumb]");
    thumbs.forEach(function (btn) {
      btn.addEventListener("click", function () {
        var src = btn.getAttribute("data-src");
        if (mainImg && src) {
          mainImg.src = src;
          thumbs.forEach(function (t) { t.classList.remove("active"); });
          btn.classList.add("active");
        }
      });
    });
  }

  /* Multi-Photo Uploader & Interactive Crop + Pencil Canvas Editor (2 to 5 photos) */
  var photoInput = $("#f-photo-input");
  var photoGrid = $("#photo-preview-grid");
  var photoDropzone = $("#photo-dropzone");
  var photoCountBadge = $("#photo-count-badge");
  var photoErrMsg = $("[data-dz-error]");
  var editorModal = $("#photo-editor-modal");

  if (photoInput && photoGrid && photoDropzone) {
    var currentPhotos = []; // array of { file: File, previewUrl: string, name: string }
    var maxPhotos = 5;
    var maxBytes = 5 * 1024 * 1024;
    var allowedTypes = ["image/png", "image/jpeg", "image/webp"];

    var showPhotoError = function (txt) {
      if (!photoErrMsg) return;
      photoErrMsg.textContent = txt || "";
      photoErrMsg.hidden = !txt;
    };

    var syncInputFiles = function () {
      try {
        var dt = new DataTransfer();
        currentPhotos.forEach(function (p) { dt.items.add(p.file); });
        photoInput.files = dt.files;
      } catch (err) {
        // Fallback for older browsers
      }
      if (photoCountBadge) {
        photoCountBadge.textContent = currentPhotos.length + " / " + maxPhotos + " photos";
      }
      renderPhotoGrid();
    };

    var renderPhotoGrid = function () {
      photoGrid.innerHTML = "";
      if (currentPhotos.length === 0) {
        photoGrid.hidden = true;
        photoDropzone.hidden = false;
        return;
      }
      photoGrid.hidden = false;
      // Dropzone stays visible unless limit reached
      photoDropzone.hidden = currentPhotos.length >= maxPhotos;

      currentPhotos.forEach(function (p, idx) {
        var card = document.createElement("div");
        card.className = "photo-thumb-card";
        card.innerHTML =
          '<div class="photo-thumb-media">' +
            '<img src="' + p.previewUrl + '" alt="Photo ' + (idx + 1) + '">' +
            (idx === 0 ? '<span class="photo-thumb-badge">Cover</span>' : '') +
          '</div>' +
          '<div class="photo-thumb-actions">' +
            '<button type="button" class="btn-thumb btn-edit" title="Crop & Pencil tool">✏️ Edit</button>' +
            '<button type="button" class="btn-thumb btn-danger" title="Remove photo">✕</button>' +
          '</div>';

        var editBtn = $(".btn-edit", card);
        var remBtn = $(".btn-danger", card);

        editBtn.addEventListener("click", function () {
          openPhotoEditor(idx);
        });

        remBtn.addEventListener("click", function () {
          URL.revokeObjectURL(p.previewUrl);
          currentPhotos.splice(idx, 1);
          syncInputFiles();
        });

        photoGrid.appendChild(card);
      });
    };

    var addFiles = function (files) {
      showPhotoError("");
      var remaining = maxPhotos - currentPhotos.length;
      if (remaining <= 0) {
        showPhotoError("You can upload a maximum of " + maxPhotos + " pictures.");
        return;
      }

      var addedCount = 0;
      for (var i = 0; i < files.length; i++) {
        if (currentPhotos.length >= maxPhotos) {
          showPhotoError("Maximum 5 pictures allowed. Additional photos ignored.");
          break;
        }
        var f = files[i];
        if (allowedTypes.indexOf(f.type) === -1) {
          showPhotoError("Please select PNG, JPG or WEBP image files only.");
          continue;
        }
        if (f.size > maxBytes) {
          showPhotoError("'" + f.name + "' is larger than 5 MB. Please choose smaller images.");
          continue;
        }
        var previewUrl = URL.createObjectURL(f);
        currentPhotos.push({ file: f, previewUrl: previewUrl, name: f.name });
        addedCount++;
      }

      if (addedCount > 0) {
        syncInputFiles();
      }
    };

    photoInput.addEventListener("change", function () {
      if (photoInput.files && photoInput.files.length) {
        addFiles(Array.from(photoInput.files));
      }
    });

    ["dragenter", "dragover"].forEach(function (ev) {
      photoDropzone.addEventListener(ev, function () { photoDropzone.classList.add("drag"); });
    });
    ["dragleave", "drop"].forEach(function (ev) {
      photoDropzone.addEventListener(ev, function () { photoDropzone.classList.remove("drag"); });
    });
    photoDropzone.addEventListener("drop", function (e) {
      e.preventDefault();
      if (e.dataTransfer && e.dataTransfer.files) {
        addFiles(Array.from(e.dataTransfer.files));
      }
    });

    /* --- Interactive Canvas Editor (Crop & Pencil Mark) --- */
    var canvas = $("#photo-editor-canvas");
    var ctx = canvas ? canvas.getContext("2d") : null;
    var activePhotoIndex = -1;
    var loadedImage = null;

    // Viewport transform (Pan/Zoom within 4:3 800x600 fixed bounds)
    var viewScale = 1;
    var viewPanX = 0;
    var viewPanY = 0;

    // Tools & Drawing state
    var currentTool = "pencil"; // "pencil" | "pan"
    var currentColor = "#f96a46";
    var currentLineWidth = 6;
    var isInteracting = false;
    var lastPoint = null;
    var strokes = []; // array of { color, width, points: [{x, y}] } in canvas coordinate space

    var toolPencilBtn = $("#tool-pencil-btn");
    var toolPanBtn = $("#tool-pan-btn");
    var colorDots = $$(".color-dot");
    var strokeSelect = $("#stroke-size-select");
    var undoBtn = $("#editor-undo-btn");
    var clearBtn = $("#editor-clear-btn");
    var zoomSlider = $("#editor-zoom-slider");
    var resetZoomBtn = $("#editor-reset-zoom-btn");
    var saveBtn = $("#editor-save-btn");
    var cancelBtn = $("#editor-cancel-btn");
    var closeBtn = $("#editor-close-btn");
    var modeHint = $("#canvas-mode-hint");

    var redrawCanvas = function () {
      if (!ctx || !loadedImage) return;
      var w = canvas.width;  // 800
      var h = canvas.height; // 600

      ctx.save();
      ctx.fillStyle = "#11181c";
      ctx.fillRect(0, 0, w, h);

      // Center and fit loaded image into canvas with current zoom and pan
      ctx.save();
      ctx.translate(w / 2 + viewPanX, h / 2 + viewPanY);
      ctx.scale(viewScale, viewScale);

      var imgRatio = loadedImage.width / loadedImage.height;
      var canvasRatio = w / h; // 4:3
      var drawW, drawH;
      if (imgRatio > canvasRatio) {
        drawH = h;
        drawW = h * imgRatio;
      } else {
        drawW = w;
        drawH = w / imgRatio;
      }

      ctx.drawImage(loadedImage, -drawW / 2, -drawH / 2, drawW, drawH);
      ctx.restore();

      // Render pencil strokes on top
      strokes.forEach(function (st) {
        if (!st.points || st.points.length < 2) return;
        ctx.beginPath();
        ctx.strokeStyle = st.color;
        ctx.lineWidth = st.width;
        ctx.lineCap = "round";
        ctx.lineJoin = "round";
        ctx.moveTo(st.points[0].x, st.points[0].y);
        for (var i = 1; i < st.points.length; i++) {
          ctx.lineTo(st.points[i].x, st.points[i].y);
        }
        ctx.stroke();
      });

      ctx.restore();
    };

    var setTool = function (tool) {
      currentTool = tool;
      if (toolPencilBtn) toolPencilBtn.classList.toggle("active", tool === "pencil");
      if (toolPanBtn) toolPanBtn.classList.toggle("active", tool === "pan");
      if (canvas) canvas.classList.toggle("pan-mode", tool === "pan");
      if (modeHint) {
        modeHint.textContent = tool === "pencil"
          ? "✏️ Draw with pencil directly on the image to highlight marks, scratches or logos"
          : "✋ Drag on the image to pan and crop within the 4:3 frame";
      }
    };

    if (toolPencilBtn) toolPencilBtn.addEventListener("click", function () { setTool("pencil"); });
    if (toolPanBtn) toolPanBtn.addEventListener("click", function () { setTool("pan"); });

    colorDots.forEach(function (dot) {
      dot.addEventListener("click", function () {
        currentColor = dot.getAttribute("data-color");
        colorDots.forEach(function (d) { d.classList.remove("active"); });
        dot.classList.add("active");
        setTool("pencil");
      });
    });

    if (strokeSelect) {
      strokeSelect.addEventListener("change", function () {
        currentLineWidth = parseInt(strokeSelect.value, 10) || 6;
      });
    }

    if (undoBtn) {
      undoBtn.addEventListener("click", function () {
        if (strokes.length > 0) {
          strokes.pop();
          redrawCanvas();
        }
      });
    }

    if (clearBtn) {
      clearBtn.addEventListener("click", function () {
        strokes = [];
        redrawCanvas();
      });
    }

    if (zoomSlider) {
      zoomSlider.addEventListener("input", function () {
        viewScale = parseFloat(zoomSlider.value) || 1;
        redrawCanvas();
      });
    }

    if (resetZoomBtn) {
      resetZoomBtn.addEventListener("click", function () {
        viewScale = 1;
        viewPanX = 0;
        viewPanY = 0;
        if (zoomSlider) zoomSlider.value = "1";
        redrawCanvas();
      });
    }

    var getCanvasCoords = function (e) {
      var rect = canvas.getBoundingClientRect();
      var clientX = e.clientX || (e.touches && e.touches[0] ? e.touches[0].clientX : 0);
      var clientY = e.clientY || (e.touches && e.touches[0] ? e.touches[0].clientY : 0);
      var scaleX = canvas.width / rect.width;
      var scaleY = canvas.height / rect.height;
      return {
        x: (clientX - rect.left) * scaleX,
        y: (clientY - rect.top) * scaleY,
        rawX: clientX,
        rawY: clientY
      };
    };

    if (canvas) {
      var startInteraction = function (e) {
        e.preventDefault();
        isInteracting = true;
        var pos = getCanvasCoords(e);
        lastPoint = pos;

        if (currentTool === "pencil") {
          var newStroke = {
            color: currentColor,
            width: currentLineWidth,
            points: [{ x: pos.x, y: pos.y }]
          };
          strokes.push(newStroke);
          redrawCanvas();
        }
      };

      var moveInteraction = function (e) {
        if (!isInteracting) return;
        e.preventDefault();
        var pos = getCanvasCoords(e);

        if (currentTool === "pencil") {
          var curStroke = strokes[strokes.length - 1];
          if (curStroke) {
            curStroke.points.push({ x: pos.x, y: pos.y });
            redrawCanvas();
          }
        } else if (currentTool === "pan") {
          var dx = pos.rawX - lastPoint.rawX;
          var dy = pos.rawY - lastPoint.rawY;
          viewPanX += dx * (canvas.width / canvas.clientWidth);
          viewPanY += dy * (canvas.height / canvas.clientHeight);
          lastPoint = pos;
          redrawCanvas();
        }
      };

      var endInteraction = function () {
        isInteracting = false;
        lastPoint = null;
      };

      canvas.addEventListener("mousedown", startInteraction);
      window.addEventListener("mousemove", moveInteraction);
      window.addEventListener("mouseup", endInteraction);

      canvas.addEventListener("touchstart", startInteraction, { passive: false });
      window.addEventListener("touchmove", moveInteraction, { passive: false });
      window.addEventListener("touchend", endInteraction);
    }

    var openPhotoEditor = function (idx) {
      if (!editorModal || !currentPhotos[idx]) return;
      activePhotoIndex = idx;
      var photoObj = currentPhotos[idx];

      loadedImage = new Image();
      loadedImage.onload = function () {
        viewScale = 1;
        viewPanX = 0;
        viewPanY = 0;
        strokes = [];
        if (zoomSlider) zoomSlider.value = "1";
        setTool("pencil");
        redrawCanvas();
        if (typeof editorModal.showModal === "function") {
          editorModal.showModal();
        } else {
          editorModal.setAttribute("open", "true");
        }
      };
      loadedImage.src = photoObj.previewUrl;
    };

    var closePhotoEditor = function () {
      if (!editorModal) return;
      if (typeof editorModal.close === "function") {
        editorModal.close();
      } else {
        editorModal.removeAttribute("open");
      }
      activePhotoIndex = -1;
    };

    if (cancelBtn) cancelBtn.addEventListener("click", closePhotoEditor);
    if (closeBtn) closeBtn.addEventListener("click", closePhotoEditor);

    if (saveBtn) {
      saveBtn.addEventListener("click", function () {
        if (activePhotoIndex < 0 || !currentPhotos[activePhotoIndex] || !canvas) return;
        canvas.toBlob(function (blob) {
          if (!blob) return closePhotoEditor();
          var orig = currentPhotos[activePhotoIndex];
          var newFileName = orig.name.replace(/\.[^.]+$/, "") + "-edited.jpg";
          var newFile = new File([blob], newFileName, { type: "image/jpeg" });

          URL.revokeObjectURL(orig.previewUrl);
          orig.file = newFile;
          orig.previewUrl = URL.createObjectURL(newFile);
          orig.name = newFileName;

          syncInputFiles();
          closePhotoEditor();
        }, "image/jpeg", 0.92);
      });
    }
  } else {
    /* Fallback for single-photo dropzones elsewhere */
    $$("[data-dropzone]").forEach(function (zone) {
      var input = $("input[type=file]", zone), empty = $("[data-dz-empty]", zone), preview = $("[data-dz-preview]", zone);
      var img = $("img", preview), meta = $("[data-dz-name]", preview), remove = $("[data-dz-remove]", preview), msg = $("[data-dz-error]", zone.parentNode);
      var types = (input.getAttribute("accept") || "").split(",");
      var maxBytes = 5 * 1024 * 1024, url = null;
      var fail = function (text) { if (msg) { msg.textContent = text; msg.hidden = !text; } };
      var reset = function () {
        input.value = ""; if (url) URL.revokeObjectURL(url); url = null;
        zone.classList.remove("has-file"); if (preview) preview.hidden = true; if (empty) empty.hidden = false;
      };
      input.addEventListener("change", function () {
        var f = input.files && input.files[0];
        fail("");
        if (!f) return reset();
        if (types.length && types[0] && types.indexOf(f.type) === -1) { reset(); return fail("Please choose a PNG, JPG, GIF or WEBP image."); }
        if (f.size > maxBytes) { reset(); return fail("That image is larger than 5 MB. Please choose a smaller one."); }
        if (url) URL.revokeObjectURL(url);
        url = URL.createObjectURL(f); if (img) img.src = url;
        if (meta) meta.textContent = f.name + " (" + (f.size / 1024 < 1024 ? Math.round(f.size / 1024) + " KB" : (f.size / 1048576).toFixed(1) + " MB") + ")";
        zone.classList.add("has-file"); if (preview) preview.hidden = false; if (empty) empty.hidden = true;
      });
      if (remove) remove.addEventListener("click", function () { fail(""); reset(); });
      ["dragenter", "dragover"].forEach(function (ev) { zone.addEventListener(ev, function () { zone.classList.add("drag"); }); });
      ["dragleave", "drop"].forEach(function (ev) { zone.addEventListener(ev, function () { zone.classList.remove("drag"); }); });
    });
  }

  /* Report form: lost/found toggle and Option A/Option B contact visibility */
  var report = $("[data-report-form]");
  if (report) {
    var radios = $$("input[name=type]", report);
    var handoverRadios = $$("input[name=handover]", report);
    var contactWrap = $("[data-contact-wrapper]", report);
    var contactInput = $("[data-contact-input]", report);

    var sync = function () {
      var kind = (radios.filter(function (r) { return r.checked; })[0] || {}).value || "lost";
      var handover = (handoverRadios.filter(function (r) { return r.checked; })[0] || {}).value || "direct";

      $$("[data-for]", report).forEach(function (el) {
        el.hidden = el.getAttribute("data-for") !== kind;
      });

      if (contactWrap && contactInput) {
        var isDirect = handover === "direct";
        contactWrap.hidden = !isDirect;
        contactInput.required = isDirect;
      }

      document.title = document.title.replace(/Report a (lost|found) item/, "Report a " + kind + " item");
    };

    radios.forEach(function (r) { r.addEventListener("change", sync); });
    handoverRadios.forEach(function (r) { r.addEventListener("change", sync); });
    sync();
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
      $$("[data-roster-dept]", form).forEach(function (s) { s.dispatchEvent(new Event("change")); });
      var reasonBox = $("input[name=reason]", dlg);
      if (reasonBox) { reasonBox.required = !!v.reason_required; reasonBox.minLength = v.reason_min || 0; }
      var ok = $("[data-confirm-btn]", dlg);
      if (ok) ok.className = "btn " + (v.danger ? "btn-danger-solid" : "btn-primary");
      dlg.showModal();
    });
  });

  /* Roster dialogs: department is a required dropdown; "Other" reveals a text box */
  $$("[data-roster-dept]").forEach(function (sel) {
    var other = $("[data-roster-dept-other]", sel.parentNode);
    if (!other) return;
    function sync(focus) {
      var on = sel.value === "__other__";
      other.hidden = !on; other.required = on;
      if (!on) other.value = "";
      if (on && focus) other.focus();
    }
    sel.addEventListener("change", function () { sync(true); });
    sync(false);
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


  /* Scrollable tab rows: no scrollbar, but the wheel, touch and mouse-drag all work,
     the open tab is brought into view, and the edges fade when there is more to see */
  $$("[data-scroll-row]").forEach(function (row) {
    var wrap = row.parentNode, down = false, startX = 0, startLeft = 0, moved = false;
    function edges() {
      var max = row.scrollWidth - row.clientWidth;
      wrap.classList.toggle("can-left", row.scrollLeft > 4);
      wrap.classList.toggle("can-right", row.scrollLeft < max - 4);
    }
    var cur = row.querySelector("[aria-current]");
    if (cur) row.scrollLeft = Math.max(0, cur.offsetLeft - (row.clientWidth - cur.offsetWidth) / 2);
    row.addEventListener("scroll", edges, { passive: true });
    window.addEventListener("resize", edges);
    row.addEventListener("wheel", function (e) {
      if (row.scrollWidth <= row.clientWidth || Math.abs(e.deltaY) <= Math.abs(e.deltaX)) return;
      row.scrollLeft += e.deltaY; e.preventDefault();
    }, { passive: false });
    row.addEventListener("pointerdown", function (e) {
      if (e.pointerType !== "mouse") return;
      down = true; moved = false; startX = e.clientX; startLeft = row.scrollLeft;
    });
    window.addEventListener("pointermove", function (e) {
      if (!down) return;
      var dx = e.clientX - startX;
      if (Math.abs(dx) > 4) { moved = true; row.classList.add("is-dragging"); }
      row.scrollLeft = startLeft - dx;
    });
    window.addEventListener("pointerup", function () { down = false; row.classList.remove("is-dragging"); });
    row.addEventListener("click", function (e) { if (moved) { e.preventDefault(); moved = false; } }, true);
    edges();
  });

  /* Numeric code inputs */
  $$("[data-digits]").forEach(function (el) { el.addEventListener("input", function () { el.value = el.value.replace(/\D/g, "").slice(0, 6); }); });
})();
