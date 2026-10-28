"use strict";
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const markup = (s) => esc(s).replace(/&lt;(\/?)(b|i)&gt;/g, "<$1$2>"); // the only tags we allow back
const STAGES = ["validate", "extract", "analyze", "translate", "render", "done"];
const LABEL = { stage: "Stage", analysis: "Analyst", refine: "Refiner", escalate: "Escalation", tool: "Tool",
  page: "Page", warning: "Warning", extracted: "Extract", trace: "Trace", error: "Error", done: "Done" };
const state = { config: null, file: null, job: null, pages: 0, page: 1, result: null, events: [] };

const cls = (score) => (score >= 85 ? "ok" : score >= 65 ? "mid" : "bad");
const fmtTokens = (n) => (n > 9999 ? (n / 1000).toFixed(1) + "k" : String(n));

async function api(path, opts) {
  const res = await fetch(path, opts);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || `Request failed (${res.status})`);
  return body;
}

function show(id) {
  for (const s of ["upload", "progress", "result"]) $("#" + s).hidden = s !== id;
}

// ------------------------------------------------------------------ upload
async function init() {
  state.config = await api("/api/config");
  const mode = $("#mode");
  if (state.config.backend === "openai") {
    mode.textContent = `Live · ${state.config.models.translator}`;
    mode.className = "mode live";
  } else {
    mode.textContent = "Demo mode · pseudo-translation (no API key)";
    mode.className = "mode pseudo";
  }
  const sel = $("#target");
  for (const l of state.config.languages) sel.add(new Option(`${l.name} · ${l.native}`, l.code));
  sel.value = localStorage.getItem("target") || "es";
  $("#drop-hint").textContent = `Up to ${state.config.limits.max_upload_mb} MB and ${state.config.limits.max_pages} pages`;
  const samples = await api("/api/samples").catch(() => []);
  for (const s of samples) {
    const b = document.createElement("button");
    b.className = "chip";
    b.type = "button";
    b.title = s.description;
    b.innerHTML = `${esc(s.name.replace(/\.pdf$/, ""))}<small>${s.pages} p.</small>`;
    b.onclick = () => start({ sample: s.name, label: s.name });
    $("#samples").append(b);
  }
  const m = location.hash.match(/job=([0-9a-f]{32})/);
  if (m) resume(m[1]);
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
$("#form").addEventListener("submit", (e) => { e.preventDefault(); if (state.file) start({ file: state.file, label: state.file.name }); });
for (const id of ["#new-1", "#new-2"]) $(id).onclick = reset;

function reset() {
  history.replaceState(null, "", "/");
  Object.assign(state, { file: null, job: null, result: null, events: [] });
  $("#drop").classList.remove("has-file");
  $("#drop-text").innerHTML = "<b>Drop a PDF here</b> or click to choose a file";
  $("#go").disabled = true;
  $("#file").value = "";
  show("upload");
}

async function start({ file, sample, label }) {
  const fd = new FormData();
  fd.append("target", $("#target").value);
  if (file) fd.append("file", file);
  if (sample) fd.append("sample", sample);
  $("#go").disabled = true;
  try {
    const job = await api("/api/jobs", { method: "POST", body: fd });
    history.replaceState(null, "", "#job=" + job.id);
    follow(job);
  } catch (err) {
    formError(err.message);
    $("#go").disabled = !state.file;
  }
  void label;
}

async function resume(id) {
  try { follow(await api(`/api/jobs/${id}`)); } catch { reset(); }
}

// ------------------------------------------------------------------ progress
function follow(job) {
  state.job = job;
  state.events = [];
  const lang = state.config.languages.find((l) => l.code === job.target);
  $("#job-title").textContent = job.filename;
  $("#job-sub").textContent = `→ ${lang ? lang.name : job.target}`;
  $("#log").innerHTML = "";
  $("#pages").innerHTML = "";
  $("#job-error").hidden = true;
  $("#bar-fill").style.width = "0";
  show("progress");
  const es = new EventSource(`/api/jobs/${job.id}/events`);
  es.onmessage = (m) => onEvent(JSON.parse(m.data));
  es.addEventListener("end", () => es.close());
  es.onerror = () => { es.close(); setTimeout(() => resume(job.id), 1500); };
}

function setStage(stage, percent, message) {
  const idx = STAGES.indexOf(stage);
  document.querySelectorAll("#steps li").forEach((li, i) => {
    li.classList.toggle("done", idx >= 0 && i < idx);
    li.classList.toggle("active", i === idx);
  });
  if (percent != null) $("#bar-fill").style.width = percent + "%";
  if (message) $("#stage-msg").textContent = message;
}

function log(kind, text, warn) {
  const li = document.createElement("li");
  if (warn) li.className = "warn";
  const t = state.events.length ? state.events[state.events.length - 1].t : 0;
  li.innerHTML = `<span class="t">${Number(t).toFixed(1)}s</span><span class="k">${esc(kind)}</span><span>${esc(text)}</span>`;
  const ul = $("#log");
  ul.append(li);
  ul.scrollTop = ul.scrollHeight;
  $("#activity-count").textContent = `(${ul.children.length})`;
}

function onEvent(e) {
  state.events.push(e);
  switch (e.type) {
    case "stage":
      setStage(e.stage, e.percent, e.message);
      if (e.stage !== "done") log("Stage", e.message);
      if (e.stage === "translate") document.querySelectorAll(".pg").forEach((p) => p.classList.add("work"));
      break;
    case "extracted": {
      state.pages = e.pages;
      const box = $("#pages");
      box.innerHTML = "";
      for (let i = 1; i <= e.pages; i++) box.insertAdjacentHTML("beforeend", `<div class="pg" id="pg-${i}">${i}</div>`);
      log("Extract", `${e.blocks} text blocks on ${e.pages} pages` + (e.scanned_pages.length ? `, OCR on pages ${e.scanned_pages.join(", ")}` : ""));
      break;
    }
    case "analysis": {
      const a = e.analysis;
      log("Analyst", `${a.source_language_name} · ${a.tone} · ${a.domain} · ${a.protected_terms.length} protected terms, ${a.jargon.length} jargon terms`);
      break;
    }
    case "page": {
      const pg = $(`#pg-${e.page}`);
      if (pg) { pg.className = `pg ${cls(e.score)}`; pg.title = `Page ${e.page}: ${e.score}`; }
      setStage("translate", e.percent, `Page ${e.page} done · score ${e.score}`);
      break;
    }
    case "page_step":
      log("Page", e.step === "translated" ? `Page ${e.page}: ${e.blocks} blocks translated, reviewing layout and quality`
        : `Page ${e.page}: review done, ${e.failing ? e.failing + " block(s) need another pass" : "all blocks pass"}`);
      break;
    case "refine": log("Refiner", `Page ${e.page}, round ${e.iteration}: revising ${e.blocks.length} block(s) that failed review`); break;
    case "escalate": log("Escalation", `Page ${e.page}: ${e.blocks.length} block(s) did not converge, escalating to the stronger model with the page image`); break;
    case "tool": if (e.calls) log("Tool", `${e.agent} called check_fit ${e.calls}×`); break;
    case "warning": log("Warning", e.message, true); break;
    case "trace":
      if (e.span === "agent" || e.span === "function" || (e.span === "guardrail" && e.triggered)) {
        const what = e.span === "agent" ? `${e.name} finished` : e.span === "function" ? `tool ${e.name}` : `guardrail ${e.name} tripped, retrying`;
        log("Trace", `${what}${e.ms != null ? ` · ${Math.round(e.ms)} ms` : ""}${e.error ? " · " + e.error : ""}`, !!e.error || e.span === "guardrail");
      }
      break;
    case "error":
      setStage(null, null, "");
      $("#job-error").hidden = false;
      $("#job-error").innerHTML = `<b>We could not translate this file.</b><br>${esc(e.message)}`;
      break;
    case "done":
      setStage("done", 100, "Translation ready");
      loadResult();
      break;
  }
}

// ------------------------------------------------------------------ result
async function loadResult() {
  const job = await api(`/api/jobs/${state.job.id}`);
  state.result = job.result;
  const r = job.result.report;
  const sc = $("#score");
  sc.textContent = Math.round(r.score);
  sc.className = `score ${cls(r.score)}`;
  $("#download").href = `/api/jobs/${job.id}/download`;
  const tokens = Object.values(r.usage_by_agent).reduce((a, u) => a + u.input_tokens + u.output_tokens, 0);
  const stats = [
    [r.blocks_total, "text blocks"],
    [r.blocks_refined, "revised by the loop"],
    [r.blocks_escalated, "escalated"],
    [r.overflow_blocks, "overflowing their box"],
    [r.shrunk_blocks, "slightly shrunk to fit"],
    [Math.round(r.font_preserved_ratio * 100) + "%", "original fonts kept"],
    [r.images_preserved ? "Yes" : "Moved", "images in place"],
    [Math.round(r.judge_coverage * 100) + "%", "reviewed by the judge"],
    [r.timings.total.toFixed(0) + " s", "total time"],
    [fmtTokens(tokens), "tokens"],
  ];
  $("#stats").innerHTML = stats.map(([v, l]) => `<div class="stat"><b>${esc(v)}</b><span>${esc(l)}</span></div>`).join("");
  const warns = state.events.filter((e) => e.type === "warning").map((e) => e.message);
  if (r.backend === "pseudo") warns.unshift("Demo mode: this is a pseudo-translation made locally to exercise the layout engine. Set OPENAI_API_KEY for real translations.");
  if (!r.passed) warns.unshift(`Some blocks stayed below the quality threshold (${r.threshold}). Check “Blocks & issues” before using this document.`);
  $("#warnings").innerHTML = [...new Set(warns)].slice(0, 6).map((w) => `<div class="warning">${esc(w)}</div>`).join("");
  state.page = 1;
  state.pages = r.pages.length;
  renderPage();
  renderAnalysis(job.result.analysis);
  renderBlocks();
  renderTrace(r);
  show("result");
}

function renderPage() {
  const id = state.job.id;
  $("#page-label").textContent = `Page ${state.page} of ${state.pages}`;
  $("#img-orig").src = `/api/jobs/${id}/pages/${state.page}.png?version=original&dpi=90`;
  $("#img-tr").src = `/api/jobs/${id}/pages/${state.page}.png?version=translated&dpi=90`;
  $("#prev").disabled = state.page <= 1;
  $("#next").disabled = state.page >= state.pages;
}
$("#prev").onclick = () => { state.page--; renderPage(); };
$("#next").onclick = () => { state.page++; renderPage(); };

function renderAnalysis(a) {
  const terms = a.protected_terms.map((t) => `<span class="term" title="${esc(t.kind)}: ${esc(t.reason)}">${esc(t.term)}</span>`).join("") || "<span class='muted'>none</span>";
  const jargon = a.jargon.length
    ? `<table><tr><th>Term</th><th>Rendered as</th><th>Note</th></tr>${a.jargon.map((j) => `<tr><td>${esc(j.term)}</td><td>${esc(j.translation)}</td><td class="muted">${esc(j.note)}</td></tr>`).join("")}</table>`
    : "<span class='muted'>none detected</span>";
  $("#analysis").innerHTML = `
    <dl class="kv">
      <dt>Source language</dt><dd>${esc(a.source_language_name)} (${esc(a.source_language)})</dd>
      <dt>Tone</dt><dd>${esc(a.tone)}</dd>
      <dt>Domain</dt><dd>${esc(a.domain)}</dd>
      <dt>Summary</dt><dd>${esc(a.summary)}</dd>
      <dt>Style</dt><dd>${esc(a.style_notes)}</dd>
      <dt>Never translated</dt><dd class="terms">${terms}</dd>
    </dl>
    <h3>Jargon glossary used by every translator</h3>${jargon}`;
}

function renderBlocks() {
  const only = $("#only-issues").checked;
  const blocks = state.result.blocks.filter((b) => !only || b.issues.length || b.attempts.length > 1);
  if (!blocks.length) { $("#blocks").innerHTML = "<p class='muted'>Every block passed on the first try.</p>"; return; }
  $("#blocks").innerHTML = blocks.map((b) => `
    <div class="block">
      <div class="block-head"><span>${esc(b.id)} · page ${b.page} · ${esc(b.fit.status)}${b.fit.scale < 1 ? ` ${Math.round(b.fit.scale * 100)}%` : ""}</span>
        <span class="${cls(b.score)}">score ${Math.round(b.score)}${b.converged ? "" : " · below threshold"}</span></div>
      <div class="src">${markup(b.source)}</div>
      <div class="tr">${markup(b.translation)}</div>
      ${b.issues.length ? `<ul class="issues">${b.issues.map((i) => `<li><span class="sev ${i.severity}">${i.severity}</span>${esc(i.message)}${i.suggestion ? ` <span class="muted">→ ${esc(i.suggestion)}</span>` : ""} <span class="muted">(${i.origin})</span></li>`).join("")}</ul>` : ""}
      ${b.attempts.length > 1 ? `<div class="attempts">History: ${b.attempts.map((a) => `${a.stage} ${Math.round(a.score)}`).join(" → ")}</div>` : ""}
    </div>`).join("");
}
$("#only-issues").onchange = renderBlocks;

function renderTrace(r) {
  const rows = Object.entries(r.usage_by_agent).map(([name, u]) => `<tr><td>${esc(name)}</td><td>${u.requests}</td><td>${fmtTokens(u.input_tokens)}</td><td>${fmtTokens(u.output_tokens)}</td></tr>`).join("");
  const models = Object.entries(r.models).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");
  const timings = Object.entries(r.timings).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v.toFixed(2)} s</dd>`).join("");
  const spans = state.events.filter((e) => e.type === "trace");
  $("#trace").innerHTML = `
    <table><tr><th>Agent</th><th>Requests</th><th>Input tokens</th><th>Output tokens</th></tr>${rows}</table>
    <h3>Models</h3><dl class="kv">${models}</dl>
    <h3>Timings</h3><dl class="kv">${timings}</dl>
    <h3>Trace</h3><p class="muted">${spans.length} spans recorded (agent runs, model calls, tool calls, guardrails).
      ${r.backend === "openai" ? "The full trace is also in the OpenAI dashboard under Traces, grouped by job id." : ""}</p>`;
}

document.querySelectorAll(".tab").forEach((t) => (t.onclick = () => {
  document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("active", x === t));
  document.querySelectorAll(".tab-body").forEach((b) => (b.hidden = b.dataset.body !== t.dataset.tab));
}));

init().catch((err) => formError(err.message));
