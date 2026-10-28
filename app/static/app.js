"use strict";
const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const markup = (s) => esc(s).replace(/&lt;(\/?)(b|i|u)&gt;/g, "<$1$2>").replace(/&lt;br\s*\/?&gt;/g, "<br>");
const STAGES = ["validate", "extract", "analyze", "translate", "render", "done"];
const state = { config: null, file: null, job: null, version: null, pages: 0, page: 1, result: null, events: [],
  history: { offset: 0, limit: 25 }, decisions: {}, logFilter: new Set() };

const cls = (score) => (score >= 85 ? "ok" : score >= 65 ? "mid" : "bad");
const fmtTokens = (n) => (n > 9999 ? (n / 1000).toFixed(1) + "k" : String(Math.round(n)));
const fmtUsd = (n) => "$" + (n < 0.01 ? n.toFixed(4) : n.toFixed(2));
const fmtDate = (t) => new Date(t * 1000).toLocaleString();

async function api(path, opts) {
  const res = await fetch(path, opts);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || `Request failed (${res.status})`);
  return body;
}
const postJson = (path, data) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) });

function show(id) {
  for (const s of ["upload", "progress", "result", "history"]) $("#" + s).hidden = s !== id;
  $("#nav-history").classList.toggle("active", id === "history");
  $("#nav-new").classList.toggle("active", id === "upload");
}

function tabs(group, onShow) {
  $$(`[data-group="${group}"] > [data-tab]`).forEach((t) => (t.onclick = () => {
    $$(`[data-group="${group}"] > [data-tab]`).forEach((x) => x.classList.toggle("active", x === t));
    $$(`.tab-body[data-group="${group}"]`).forEach((b) => (b.hidden = b.dataset.body !== t.dataset.tab));
    onShow && onShow(t.dataset.tab);
  }));
}
function openTab(group, name) { const t = $(`[data-group="${group}"] > [data-tab="${name}"]`); t && t.click(); }

// ------------------------------------------------------------------ routing
function route() {
  const h = location.hash;
  const m = h.match(/job=([0-9a-f]{32})(?:&v=(\d+))?/);
  if (h === "#history") return loadHistory();
  if (m) return resume(m[1], m[2] ? Number(m[2]) : null);
  reset(false);
}
window.addEventListener("hashchange", route);

// ------------------------------------------------------------------ upload
async function init() {
  state.config = await api("/api/config");
  const mode = $("#mode");
  if (state.config.backend === "openai") { mode.textContent = `Live · ${state.config.models.translator}`; mode.className = "mode live"; }
  else { mode.textContent = "Demo mode · pseudo-translation"; mode.className = "mode pseudo"; }
  const sel = $("#target");
  for (const l of state.config.languages) sel.add(new Option(`${l.name} · ${l.native}`, l.code));
  sel.value = localStorage.getItem("target") || "es";
  const purpose = localStorage.getItem("purpose") || "publish";
  $$('input[name="purpose"]').forEach((r) => (r.checked = r.value === purpose));
  $("#drop-hint").textContent = `Up to ${state.config.limits.max_upload_mb} MB and ${state.config.limits.max_pages} pages`;
  const samples = await api("/api/samples").catch(() => []);
  for (const s of samples) {
    const b = document.createElement("button");
    b.className = "chip"; b.type = "button"; b.title = s.description;
    b.innerHTML = `${esc(s.name.replace(/\.pdf$/, ""))}<small>${s.pages} p.</small>`;
    b.onclick = () => start({ sample: s.name });
    $("#samples").append(b);
  }
  tabs("main", (t) => t === "preview" && renderPage());
  tabs("details");
  route();
}

function pickFile(f) {
  $("#form-error").hidden = true;
  if (!f) return;
  const mb = state.config.limits.max_upload_mb;
  if (!/\.pdf$/i.test(f.name) && f.type !== "application/pdf") return formError("Only PDF files are supported for now.");
  if (f.size > mb * 1024 * 1024) return formError(`This file is ${(f.size / 1048576).toFixed(1)} MB; the limit is ${mb} MB.`);
  state.file = f;
  $("#drop").classList.add("has-file");
  $("#drop-text").innerHTML = `<b>${esc(f.name)}</b> · ${(f.size / 1024).toFixed(0)} KB`;
  $("#go").disabled = false;
}
function formError(msg) { const e = $("#form-error"); e.textContent = msg; e.hidden = false; }

const drop = $("#drop");
drop.addEventListener("click", () => $("#file").click());
drop.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); $("#file").click(); } });
$("#file").addEventListener("change", (e) => pickFile(e.target.files[0]));
for (const ev of ["dragenter", "dragover"]) drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); });
for (const ev of ["dragleave", "drop"]) drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); });
drop.addEventListener("drop", (e) => pickFile(e.dataTransfer.files[0]));
$("#target").addEventListener("change", (e) => localStorage.setItem("target", e.target.value));
$$('input[name="purpose"]').forEach((r) => r.addEventListener("change", () => localStorage.setItem("purpose", r.value)));
$("#form").addEventListener("submit", (e) => { e.preventDefault(); if (state.file) start({ file: state.file }); });
$$(".new-btn").forEach((b) => (b.onclick = () => { location.hash = ""; reset(true); }));
$("#nav-new").onclick = (e) => { e.preventDefault(); location.hash = ""; reset(true); };

function reset(clear) {
  if (clear) history.replaceState(null, "", location.pathname);
  Object.assign(state, { file: null, job: null, result: null, events: [], decisions: {} });
  $("#drop").classList.remove("has-file");
  $("#drop-text").innerHTML = "<b>Drop a PDF here</b> or click to choose a file";
  $("#go").disabled = true;
  $("#file").value = "";
  show("upload");
}

async function start({ file, sample }) {
  const fd = new FormData();
  fd.append("target", $("#target").value);
  fd.append("purpose", ($('input[name="purpose"]:checked') || {}).value || "publish");
  if (file) fd.append("file", file);
  if (sample) fd.append("sample", sample);
  $("#go").disabled = true;
  try {
    const job = await api("/api/jobs", { method: "POST", body: fd });
    history.replaceState(null, "", "#job=" + job.id);
    follow(job);
  } catch (err) { formError(err.message); $("#go").disabled = !state.file; }
}

async function resume(id, version) {
  try {
    const job = await api(`/api/jobs/${id}` + (version ? `?version=${version}` : ""));
    if (job.status === "done" || (job.status === "error" && job.result)) { state.job = job; showResult(job); }
    else follow(job);
  } catch { reset(true); }
}

// ------------------------------------------------------------------ progress
function follow(job) {
  state.job = job;
  state.events = [];
  const lang = state.config.languages.find((l) => l.code === job.target);
  $("#job-title").textContent = job.filename + (job.version > 1 ? ` · v${job.version}` : "");
  $("#job-sub").textContent = `→ ${lang ? lang.name : job.target} · ${job.purpose === "understand" ? "Understand (draft)" : "Publish"}`;
  $("#log").innerHTML = ""; $("#pages").innerHTML = ""; $("#job-error").hidden = true;
  $("#bar-fill").style.width = "0";
  show("progress");
  const es = new EventSource(`/api/jobs/${job.id}/events`);
  es.onmessage = (m) => onEvent(JSON.parse(m.data));
  es.addEventListener("end", () => es.close());
  es.onerror = () => { es.close(); setTimeout(() => resume(job.id), 1500); };
}

function setStage(stage, percent, message) {
  const idx = STAGES.indexOf(stage === "review" ? "render" : stage);
  $$("#steps li").forEach((li, i) => { li.classList.toggle("done", idx >= 0 && i < idx); li.classList.toggle("active", i === idx); });
  if (percent != null) $("#bar-fill").style.width = percent + "%";
  if (message) $("#stage-msg").textContent = message;
}

// one formatter for the live log and the Log tab
function describe(e) {
  switch (e.type) {
    case "stage": return e.stage === "done" ? null : ["Stage", e.message];
    case "extracted": return ["Extract", `${e.blocks} text blocks on ${e.pages} pages` + (e.scanned_pages && e.scanned_pages.length ? `, OCR on pages ${e.scanned_pages.join(", ")}` : "")];
    case "analysis": { const a = e.analysis; return ["Analyst", `${a.source_language_name} · ${a.tone} · ${a.domain} · ${a.protected_terms.length} protected terms, ${a.jargon.length} jargon terms`]; }
    case "page_step": return ["Page", e.step === "translated" ? `Page ${e.page}: ${e.blocks} blocks translated, reviewing layout and quality` : `Page ${e.page}: review done, ${e.failing ? e.failing + " block(s) need another pass" : "all blocks pass"}`];
    case "layout": return ["Layout", `Page ${e.page}: ${e.collisions} block(s) collide with other content, sending them back`, true];
    case "page": return ["Page", `Page ${e.page} done · score ${e.score}`];
    case "refine": return ["Refiner", `Page ${e.page}, round ${e.iteration}: revising ${e.blocks.length} block(s) that failed review`];
    case "escalate": return ["Escalation", `Page ${e.page}: ${e.blocks.length} block(s) did not converge, escalating to the stronger model with the page image`];
    case "tool": return e.calls ? ["Tool", `${e.agent} called check_fit ${e.calls}×`] : null;
    case "redo": return ["Redo", `${e.direct} block(s) changed directly, ${e.agent} sent to the Refiner, ${e.strong} to the stronger model, ${e.shrink} allowed to shrink`];
    case "log": return ["Info", e.message];
    case "warning": return ["Warning", e.message, true];
    case "trace":
      if (e.span === "agent" || e.span === "function" || (e.span === "guardrail" && e.triggered)) {
        const what = e.span === "agent" ? `${e.name} finished` : e.span === "function" ? `tool ${e.name}` : `guardrail ${e.name} tripped, retrying`;
        return ["Trace", `${what}${e.ms != null ? ` · ${Math.round(e.ms)} ms` : ""}${e.error ? " · " + e.error : ""}`, !!e.error || e.span === "guardrail"];
      }
      return null;
    case "error": return ["Error", e.message, true];
    case "done": return ["Done", `Score ${e.report.score}`];
  }
  return null;
}

function logLine(ul, e, d) {
  const li = document.createElement("li");
  if (d[2]) li.className = "warn";
  li.innerHTML = `<span class="t">${Number(e.t || 0).toFixed(1)}s</span><span class="k">${esc(d[0])}</span><span>${esc(d[1])}</span>`;
  ul.append(li);
}

function onEvent(e) {
  state.events.push(e);
  const d = describe(e);
  if (d) { logLine($("#log"), e, d); $("#log").scrollTop = $("#log").scrollHeight; $("#activity-count").textContent = `(${$("#log").children.length})`; }
  switch (e.type) {
    case "stage":
      setStage(e.stage, e.percent, e.message);
      if (e.stage === "translate") $$(".pg").forEach((p) => p.classList.add("work"));
      break;
    case "extracted": {
      state.pages = e.pages;
      $("#pages").innerHTML = Array.from({ length: e.pages }, (_, i) => `<div class="pg" id="pg-${i + 1}">${i + 1}</div>`).join("");
      break;
    }
    case "page": {
      const pg = $(`#pg-${e.page}`);
      if (pg) { pg.className = `pg ${cls(e.score)}`; pg.title = `Page ${e.page}: ${e.score}`; }
      setStage("translate", e.percent, `Page ${e.page} done · score ${e.score}`);
      break;
    }
    case "error":
      $("#job-error").hidden = false;
      $("#job-error").innerHTML = `<b>We could not finish this translation.</b><br>${esc(e.message)}` +
        (state.job && state.job.version > 1 ? `<br><a href="#job=${state.job.id}&v=${state.job.version - 1}">Open the previous version</a>` : "");
      break;
    case "done": setStage("done", 100, "Translation ready"); setTimeout(() => resume(state.job.id), 300); break;
  }
}

// ------------------------------------------------------------------ result
function showResult(job) {
  state.result = job.result;
  state.version = job.shown_version || job.version;
  state.events = job.events || [];
  state.decisions = {};
  const r = job.result.report;
  const sc = $("#score");
  sc.textContent = Math.round(r.score);
  sc.className = `score ${cls(r.score)}`;
  $("#subscores").innerHTML = `<span class="${cls(r.translation_score)}">translation ${Math.round(r.translation_score)}</span>` +
    `<span class="${cls(r.layout_score)}">layout ${Math.round(r.layout_score)}</span>` +
    `<span class="muted">${r.purpose === "understand" ? "draft" : "publish"}${r.backend === "pseudo" ? " · demo (pseudo)" : ""}</span>`;
  $("#reasons").innerHTML = r.passed ? `<span class="ok">Passed</span>` :
    r.fail_reasons.map((x) => `<span class="bad">${esc(x)}</span>`).join("");
  $("#download").href = `/api/jobs/${job.id}/download?version=${state.version}`;
  const lang = state.config.languages.find((l) => l.code === job.target);
  $("#res-file").textContent = `${job.filename} → ${lang ? lang.name : job.target}`;
  $("#versions").innerHTML = (job.versions || []).length > 1 ? "Version: " + job.versions.map((v) =>
    `<a href="#job=${job.id}&v=${v.version}" class="${v.version === state.version ? "current" : ""}">v${v.version}${v.score != null ? ` (${Math.round(v.score)})` : " (failed)"}</a>`).join(" ") : "";
  state.page = 1;
  state.pages = r.pages.length;
  renderPage();
  renderAnalysis(job.result.analysis);
  renderBlocks();
  renderStats(job);
  renderRunLog();
  renderReview();
  show("result");
}

function renderPage() {
  if (!state.job || !state.result) return;
  const id = state.job.id, v = state.version;
  $("#page-label").textContent = `Page ${state.page} of ${state.pages}`;
  $("#img-orig").src = `/api/jobs/${id}/pages/${state.page}.png?which=original&dpi=90`;
  $("#img-tr").src = `/api/jobs/${id}/pages/${state.page}.png?which=translated&version=${v}&dpi=90`;
  $("#prev").disabled = state.page <= 1;
  $("#next").disabled = state.page >= state.pages;
  renderOverlay();
}
$("#prev").onclick = () => { state.page--; renderPage(); };
$("#next").onclick = () => { state.page++; renderPage(); };
$("#outline").onchange = renderOverlay;

function renderOverlay() {
  const ov = $("#overlay");
  ov.innerHTML = "";
  if (!$("#outline").checked || !state.result) return;
  const pr = state.result.report.pages[state.page - 1];
  if (!pr || !pr.width) return;
  const blocks = state.result.blocks.filter((b) => b.page === state.page && b.issues.length && b.fit.rect);
  // percentage geometry through SVG attributes (style attributes are blocked by the CSP)
  ov.innerHTML = `<svg viewBox="0 0 ${pr.width} ${pr.height}" preserveAspectRatio="none">` + blocks.map((b) => {
    const [x0, y0, x1, y1] = b.fit.rect;
    const crit = b.issues.some((i) => i.severity === "critical");
    return `<rect data-id="${esc(b.id)}" x="${x0}" y="${y0}" width="${x1 - x0}" height="${y1 - y0}" class="${crit ? "crit" : "warn"}"><title>${esc(b.id)}: ${esc(b.issues.map((i) => i.message).join(" · "))}</title></rect>`;
  }).join("") + "</svg>";
  ov.querySelectorAll("rect").forEach((r) => (r.onclick = () => jumpToBlock(r.dataset.id)));
}

function jumpToBlock(id) {
  $("#only-issues").checked = false;
  renderBlocks();
  openTab("main", "details"); openTab("details", "refinement");
  const el = document.getElementById("blk-" + id);
  if (el) { el.scrollIntoView({ block: "center" }); el.classList.add("flash"); setTimeout(() => el.classList.remove("flash"), 1500); }
}

function renderAnalysis(a) {
  const terms = a.protected_terms.map((t) => `<span class="term" title="${esc(t.kind)}: ${esc(t.reason)}">${esc(t.term)}</span>`).join("") || "<span class='muted'>none</span>";
  const jargon = a.jargon.length
    ? `<table><tr><th>Term</th><th>Rendered as</th><th>Note</th></tr>${a.jargon.map((j) => `<tr><td>${esc(j.term)}</td><td>${esc(j.translation)}</td><td class="muted">${esc(j.note)}</td></tr>`).join("")}</table>`
    : "<span class='muted'>none detected</span>";
  $("#analysis").innerHTML = `
    <dl class="kv">
      <dt>Source language</dt><dd>${esc(a.source_language_name)} (${esc(a.source_language)})</dd>
      <dt>Tone</dt><dd>${esc(a.tone)}</dd><dt>Domain</dt><dd>${esc(a.domain)}</dd>
      <dt>Summary</dt><dd>${esc(a.summary)}</dd><dt>Style</dt><dd>${esc(a.style_notes)}</dd>
      <dt>Never translated</dt><dd class="terms">${terms}</dd>
    </dl>
    <h3>Jargon glossary used by every translator</h3>${jargon}
    <p class="muted">Change these decisions in the Review tab.</p>`;
}

function renderBlocks() {
  const only = $("#only-issues").checked;
  const blocks = state.result.blocks.filter((b) => !only || b.issues.length || b.attempts.length > 1);
  if (!blocks.length) { $("#blocks").innerHTML = "<p class='muted'>Every block passed on the first try.</p>"; return; }
  $("#blocks").innerHTML = blocks.map((b) => `
    <div class="block" id="blk-${esc(b.id)}">
      <div class="block-head"><span>${esc(b.id)} · page ${b.page} · ${esc(b.fit.status)}${b.fit.scale < 1 ? ` ${Math.round(b.fit.scale * 100)}%` : ""} · ${esc(b.font)}</span>
        <span><span class="${cls(b.translation_score)}">translation ${Math.round(b.translation_score)}</span> · <span class="${cls(b.layout_score)}">layout ${Math.round(b.layout_score)}</span></span></div>
      <div class="src">${markup(b.source)}</div>
      <div class="tr">${markup(b.translation)}</div>
      ${b.issues.length ? `<ul class="issues">${b.issues.map((i) => `<li><span class="sev ${i.severity}">${i.severity}</span>${esc(i.message)}${i.suggestion ? ` <span class="muted">→ ${esc(i.suggestion)}</span>` : ""} <span class="muted">(${i.origin})</span></li>`).join("")}</ul>` : ""}
      ${b.attempts.length > 1 ? `<div class="attempts">History: ${b.attempts.map((a) => `${a.stage} ${Math.round(a.score)}`).join(" → ")}</div>` : ""}
    </div>`).join("");
}
$("#only-issues").onchange = renderBlocks;

// ------------------------------------------------------------------ stats
function renderStats(job) {
  const res = job.result, r = res.report, calls = res.calls || [];
  const sum = (xs, f) => xs.reduce((a, x) => a + f(x), 0);
  const tok = (c) => c.input_tokens + c.output_tokens;
  const totalTok = sum(calls, tok), totalCost = sum(calls, (c) => c.cost_usd);
  const byStage = {};
  for (const c of calls) { byStage[c.stage] = byStage[c.stage] || { tokens: 0, cost: 0, requests: 0 }; byStage[c.stage].tokens += tok(c); byStage[c.stage].cost += c.cost_usd; byStage[c.stage].requests += c.requests; }
  const evaluation = (byStage.judge || {}).tokens || 0;
  const backforth = ((byStage.refine || {}).tokens || 0) + ((byStage.escalate || {}).tokens || 0) + ((byStage.redo || {}).tokens || 0);
  const pages = r.pages.map((p) => ({ label: String(p.page), parts: {} }));
  for (const c of calls) { if (c.page && pages[c.page - 1]) pages[c.page - 1].parts[c.stage] = (pages[c.page - 1].parts[c.stage] || 0) + tok(c); }
  const byAgent = {};
  for (const c of calls) { const k = `${c.agent}|${c.model}`; byAgent[k] = byAgent[k] || { requests: 0, in: 0, out: 0, cost: 0, s: 0 }; const a = byAgent[k]; a.requests += c.requests; a.in += c.input_tokens; a.out += c.output_tokens; a.cost += c.cost_usd; a.s += c.seconds; }
  const g = res.glyphs || {}, fonts = Object.entries(g.fonts || {});
  const timeline = [];
  let prev = null;
  for (const e of state.events) { if (e.type === "stage") { if (prev) prev.end = e.t; prev = { label: e.stage, start: e.t, end: e.t, key: e.stage === "translate" ? "translate" : e.stage === "analyze" ? "analysis" : "other" }; timeline.push(prev); } }
  if (prev) prev.end = Math.max(prev.end, r.timings.total || prev.end);
  const versions = (job.versions || []).filter((v) => v.score != null);
  $("#stats").innerHTML = `
    <div class="stat-row">
      <div class="stat"><b>${fmtTokens(totalTok)}</b><span>tokens this version</span></div>
      <div class="stat"><b>${fmtUsd(totalCost)}</b><span>estimated cost (list prices)</span></div>
      <div class="stat"><b>${fmtTokens(evaluation)}</b><span>evaluation (judge)</span></div>
      <div class="stat"><b>${fmtTokens(backforth)}</b><span>back and forth (refine, escalate, redo)</span></div>
      <div class="stat"><b>${g.glyphs_built || 0}</b><span>glyphs built · ${(g.ms || 0).toFixed(0)} ms · 0 tokens</span></div>
      <div class="stat"><b>${(r.timings.total || 0).toFixed(0)} s</b><span>total time</span></div>
    </div>
    <h3>Tokens by stage</h3>
    ${Charts.stacked(Object.entries(byStage).map(([k, v]) => ({ label: k, parts: { [k]: v.tokens } })))}
    <h3>Tokens per page</h3>
    ${pages.length ? Charts.columns(pages) : ""}
    <p class="muted">Document-level calls (analysis) are not attributed to a page.</p>
    <h3>Agents and models</h3>
    <table><tr><th>Agent</th><th>Model</th><th>Requests</th><th>Input</th><th>Output</th><th>Cost</th><th>Time</th></tr>
      ${Object.entries(byAgent).map(([k, a]) => { const [agent, model] = k.split("|"); return `<tr><td>${esc(agent)}</td><td>${esc(model)}</td><td>${a.requests}</td><td>${fmtTokens(a.in)}</td><td>${fmtTokens(a.out)}</td><td>${fmtUsd(a.cost)}</td><td>${a.s.toFixed(1)} s</td></tr>`; }).join("")}
      <tr class="total"><td>Total</td><td></td><td>${sum(calls, (c) => c.requests)}</td><td>${fmtTokens(sum(calls, (c) => c.input_tokens))}</td><td>${fmtTokens(sum(calls, (c) => c.output_tokens))}</td><td>${fmtUsd(totalCost)}</td><td></td></tr></table>
    <h3>Work without tokens</h3>
    <table><tr><th>Step</th><th>Detail</th><th>Time</th></tr>
      <tr><td>Glyph builder</td><td>${fonts.filter(([, f]) => f.built || f.substitute).map(([n, f]) => `${esc(n)}: ${f.built ? "built " + esc(f.built) : ""}${f.substitute ? " → " + esc(f.substitute) : ""}`).join("<br>") || "nothing needed"}</td><td>${(g.ms || 0).toFixed(0)} ms</td></tr>
      <tr><td>Layout QA</td><td>collisions ${r.collisions}, overlaps read back from the PDF ${r.output_overlaps}, font fallbacks ${r.missing_glyph_blocks + r.font_substituted_blocks}</td><td>${(res.counters.layout_qa_s || 0).toFixed(1)} s</td></tr>
      <tr><td>Extraction / OCR</td><td>${r.pages.filter((p) => p.ocr).length} scanned page(s)</td><td>${(r.timings.extract || 0).toFixed(1)} s</td></tr>
      <tr><td>Render</td><td>${r.blocks_total} blocks</td><td>${(r.timings.render || 0).toFixed(1)} s</td></tr>
      <tr><td>Review preparation</td><td>similar-font ranking</td><td>${(res.counters.review_s || 0).toFixed(1)} s</td></tr></table>
    <h3>Timeline</h3>${Charts.timeline(timeline)}
    ${versions.length > 1 ? `<h3>Versions</h3><table><tr><th>Version</th><th>Score</th><th>Tokens</th><th>Cost</th></tr>${versions.map((v) => `<tr><td>v${v.version}</td><td>${Math.round(v.score)}</td><td>${fmtTokens(v.tokens)}</td><td>${fmtUsd(v.cost_usd)}</td></tr>`).join("")}<tr class="total"><td>All versions</td><td></td><td>${fmtTokens(sum(versions, (v) => v.tokens))}</td><td>${fmtUsd(sum(versions, (v) => v.cost_usd))}</td></tr></table>` : ""}
    <p class="muted">${r.backend === "openai" ? "The full trace is also in the OpenAI dashboard (Traces), grouped by job id." : "Demo mode: token counts are estimated by the local pseudo model."}</p>`;
}

// ------------------------------------------------------------------ run log
const LOG_KINDS = ["Stage", "Analyst", "Page", "Layout", "Refiner", "Escalation", "Tool", "Trace", "Redo", "Warning", "Error"];
function renderRunLog() {
  $("#log-filters").innerHTML = LOG_KINDS.map((k) => `<button class="chip ${state.logFilter.has(k) ? "" : "on"}" data-k="${k}">${k}</button>`).join("");
  $$("#log-filters .chip").forEach((c) => (c.onclick = () => { const k = c.dataset.k; state.logFilter.has(k) ? state.logFilter.delete(k) : state.logFilter.add(k); renderRunLog(); }));
  const ul = $("#runlog");
  ul.innerHTML = "";
  for (const e of state.events) { const d = describe(e); if (d && !state.logFilter.has(d[0])) logLine(ul, e, d); }
  if (!ul.children.length) ul.innerHTML = "<li class='muted'>No events stored for this version.</li>";
}

// ------------------------------------------------------------------ review
function renderReview() {
  const items = state.result.review || [];
  const actionable = items.filter((i) => i.kind !== "terms");
  const badge = $("#review-badge");
  badge.hidden = !actionable.length;
  badge.textContent = actionable.length;
  badge.className = "badge " + (actionable.some((i) => i.severity === "critical") ? "crit" : "");
  const blocks = Object.fromEntries(state.result.blocks.map((b) => [b.id, b]));
  const pagesTxt = (p) => p.length ? `pages ${p.slice(0, 12).join(", ")}${p.length > 12 ? "…" : ""}` : "";
  $("#review").innerHTML = items.map((it, n) => {
    if (it.kind === "terms") {
      const jr = it.jargon.map((j) => `<tr><td>${esc(j.term)}</td><td>${esc(j.translation)}</td><td><label class="check inline"><input type="checkbox" data-term-keep="${esc(j.term)}"> keep in the original language</label></td></tr>`).join("");
      const pr = it.protected.map((t) => `<label class="term toggle"><input type="checkbox" data-term-translate="${esc(t)}"> ${esc(t)}</label>`).join(" ");
      return `<div class="review-item info"><h4>${esc(it.title)}</h4>
        ${jr ? `<table><tr><th>Jargon</th><th>Translated as</th><th></th></tr>${jr}</table>` : "<p class='muted'>No jargon detected.</p>"}
        <p class="muted">Protected (never translated). Tick one to translate it after all:</p><div class="terms">${pr || "<span class='muted'>none</span>"}</div></div>`;
    }
    const opts = it.options.map((o, k) => `<label class="option ${o.preview ? "with-preview" : ""}"><input type="radio" name="opt-${n}" value="${esc(o.id)}" ${o.id === it.default ? "checked" : ""}>
      <span>${esc(o.label)}${o.tokens ? ' <span class="pill">uses tokens</span>' : ' <span class="pill free">no tokens</span>'}${o.score != null ? ` <span class="muted">similarity ${(100 - o.score * 100).toFixed(0)}%</span>` : ""}</span>
      ${o.preview ? `<img src="${o.preview}" alt="">` : ""}</label>`).join("");
    const list = (it.blocks || []).slice(0, 30).map((id) => `<a href="#" data-jump="${esc(id)}">${esc(id)}</a>`).join(" ");
    const edits = it.kind === "quality" ? `<div class="edits" data-edits="${n}" hidden>${it.blocks.map((id) => `<label>${esc(id)}<textarea data-edit="${esc(id)}">${esc(blocks[id] ? blocks[id].translation : "")}</textarea></label>`).join("")}</div>` : "";
    const extra = it.kind === "font" && it.built_automatically ? `<p class="muted">Already built automatically from the font's own pieces: ${esc(it.built_automatically)} (0 tokens).</p>` : "";
    return `<div class="review-item ${it.severity}" data-item="${n}"><h4>${esc(it.title)}</h4>
      <p class="muted">${pagesTxt(it.pages || [])} · blocks: ${list}${(it.blocks || []).length > 30 ? " …" : ""}</p>${extra}
      <div class="options">${opts}</div>${edits}</div>`;
  }).join("") || "<p>Nothing to review: everything passed.</p>";
  $$("[data-jump]").forEach((a) => (a.onclick = (e) => { e.preventDefault(); jumpToBlock(a.dataset.jump); }));
  $$("#review input, #review textarea").forEach((el) => el.addEventListener("change", () => { toggleEdits(); estimate(); }));
  toggleEdits();
  $("#redo").disabled = !items.length;
  estimate();
}

function toggleEdits() {
  (state.result.review || []).forEach((it, n) => {
    if (it.kind !== "quality") return;
    const v = ($(`input[name="opt-${n}"]:checked`) || {}).value;
    const box = $(`[data-edits="${n}"]`);
    if (box) box.hidden = v !== "edit";
  });
}

function collectDecisions() {
  const d = {};
  (state.result.review || []).forEach((it, n) => {
    const v = ($(`input[name="opt-${n}"]:checked`) || {}).value;
    if (it.kind === "collision" || it.kind === "layout") d[it.kind] = { action: v, blocks: it.blocks };
    if (it.kind === "font") { d.fonts = d.fonts || {}; d.fonts[it.font] = { action: v }; }
    if (it.kind === "quality") {
      d.quality = {};
      for (const id of it.blocks) {
        const ta = $(`textarea[data-edit="${CSS.escape(id)}"]`);
        d.quality[id] = v === "edit" ? { action: "edit", text: ta ? ta.value : "" } : { action: v };
      }
    }
  });
  d.terms = { keep_original: $$("[data-term-keep]:checked").map((x) => x.dataset.termKeep),
    translate: $$("[data-term-translate]:checked").map((x) => x.dataset.termTranslate) };
  return d;
}

let estTimer = null;
function estimate() {
  clearTimeout(estTimer);
  estTimer = setTimeout(async () => {
    try {
      const e = await postJson(`/api/jobs/${state.job.id}/estimate`, collectDecisions());
      $("#estimate").textContent = e.tokens ? `Estimated: ~${fmtTokens(e.tokens)} tokens (~${fmtUsd(e.cost_usd)}, ${e.model})` : "Estimated: no tokens (re-render only)";
    } catch { $("#estimate").textContent = ""; }
  }, 250);
}

$("#redo").onclick = async () => {
  $("#redo").disabled = true;
  try {
    const job = await postJson(`/api/jobs/${state.job.id}/redo`, collectDecisions());
    history.replaceState(null, "", "#job=" + job.id);
    follow(job);
  } catch (err) { alert(err.message); $("#redo").disabled = false; }
};

// ------------------------------------------------------------------ history
async function loadHistory() {
  show("history");
  const { offset, limit } = state.history;
  const data = await api(`/api/jobs?limit=${limit}&offset=${offset}`);
  const rows = data.jobs.map((j) => `<tr>
      <td>${esc(fmtDate(j.created))}</td><td class="fname">${esc(j.filename)}</td><td>${esc(j.target)}</td>
      <td>${j.purpose === "understand" ? "draft" : "publish"}</td><td>${j.versions || 0}</td>
      <td>${j.score != null ? `<b class="${cls(j.score)}">${Math.round(j.score)}</b> <span class="muted">${Math.round(j.translation_score)}/${Math.round(j.layout_score)}</span>` : "–"}</td>
      <td>${j.pages ?? "–"}</td><td>${fmtTokens(j.tokens || 0)}</td><td>${fmtUsd(j.cost_usd || 0)}</td>
      <td><span class="status ${j.status}">${esc(j.status)}</span></td>
      <td class="actions">${j.status === "done" || j.versions ? `<a href="#job=${j.id}">open</a> <a href="/api/jobs/${j.id}/download">download</a>` : j.status === "running" || j.status === "queued" ? `<a href="#job=${j.id}">follow</a>` : ""}
        <a href="#" data-pin="${j.id}" data-pinned="${j.pinned}">${j.pinned ? "unpin" : "pin"}</a>
        ${j.status !== "running" && j.status !== "queued" ? `<a href="#" data-del="${j.id}" class="bad">delete</a>` : ""}</td></tr>`).join("");
  $("#history-table").innerHTML = `<tr><th>Date</th><th>File</th><th>Target</th><th>Purpose</th><th>Versions</th><th>Score <span class="muted">tr/layout</span></th><th>Pages</th><th>Tokens</th><th>Cost</th><th>Status</th><th></th></tr>` +
    (rows || `<tr><td colspan="11" class="muted">No runs yet.</td></tr>`);
  $("#h-label").textContent = data.total ? `${offset + 1}–${Math.min(offset + limit, data.total)} of ${data.total}` : "";
  $("#h-prev").disabled = offset === 0;
  $("#h-next").disabled = offset + limit >= data.total;
  $$("[data-del]").forEach((a) => (a.onclick = async (e) => { e.preventDefault(); if (confirm("Delete this run and its files?")) { await api(`/api/jobs/${a.dataset.del}`, { method: "DELETE" }); loadHistory(); } }));
  $$("[data-pin]").forEach((a) => (a.onclick = async (e) => { e.preventDefault(); await api(`/api/jobs/${a.dataset.pin}/pin?pinned=${a.dataset.pinned === "1" ? "false" : "true"}`, { method: "POST" }); loadHistory(); }));
}
$("#h-prev").onclick = () => { state.history.offset = Math.max(0, state.history.offset - state.history.limit); loadHistory(); };
$("#h-next").onclick = () => { state.history.offset += state.history.limit; loadHistory(); };

init().catch((err) => formError(err.message));
