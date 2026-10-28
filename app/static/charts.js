"use strict";
// Tiny SVG charts (no dependencies, no CDN: the app must work offline and under a strict CSP).
const Charts = (() => {
  const PALETTE = { analysis: "#8a9bb0", translate: "#cfac57", judge: "#5f7a3a", refine: "#a8702a",
    escalate: "#b04a43", redo: "#6d5a8a", other: "#b9b8b2" };
  const color = (k) => PALETTE[k] || PALETTE.other;
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const fmt = (n) => (n >= 10000 ? (n / 1000).toFixed(1) + "k" : String(Math.round(n)));

  function legend(keys) {
    // swatches as SVG: style attributes would be blocked by the Content-Security-Policy
    return `<div class="legend">${keys.map((k) => `<span><svg width="10" height="10"><rect width="10" height="10" rx="2" fill="${color(k)}"/></svg>${esc(k)}</span>`).join("")}</div>`;
  }

  // horizontal bars: rows = [{label, parts: {key: value}}]
  function stacked(rows, { unit = "", width = 640 } = {}) {
    const max = Math.max(1, ...rows.map((r) => Object.values(r.parts).reduce((a, b) => a + b, 0)));
    const keys = [...new Set(rows.flatMap((r) => Object.keys(r.parts)))];
    const rowH = 22, labelW = 110, barW = width - labelW - 70;
    const body = rows.map((r, i) => {
      let x = labelW;
      const total = Object.values(r.parts).reduce((a, b) => a + b, 0);
      const segs = keys.filter((k) => r.parts[k]).map((k) => {
        const w = (r.parts[k] / max) * barW;
        const s = `<rect x="${x}" y="${i * rowH + 3}" width="${Math.max(w, 0.5)}" height="${rowH - 8}" fill="${color(k)}"><title>${esc(k)}: ${fmt(r.parts[k])}${unit}</title></rect>`;
        x += w;
        return s;
      }).join("");
      return `<text x="${labelW - 8}" y="${i * rowH + 15}" text-anchor="end" class="lbl">${esc(r.label)}</text>${segs}<text x="${x + 6}" y="${i * rowH + 15}" class="val">${fmt(total)}${unit}</text>`;
    }).join("");
    return `<svg class="chart" viewBox="0 0 ${width} ${rows.length * rowH + 4}" width="100%">${body}</svg>${legend(keys)}`;
  }

  // vertical bars per page, stacked by key
  function columns(cols, { unit = "", height = 160, width = 640 } = {}) {
    const max = Math.max(1, ...cols.map((c) => Object.values(c.parts).reduce((a, b) => a + b, 0)));
    const keys = [...new Set(cols.flatMap((c) => Object.keys(c.parts)))];
    const gap = 4, w = Math.max(6, Math.min(40, (width - 40) / Math.max(1, cols.length) - gap));
    const body = cols.map((c, i) => {
      let y = height - 18;
      const x = 30 + i * (w + gap);
      const segs = keys.filter((k) => c.parts[k]).map((k) => {
        const h = (c.parts[k] / max) * (height - 30);
        y -= h;
        return `<rect x="${x}" y="${y}" width="${w}" height="${Math.max(h, 0.5)}" fill="${color(k)}"><title>${esc(c.label)} · ${esc(k)}: ${fmt(c.parts[k])}${unit}</title></rect>`;
      }).join("");
      return segs + `<text x="${x + w / 2}" y="${height - 4}" text-anchor="middle" class="lbl">${esc(c.label)}</text>`;
    }).join("");
    return `<svg class="chart" viewBox="0 0 ${Math.max(width, 40 + cols.length * (w + gap))} ${height}" width="100%">` +
      `<text x="2" y="12" class="val">${fmt(max)}${unit}</text>${body}</svg>${legend(keys)}`;
  }

  // timeline: spans = [{label, start, end, key}]
  function timeline(spans, { width = 640 } = {}) {
    const end = Math.max(1, ...spans.map((s) => s.end));
    const rowH = 20, labelW = 110, w = width - labelW - 50;
    const body = spans.map((s, i) => `<text x="${labelW - 8}" y="${i * rowH + 14}" text-anchor="end" class="lbl">${esc(s.label)}</text>` +
      `<rect x="${labelW + (s.start / end) * w}" y="${i * rowH + 3}" width="${Math.max(2, ((s.end - s.start) / end) * w)}" height="${rowH - 8}" fill="${color(s.key)}"><title>${esc(s.label)}: ${(s.end - s.start).toFixed(1)} s</title></rect>` +
      `<text x="${labelW + (s.end / end) * w + 4}" y="${i * rowH + 14}" class="val">${(s.end - s.start).toFixed(1)}s</text>`).join("");
    return `<svg class="chart" viewBox="0 0 ${width} ${spans.length * rowH + 4}" width="100%">${body}</svg>`;
  }

  return { stacked, columns, timeline, color };
})();
