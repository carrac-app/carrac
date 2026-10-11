/* ============================================================================
   CARRAC — MARKETING V5 (shared chrome behaviors for subpages)
   The landing page uses its own landing-v5.js (full spine/hero rig). This is
   the lean version: nav, reveal, accordion, cursor-glow, footer gold-landing,
   back-to-top. Every selector is guarded so it no-ops on pages that lack it.
   ============================================================================ */
(function () {
  "use strict";
  var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* ---- nav: frosted-on-scroll ---- */
  var nav = document.querySelector(".ed-nav");
  if (nav) {
    var onScroll = function () { nav.classList.toggle("scrolled", (window.scrollY || window.pageYOffset || 0) > 12); };
    window.addEventListener("scroll", onScroll, { passive: true });
    onScroll();
  }

  /* ---- nav: mobile toggle ---- */
  var toggle = document.querySelector(".nav-toggle");
  var mobile = document.querySelector(".nav-mobile");
  if (toggle && mobile) {
    toggle.addEventListener("click", function () {
      var open = mobile.classList.toggle("open");
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
    });
    mobile.querySelectorAll("a").forEach(function (a) {
      a.addEventListener("click", function () { mobile.classList.remove("open"); toggle.setAttribute("aria-expanded", "false"); });
    });
  }

  /* ---- nav: dropdown (click to toggle, hover-friendly, Esc/outside close) ---- */
  var navItems = [].slice.call(document.querySelectorAll(".nav-item.has-drop"));
  navItems.forEach(function (item) {
    var btn = item.querySelector(".nav-link");
    if (!btn) return;
    btn.addEventListener("click", function (e) {
      e.preventDefault(); e.stopPropagation();
      var open = !item.classList.contains("open");
      navItems.forEach(function (o) { o.classList.remove("open"); var b = o.querySelector(".nav-link"); if (b) b.setAttribute("aria-expanded", "false"); });
      item.classList.toggle("open", open);
      btn.setAttribute("aria-expanded", open ? "true" : "false");
    });
    item.querySelectorAll(".nav-drop a").forEach(function (a) {
      a.addEventListener("click", function () { item.classList.remove("open"); btn.setAttribute("aria-expanded", "false"); });
    });
  });
  if (navItems.length) {
    document.addEventListener("click", function (e) {
      navItems.forEach(function (item) { if (!item.contains(e.target)) { item.classList.remove("open"); var b = item.querySelector(".nav-link"); if (b) b.setAttribute("aria-expanded", "false"); } });
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") navItems.forEach(function (item) { item.classList.remove("open"); var b = item.querySelector(".nav-link"); if (b) b.setAttribute("aria-expanded", "false"); });
    });
  }

  /* ---- reveal-on-scroll: veil below-fold .rise, clear via IntersectionObserver ---- */
  var rises = [].slice.call(document.querySelectorAll(".rise"));
  if (rises.length && !reduce && "IntersectionObserver" in window) {
    var vh0 = window.innerHeight;
    rises.forEach(function (el) { if (el.getBoundingClientRect().top > vh0 * 0.86) el.classList.add("veil"); });
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) { if (en.isIntersecting) { en.target.classList.remove("veil"); io.unobserve(en.target); } });
    }, { rootMargin: "0px 0px -12% 0px" });
    rises.forEach(function (el) { if (el.classList.contains("veil")) io.observe(el); });
    // safety: never leave content hidden
    setTimeout(function () { document.querySelectorAll(".rise.veil").forEach(function (el) { el.classList.remove("veil"); }); }, 2600);
  }

  /* ---- legacy .reveal blocks: subpages use .reveal (not .rise); under normal motion .reveal is opacity:0 until .in is added. this script must add .in to .reveal blocks too or their content stays invisible. Add .in on intersect, with a no-IO fallback + safety so content is never stuck hidden. ---- */
  var reveals = [].slice.call(document.querySelectorAll(".reveal"));
  if (reveals.length) {
    if ("IntersectionObserver" in window) {
      var rio = new IntersectionObserver(function (entries) {
        entries.forEach(function (en) { if (en.isIntersecting) { en.target.classList.add("in"); rio.unobserve(en.target); } });
      }, { threshold: 0.12 });
      reveals.forEach(function (el) { rio.observe(el); });
      setTimeout(function () { document.querySelectorAll(".reveal:not(.in)").forEach(function (el) { el.classList.add("in"); }); }, 2600);
    } else {
      reveals.forEach(function (el) { el.classList.add("in"); });
    }
  }

  /* ---- progressive disclosure / accordion ---- */
  function openDisclose(d) {
    var panel = d.querySelector(".disclose-panel");
    if (!panel) return;
    d.classList.add("open");
    panel.style.maxHeight = (panel.scrollHeight + 28) + "px";
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

  /* ---- footer gold-landing: light it when the footer scrolls into view ---- */
  var footer = document.querySelector(".ed-foot");
  if (footer && !reduce && "IntersectionObserver" in window) {
    var fio = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) { footer.classList.toggle("lit", en.isIntersecting); });
    }, { rootMargin: "0px 0px -8% 0px" });
    fio.observe(footer);
  }

  /* ---- cursor-glow trailing light (desktop, motion-ok) ---- */
  var glow = document.querySelector(".cursor-glow");
  if (glow && !reduce && window.matchMedia && window.matchMedia("(hover: hover)").matches) {
    var gx = window.innerWidth / 2, gy = window.innerHeight / 2, cx = gx, cy = gy, shown = false;
    window.addEventListener("mousemove", function (e) { gx = e.clientX; gy = e.clientY; if (!shown) { shown = true; glow.classList.add("on"); } });
    document.addEventListener("mouseleave", function () { glow.classList.remove("on"); shown = false; });
    (function loop() { cx += (gx - cx) * 0.16; cy += (gy - cy) * 0.16; glow.style.transform = "translate(" + cx.toFixed(1) + "px," + cy.toFixed(1) + "px) translate(-50%,-50%)"; requestAnimationFrame(loop); })();
  }

  /* ---- cursor-tracking specular gleam on primary/ghost buttons ---- */
  [].forEach.call(document.querySelectorAll(".ed-btn.primary, .ed-btn.ghost, .nav-cta"), function (btn) {
    btn.addEventListener("mousemove", function (e) {
      var r = btn.getBoundingClientRect();
      btn.style.setProperty("--mx", ((e.clientX - r.left) / r.width * 100).toFixed(1) + "%");
      btn.style.setProperty("--my", ((e.clientY - r.top) / r.height * 100).toFixed(1) + "%");
    });
  });

  /* ---- back-to-top ---- */
  var top = document.getElementById("toTop");
  if (top) {
    var upd = function () { top.classList.toggle("on", (window.scrollY || 0) > window.innerHeight * 0.8); };
    window.addEventListener("scroll", upd, { passive: true });
    upd();
    top.addEventListener("click", function () { window.scrollTo({ top: 0, behavior: "smooth" }); });
  }

  /* ---- copy-to-clipboard buttons ([data-copy] or .cmd-copy in .install-cmd) ---- */
  document.addEventListener("click", function (e) {
    var btn = e.target.closest && e.target.closest("[data-copy], .install-cmd .cmd-copy");
    if (!btn || !navigator.clipboard) return;
    var cmd = btn.getAttribute("data-copy") || (btn.closest(".install-cmd") && btn.closest(".install-cmd").getAttribute("data-cmd"));
    if (!cmd) return;
    navigator.clipboard.writeText(cmd).then(function () {
      btn.classList.add("copied");
      var prev = btn.getAttribute("data-copy") ? btn.textContent : null;
      if (prev !== null) { btn.textContent = "Copied!"; setTimeout(function () { btn.textContent = prev; btn.classList.remove("copied"); }, 1600); }
      else { setTimeout(function () { btn.classList.remove("copied"); }, 1600); }
    }).catch(function(e) { if (typeof window.carracLog === 'function') window.carracLog('fetch', 'dynamic failed — rendered as empty', String((e && e.message) || e)); });
  });

  if (window.lucide && window.lucide.createIcons) window.lucide.createIcons();
})();
