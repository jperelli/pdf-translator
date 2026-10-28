"use strict";
// Landing page. The translator moved to /app: old links (/#job=..., /#history, /#admin) still open it.
(() => {
  if (/^#(job=|history|admin)/.test(location.hash)) {
    location.replace("/app" + location.hash);
    return;
  }

  // Measured on two pages of the Stark VARG manual, Quality mode, real models (see README "Results").
  const RUNS = {
    de: { name: "German", score: 99.4, translation: 99.4, layout: 99.9, cost: 0.0249, seconds: 15.6 },
    es: { name: "Spanish", score: 99.4, translation: 99.4, layout: 99.8, cost: 0.0308, seconds: 16.3 },
    ar: { name: "Arabic", score: 99.1, translation: 99.1, layout: 100, cost: 0.0261, seconds: 21.4 },
    zh: { name: "Chinese", score: 99.5, translation: 99.5, layout: 100, cost: 0.0197, seconds: 9.9 },
  };
  const $ = (s) => document.querySelector(s);

  function show(lang) {
    const r = RUNS[lang];
    $("#sc-t1").src = `/landing/p1-${lang}.webp`;
    $("#sc-t2").src = `/landing/p2-${lang}.webp`;
    $("#sc-t1").alt = `Page 1 translated into ${r.name}`;
    $("#sc-t2").alt = `Page 2 translated into ${r.name}`;
    $("#sc-label").textContent = r.name;
    const rows = [["Score", r.score.toFixed(1)], ["Translation", r.translation.toFixed(1)],
      ["Layout", r.layout.toFixed(1)], ["Overlapping lines", "0"], ["Cost", `$${r.cost.toFixed(3)}`],
      ["Time", `${r.seconds.toFixed(0)} s`]];
    $("#sc-scores").replaceChildren(...rows.map(([k, v]) => {
      const d = document.createElement("div");
      const dt = document.createElement("dt");
      const dd = document.createElement("dd");
      dt.textContent = k;
      dd.textContent = v;
      d.append(dd, dt);
      return d;
    }));
    document.querySelectorAll(".tabs [data-lang]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.lang === lang)));
  }
  document.querySelectorAll(".tabs [data-lang]").forEach((b) => b.addEventListener("click", () => show(b.dataset.lang)));
  show("de");

  $("#mcp-url").textContent = `${location.origin}/mcp`;
  $("#copy").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText($("#mcp-config").innerText);
      $("#copy").textContent = "Copied";
    } catch {
      $("#copy").textContent = "Select and copy";
    }
    setTimeout(() => ($("#copy").textContent = "Copy"), 1800);
  });

  fetch("/api/config").then((r) => r.json()).then((c) => {
    $("#lim-pages").textContent = c.limits.max_pages;
    $("#lim-mb").textContent = c.limits.max_upload_mb;
  }).catch(() => {});
})();
