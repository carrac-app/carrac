/* Carrac — editorial page: one rAF loop drives scroll-progress spine, section
   ticks, and reveal-on-scroll. Robust to embeds that don't fire scroll/IO:
   the loop polls scrollY every frame, and a timer hard-clears any veil. */
(function () {
  "use strict";
  var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  var nav = document.querySelector(".ed-nav");
  var fill = document.querySelector(".spine .fill");
  var node = document.querySelector(".spine .node");
  var spine = document.querySelector(".spine");
  var footer = document.querySelector(".ed-foot");
  var footerLit = false;
  var heroScroll = document.querySelector(".hero-scroll");
  var plates = [].slice.call(document.querySelectorAll(".plate"));
  var rises = [].slice.call(document.querySelectorAll(".rise"));

  /* place a tick on the spine per plate, proportional to its scroll position */
  var ticks = [];
  function layoutTicks() {
    if (!spine) return;
    var docH = document.documentElement.scrollHeight - window.innerHeight;
    ticks.forEach(function (t) { t.remove(); });
    ticks = [];
    plates.forEach(function (p) {
      var center = p.offsetTop + p.offsetHeight / 2;
      var pct = Math.max(0, Math.min(1, center / (docH || 1)));
      var t = document.createElement("div");
      t.className = "tick";
      t.style.top = (pct * 100) + "%";
      t.dataset.center = center;
      spine.appendChild(t);
      ticks.push(t);
    });
  }

  /* veil below-fold .rise elements so they animate in (only if motion ok) */
  if (!reduce) {
    var vh0 = window.innerHeight;
    rises.forEach(function (el) {
      var r = el.getBoundingClientRect();
      if (r.top > vh0 * 0.86) el.classList.add("veil");
    });
  }

  var lastY = -1;
  function frame() {
    var y = window.scrollY || window.pageYOffset || 0;
    if (y !== lastY) {
      lastY = y;
      var docH = document.documentElement.scrollHeight - window.innerHeight;
      var prog = docH > 0 ? Math.max(0, Math.min(1, y / docH)) : 0;
      if (nav) nav.classList.toggle("scrolled", y > 12);
      if (nav) nav.classList.toggle("cta-on", y > window.innerHeight * 0.55);
      if (spine) spine.classList.toggle("in-hero", y < window.innerHeight * 0.6);
      var vh = window.innerHeight;
      // end the spine exactly at the top of the footer as it scrolls into view
      if (spine && footer) spine.style.bottom = Math.max(0, vh - footer.getBoundingClientRect().top) + "px";
      if (fill) fill.style.height = (prog * 100) + "%";
      if (node) node.style.top = (prog * 100) + "%";
      // fire the footer ripple the instant the line's end reaches the footer's top edge
      if (footer && node) {
        var fTop = footer.getBoundingClientRect().top;
        var nodeBottom = node.getBoundingClientRect().bottom;
        if (!footerLit && fTop < vh && nodeBottom >= fTop - 1.5) { footer.classList.add("lit"); footerLit = true; }
        else if (footerLit && fTop > vh + 4) { footer.classList.remove("lit"); footerLit = false; }
      }
      // reveal
      for (var i = rises.length - 1; i >= 0; i--) {
        var el = rises[i];
        if (el.getBoundingClientRect().top < vh * 0.86) { el.classList.remove("veil"); rises.splice(i, 1); }
      }
      // ticks ignite once passed their center
      ticks.forEach(function (t) { t.classList.toggle("on", y + vh * 0.5 >= parseFloat(t.dataset.center)); });
      // fade the scroll cue as you leave the hero (no parallax)
      if (heroScroll) heroScroll.style.opacity = Math.max(0, 1 - y / 280);
    }
    requestAnimationFrame(frame);
  }

  function init() { layoutTicks(); requestAnimationFrame(frame); }
  if (document.readyState === "complete") init();
  else window.addEventListener("load", init);
  window.addEventListener("resize", layoutTicks);
  // hard fallback: never leave content hidden
  setTimeout(function () { document.querySelectorAll(".rise.veil").forEach(function (el) { el.classList.remove("veil"); }); }, 2600);

  /* hero film: auto-load a real clip from the project if present; otherwise
     keep the procedural aurora. Also supports drag/drop + click to preview. */
  var film = document.querySelector(".hero-film");
  if (film) {
    var vid = film.querySelector(".film");
    vid.addEventListener("loadeddata", function () { vid.playbackRate = 0.33; });

    function showVideo() { film.classList.add("has-video"); vid.playbackRate = 0.33; vid.play().catch(function(e) { if (typeof window.carracLog === 'function') window.carracLog('fetch', 'dynamic failed — rendered as empty', String((e && e.message) || e)); }); }
    function loadFile(file) {
      if (!file || !/^video\//.test(file.type)) return;
      vid.src = URL.createObjectURL(file); showVideo();
    }

    /* try the canonical project path(s). If none load, the aurora stays. */
    var candidates = ["assets/hero-aurora.mp4", "assets/hero-aurora.webm"];  // startrails dropped (non-looping/slow/laggy) — drop a suitable looping clip here as hero-aurora.mp4
    (function tryNext(i) {
      if (i >= candidates.length) return;            // none found → aurora remains
      var probe = document.createElement("video");
      probe.muted = true; probe.preload = "metadata";
      probe.onloadeddata = function () { vid.src = candidates[i]; showVideo(); };
      probe.onerror = function () { tryNext(i + 1); };
      probe.src = candidates[i];
    })(0);

    ["dragenter", "dragover"].forEach(function (ev) {
      film.addEventListener(ev, function (e) { e.preventDefault(); film.classList.add("drag"); });
    });
    film.addEventListener("dragleave", function () { film.classList.remove("drag"); });
    film.addEventListener("drop", function (e) {
      e.preventDefault(); film.classList.remove("drag");
      if (e.dataTransfer && e.dataTransfer.files[0]) loadFile(e.dataTransfer.files[0]);
    });
    var pick = film.querySelector(".ph-pick");
    if (pick) pick.addEventListener("click", function () {
      var inp = document.createElement("input"); inp.type = "file"; inp.accept = "video/*";
      inp.onchange = function () { if (inp.files[0]) loadFile(inp.files[0]); };
      inp.click();
    });
  }

  /* progressive disclosure: frame draws in, then text staggers in after a beat */
  function openDisclose(d) {
    var panel = d.querySelector(".disclose-panel");
    var inner = d.querySelector(".disclose-inner");
    if (!panel) return;
    d.classList.add("open");
    panel.style.maxHeight = (panel.scrollHeight + 28) + "px";
    if (reduce || !inner || !inner.animate) return;
    var anims = [];
    anims.push(inner.animate(
      [{ opacity: 0, transform: "translateY(-6px)", clipPath: "inset(0 0 100% 0)" },
       { opacity: 1, transform: "none", clipPath: "inset(0 0 0% 0)" }],
      { duration: 540, easing: "cubic-bezier(.22,.61,.36,1)", fill: "backwards" }));
    var nodes = [].slice.call(inner.querySelectorAll(".disclose-list li, .price-strip .pp"));
    [].forEach.call(inner.children, function (c) { if (c.tagName === "P") nodes.unshift(c); });
    nodes.forEach(function (n, i) {
      anims.push(n.animate(
        [{ opacity: 0, transform: "translateY(13px)" }, { opacity: 1, transform: "none" }],
        { duration: 560, delay: 240 + i * 80, easing: "cubic-bezier(.22,.61,.36,1)", fill: "backwards" }));
    });
    // safety: if the document timeline is frozen, clear animations so nothing stays hidden
    setTimeout(function () { anims.forEach(function (a) { try { a.cancel(); } catch (e) {} }); }, 1600);
  }
  function closeDisclose(d) {
    var panel = d.querySelector(".disclose-panel");
    d.classList.remove("open");
    if (panel) panel.style.maxHeight = "0px";
  }
  [].forEach.call(document.querySelectorAll(".disclose-btn"), function (btn) {
    btn.setAttribute("aria-expanded", "false");
    btn.addEventListener("click", function () {
      var d = btn.closest(".disclose");
      var willOpen = !d.classList.contains("open");
      btn.setAttribute("aria-expanded", willOpen ? "true" : "false");
      if (willOpen) openDisclose(d); else closeDisclose(d);
    });
  });
  window.addEventListener("resize", function () {
    [].forEach.call(document.querySelectorAll(".disclose.open .disclose-panel"), function (p) {
      p.style.maxHeight = (p.scrollHeight + 24) + "px";
    });
  });

  /* aurora dropped per request (non-looping/laggy) — hero stays static; drop a looping clip into assets/hero-aurora.mp4 to restore a moving hero */

  /* nav dropdown — click to toggle, hover-friendly, closes on outside/Esc */
  var navItems = [].slice.call(document.querySelectorAll(".nav-item.has-drop"));
  navItems.forEach(function (item) {
    var btn = item.querySelector(".nav-link");
    if (!btn) return;
    btn.addEventListener("click", function (e) {
      e.preventDefault(); e.stopPropagation();
      var open = !item.classList.contains("open");
      navItems.forEach(function (o) { o.classList.remove("open"); o.querySelector(".nav-link").setAttribute("aria-expanded", "false"); });
      item.classList.toggle("open", open);
      btn.setAttribute("aria-expanded", open ? "true" : "false");
    });
    item.querySelectorAll(".nav-drop a").forEach(function (a) {
      a.addEventListener("click", function () { item.classList.remove("open"); btn.setAttribute("aria-expanded", "false"); });
    });
  });
  document.addEventListener("click", function (e) {
    navItems.forEach(function (item) { if (!item.contains(e.target)) { item.classList.remove("open"); item.querySelector(".nav-link").setAttribute("aria-expanded", "false"); } });
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") navItems.forEach(function (item) { item.classList.remove("open"); item.querySelector(".nav-link").setAttribute("aria-expanded", "false"); });
  });

  /* soft light trailing the cursor (desktop, motion-ok only) */
  var glow = document.querySelector(".cursor-glow");
  if (glow && !reduce && window.matchMedia && window.matchMedia("(hover: hover)").matches) {
    var gx = window.innerWidth / 2, gy = window.innerHeight / 2, cx = gx, cy = gy, shown = false;
    window.addEventListener("mousemove", function (e) {
      gx = e.clientX; gy = e.clientY;
      if (!shown) { shown = true; glow.classList.add("on"); }
    });
    document.addEventListener("mouseleave", function () { glow.classList.remove("on"); shown = false; });
    (function glowLoop() {
      cx += (gx - cx) * 0.16; cy += (gy - cy) * 0.16;
      glow.style.transform = "translate(" + cx.toFixed(1) + "px," + cy.toFixed(1) + "px) translate(-50%,-50%)";
      requestAnimationFrame(glowLoop);
    })();
  }

  /* cursor-tracking specular gleam on platinum primary buttons */
  [].forEach.call(document.querySelectorAll(".ed-btn.primary, .ed-btn.ghost"), function (btn) {
    btn.addEventListener("mousemove", function (e) {
      var r = btn.getBoundingClientRect();
      btn.style.setProperty("--mx", ((e.clientX - r.left) / r.width * 100).toFixed(1) + "%");
      btn.style.setProperty("--my", ((e.clientY - r.top) / r.height * 100).toFixed(1) + "%");
    });
  });

  if (window.lucide && window.lucide.createIcons) window.lucide.createIcons();
})();


/* Hybrid skin: active-section lighting. Blocks dim slightly at
   viewport edges and lift as they cross the center band. Dimming only arms
   once this observer is confirmed running (html.sectionlit-armed). */
(function () {
  var sel = ".sf-item,.report-card,.compare-sec .compare,.deeper-head,.surf-cap,.mcp-pt,.plate-inner,.manifesto p";
  var els = document.querySelectorAll(sel);
  if (!els.length || !("IntersectionObserver" in window)) return;
  document.documentElement.classList.add("sectionlit-armed");
  var io = new IntersectionObserver(function (es) {
    es.forEach(function (e) { e.target.classList.toggle("lit", e.isIntersecting); });
  }, { rootMargin: "-28% 0px -28% 0px" });
  els.forEach(function (el) { io.observe(el); });
})();
