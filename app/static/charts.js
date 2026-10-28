"use strict";
// Interactive charts for the Stats tab, on Chart.js (vendored: app/static/vendor, MIT). Hover shows the numbers,
// clicking a page bar opens that page in Preview, the legend toggles series.
const Charts = (() => {
  const PALETTE = { analysis: "#8a9bb0", translate: "#cfac57", judge: "#5f7a3a", refine: "#a8702a",
    escalate: "#b04a43", redo: "#6d5a8a", other: "#b9b8b2" };
  const color = (k) => PALETTE[k] || PALETTE.other;
  const live = new Map(); // canvas id -> Chart

  const METRICS = {
    tokens: { label: "tokens", fmt: (n) => Math.round(n).toLocaleString() },
    cost: { label: "USD", fmt: (n) => "$" + (n < 0.01 ? n.toFixed(4) : n.toFixed(3)) },
    requests: { label: "requests", fmt: (n) => String(Math.round(n)) },
    seconds: { label: "seconds", fmt: (n) => n.toFixed(n < 1 && n > 0 ? 2 : 1) + " s" },
  };

  if (window.Chart) {
    Chart.defaults.font.family = "Archivo, system-ui, sans-serif";
    Chart.defaults.font.size = 12;
    Chart.defaults.color = "#5d5b55";
    Chart.defaults.plugins.tooltip.backgroundColor = "rgba(28, 27, 25, .92)";
    Chart.defaults.plugins.tooltip.padding = 10;
    Chart.defaults.plugins.tooltip.boxPadding = 4;
    Chart.defaults.plugins.legend.labels.boxWidth = 12;
  }

  function draw(id, config) {
    const canvas = document.getElementById(id);
    if (!canvas || !window.Chart) return null;
    live.get(id)?.destroy();
    const chart = new Chart(canvas, config);
    live.set(id, chart);
    return chart;
  }

  function destroyAll() {
    for (const c of live.values()) c.destroy();
    live.clear();
  }

  const grid = { color: "rgba(0, 0, 0, .06)" };

  // stages: [{key, tokens, cost, requests, seconds, input, output}]
  function byStage(id, stages, metric) {
    const m = METRICS[metric];
    return draw(id, {
      type: "bar",
      data: {
        labels: stages.map((s) => s.key),
        datasets: [{ data: stages.map((s) => s[metric]), backgroundColor: stages.map((s) => color(s.key)),
          borderRadius: 4, maxBarThickness: 26 }],
      },
      options: {
        indexAxis: "y", responsive: true, maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: { callbacks: {
            label: (ctx) => `${m.fmt(ctx.parsed.x)} ${metric === "tokens" ? "tokens" : ""}`.trim(),
            afterLabel: (ctx) => {
              const s = stages[ctx.dataIndex];
              return [`input ${METRICS.tokens.fmt(s.input)} · output ${METRICS.tokens.fmt(s.output)}`,
                `${s.requests} request(s) · ${METRICS.cost.fmt(s.cost)} · ${METRICS.seconds.fmt(s.seconds)}`];
            },
          } },
        },
        scales: { x: { grid, ticks: { callback: (v) => m.fmt(v) } }, y: { grid: { display: false } } },
      },
    });
  }

  // pages: [{page, parts: {stage: {tokens, cost, ...}}}]; onPick(page) when a bar is clicked
  function perPage(id, pages, metric, onPick) {
    const m = METRICS[metric];
    const keys = [...new Set(pages.flatMap((p) => Object.keys(p.parts)))];
    return draw(id, {
      type: "bar",
      data: {
        labels: pages.map((p) => `p. ${p.page}`),
        datasets: keys.map((k) => ({ label: k, data: pages.map((p) => (p.parts[k] ? p.parts[k][metric] : 0)),
          backgroundColor: color(k), borderRadius: 2, maxBarThickness: 40 })),
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        onClick: (_e, els) => { if (els.length && onPick) onPick(pages[els[0].index].page); },
        onHover: (e, els) => { e.native.target.style.cursor = els.length ? "pointer" : "default"; },
        plugins: {
          legend: { position: "bottom" },
          tooltip: {
            filter: (item) => item.parsed.y > 0,
            callbacks: {
              title: (items) => `Page ${pages[items[0].dataIndex].page}`,
              label: (ctx) => ` ${ctx.dataset.label}: ${m.fmt(ctx.parsed.y)}`,
              footer: (items) => {
                const total = items.reduce((a, i) => a + i.parsed.y, 0);
                return [`total ${m.fmt(total)}`, "click to open this page in Preview"];
              },
            },
          },
        },
        scales: { x: { stacked: true, grid: { display: false } },
          y: { stacked: true, grid, ticks: { callback: (v) => m.fmt(v) } } },
      },
    });
  }

  // spans: [{label, start, end, key}] in seconds since the job started
  function timeline(id, spans) {
    return draw(id, {
      type: "bar",
      data: {
        labels: spans.map((s) => s.label),
        datasets: [{ data: spans.map((s) => [s.start, Math.max(s.end, s.start + 0.05)]),
          backgroundColor: spans.map((s) => color(s.key)), borderRadius: 3, borderSkipped: false, maxBarThickness: 22 }],
      },
      options: {
        indexAxis: "y", responsive: true, maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: { callbacks: { label: (ctx) => {
            const s = spans[ctx.dataIndex];
            return `${(s.end - s.start).toFixed(1)} s (from ${s.start.toFixed(1)} s to ${s.end.toFixed(1)} s)`;
          } } },
        },
        scales: { x: { grid, title: { display: true, text: "seconds since start" } }, y: { grid: { display: false } } },
      },
    });
  }

  // versions: [{version, score, tokens, cost_usd}]
  function versions(id, rows) {
    return draw(id, {
      type: "bar",
      data: {
        labels: rows.map((v) => `v${v.version}`),
        datasets: [
          { type: "bar", label: "tokens", data: rows.map((v) => v.tokens), backgroundColor: color("translate"),
            yAxisID: "y", borderRadius: 3, maxBarThickness: 36 },
          { type: "line", label: "score", data: rows.map((v) => v.score), borderColor: color("judge"),
            backgroundColor: color("judge"), yAxisID: "score", tension: 0.2 },
        ],
      },
      options: {
        responsive: true, maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
        plugins: { legend: { position: "bottom" }, tooltip: { callbacks: {
          label: (ctx) => ctx.dataset.label === "score" ? ` score ${Math.round(ctx.parsed.y)}`
            : ` ${METRICS.tokens.fmt(ctx.parsed.y)} tokens · ${METRICS.cost.fmt(rows[ctx.dataIndex].cost_usd)}`,
        } } },
        scales: { y: { grid, position: "left" }, score: { position: "right", min: 0, max: 100, grid: { display: false } },
          x: { grid: { display: false } } },
      },
    });
  }

  return { byStage, perPage, timeline, versions, destroyAll, METRICS, available: () => !!window.Chart };
})();
