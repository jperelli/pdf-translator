"""Draw the architecture diagram (app/static/landing/architecture.svg), used by the landing page and the README.

    python scripts/architecture_svg.py

Plain SVG with presentation attributes only (the site's CSP blocks inline style attributes).
"""

from __future__ import annotations

from html import escape
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "app" / "static" / "landing" / "architecture.svg"
W, H = 1160, 922
FONT = "Archivo, 'Helvetica Neue', Helvetica, Arial, sans-serif"
INK, BODY, MUTED, LINE = "#2b2b29", "#4b4b48", "#85847f", "#d6d4cc"
GOLD, GOLD_INK, GOLD_SOFT = "#cfac57", "#8a6d27", "#f6efdc"
PANEL, DARK, BLUE, BLUE_SOFT = "#fcfbf8", "#232321", "#4f6d8f", "#e9eef4"

out: list[str] = []


def text(x, y, s, size=13, weight=400, fill=INK, anchor="start", italic=False):
    out.append(f'<text x="{x}" y="{y}" font-size="{size}" font-weight="{weight}" fill="{fill}" '
               f'text-anchor="{anchor}"{" font-style=" + chr(34) + "italic" + chr(34) if italic else ""}>'
               f'{escape(s)}</text>')


def box(x, y, w, h, title, lines=(), kind="code", tag=None):
    """kind: code (deterministic, zero tokens) | agent (LLM agent) | store | client | external."""
    fill, stroke, tfill, sub = {
        "code": ("#ffffff", LINE, INK, BODY),
        "agent": (GOLD_SOFT, GOLD, INK, BODY),
        "store": (DARK, DARK, "#ffffff", "#c9c7c0"),
        "client": ("#ffffff", INK, INK, BODY),
        "external": (BLUE_SOFT, BLUE, INK, BODY),
    }[kind]
    out.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="9" fill="{fill}" stroke="{stroke}" '
               f'stroke-width="{1.6 if kind in ("agent", "client", "external") else 1.2}"/>')
    text(x + 12, y + 21, title, 13.5, 600, tfill)
    if tag:
        text(x + w - 10, y + 20, tag, 10.5, 600, GOLD_INK if kind == "agent" else MUTED, "end")
    for i, line in enumerate(lines):
        text(x + 12, y + 39 + i * 15.5, line, 11.5, 400, sub)


def group(x, y, w, h, title, subtitle=""):
    out.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="14" fill="{PANEL}" stroke="{LINE}" '
               f'stroke-width="1.2" stroke-dasharray="5 4"/>')
    text(x + 16, y + 24, title.upper(), 11.5, 700, MUTED)
    if subtitle:
        text(x + 16 + len(title) * 8.6 + 10, y + 24, subtitle, 11.5, 400, MUTED)


def arrow(points, label=None, at=None, color=INK, dashed=False, both=False, lx=0, ly=-6, anchor="middle"):
    d = "M " + " L ".join(f"{px} {py}" for px, py in points)
    marker = "url(#a-gold)" if color == GOLD_INK else "url(#a-blue)" if color == BLUE else "url(#a)"
    out.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="1.5"'
               f'{" stroke-dasharray=" + chr(34) + "5 4" + chr(34) if dashed else ""} marker-end="{marker}"'
               f'{" marker-start=" + chr(34) + marker + chr(34) if both else ""}/>')
    if label:
        (ax, ay) = at or points[len(points) // 2]
        text(ax + lx, ay + ly, label, 11, 500, color if color != INK else BODY, anchor)


def defs():
    out.append("<defs>")
    for mid, color in (("a", INK), ("a-gold", GOLD_INK), ("a-blue", BLUE)):
        out.append(f'<marker id="{mid}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
                   f'orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10 z" fill="{color}"/></marker>')
    out.append("</defs>")


def draw() -> str:
    out.append(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
               f'font-family="{FONT}" role="img" aria-labelledby="t d">')
    out.append('<title id="t">Architecture of the Stark Document Translator</title>')
    out.append('<desc id="d">Browsers, AI agents and operators talk to a FastAPI web process. Jobs go through a '
               'Huey queue on SQLite to separate worker processes that run the translation pipeline: extraction, '
               'an Analyst agent, per page a Translator agent, deterministic checks and layout measurement, a '
               'Judge agent, Refiner and Escalation agents, then rendering and a read-back of the written PDF. '
               'Workers write progress events to SQLite; the web process streams them over SSE.</desc>')
    defs()
    out.append(f'<rect width="{W}" height="{H}" rx="18" fill="#f5f4f0"/>')
    Y = 150  # top of the middle band (clients, web process, workers)

    # ------------------------------------------------------------ external services (top)
    group(530, 16, 610, 112, "External services")
    box(546, 46, 362, 70, "OpenAI API  (Agents SDK)", ["Analyst, Translator, Refiner: gpt-5.4-mini · Judge: gpt-5.4",
                                                       "Escalation: gpt-5.5 · structured outputs, tools, traces"], "external")
    box(924, 46, 200, 70, "Google Fonts", ["full font of the same family", "when a subset lacks letters"], "external")

    # ------------------------------------------------------------ clients
    group(20, Y, 196, 470, "Clients")
    box(36, Y + 38, 164, 86, "Browser", ["Landing page /", "Translator /app", "Review, redo, stats"], "client")
    box(36, Y + 156, 164, 86, "AI agent", ["Any MCP client", "Streamable HTTP", "bearer MCP_TOKEN"], "client")
    box(36, Y + 274, 164, 86, "Operator", ["/app#admin (cookie)", "queue, workers,", "cancel / retry"], "client")
    box(36, Y + 388, 164, 66, "Prometheus", ["scrapes /metrics", "→ Grafana dashboard"], "external")

    # ------------------------------------------------------------ web process
    group(244, Y, 262, 470, "Web process", "FastAPI · uvicorn")
    box(260, Y + 38, 230, 70, "REST API", ["/api/jobs  upload, result, redo", "/api/uploads  files for agents"])
    box(260, Y + 120, 230, 70, "Progress over SSE", ["/api/jobs/{id}/events", "new events read from SQLite"])
    box(260, Y + 202, 230, 86, "MCP server  /mcp", ["translate_pdf · wait_for_translation", "get_translation · cancel", "progress = notifications/progress"])
    box(260, Y + 300, 230, 70, "Admin + /metrics", ["login, queue view, actions", "Prometheus text format"])
    box(260, Y + 382, 230, 72, "Intake", ["PDF validation, page limits", "pdf_url fetch with SSRF guards"])

    arrow([(200, Y + 72), (260, Y + 72)])
    arrow([(260, Y + 150), (230, Y + 150), (230, Y + 100), (200, Y + 100)], "SSE", at=(230, Y + 128), lx=-6, ly=4, anchor="end")
    arrow([(200, Y + 196), (260, Y + 230)])
    arrow([(260, Y + 258), (200, Y + 220)], dashed=True)
    arrow([(200, Y + 316), (260, Y + 333)])
    arrow([(140, Y + 388), (140, Y + 372), (232, Y + 372), (232, Y + 346), (260, Y + 346)], dashed=True, color=BLUE)

    # ------------------------------------------------------------ workers
    group(530, Y, 610, 470, "Workers", "huey_consumer · N processes + scheduler")
    text(546, Y + 44, "asyncio.run(pipeline) per job · an event after every step · cancel checked between steps", 11, 400, MUTED)
    r1, r2, r3, r4 = Y + 58, Y + 168, Y + 284, Y + 384

    box(546, r1, 150, 82, "1  Extract", ["blocks, styles, fonts", "images stay in place", "OCR for scans"])
    box(712, r1, 180, 82, "2  Analyst", ["language, tone, domain", "protected brand names", "glossary ≤ 30 terms"], "agent", "AGENT")
    box(908, r1, 216, 82, "3  Translator", ["per page, in parallel, batched", "only the terms in the batch", "length budget per box"], "agent", "AGENT")
    arrow([(696, r1 + 41), (712, r1 + 41)])
    arrow([(892, r1 + 41), (908, r1 + 41)])

    box(908, r2, 216, 96, "4  Checks", ["numbers, tags, protected terms", "fit measured in the real renderer", "no growing into neighbours", "missing glyphs"])
    box(712, r2, 180, 96, "5  Judge (MQM)", ["translation errors with", "severity, span, suggestion;", "lists only blocks with errors"], "agent", "AGENT")
    box(546, r2, 150, 96, "6  Refiner", ["fixes failing blocks", "with a check_fit tool", "max N rounds"], "agent", "AGENT")
    arrow([(1016, r1 + 82), (1016, r2)])
    arrow([(908, r2 + 48), (892, r2 + 48)])
    arrow([(712, r2 + 48), (696, r2 + 48)])
    arrow([(621, r2), (621, r2 - 14), (1080, r2 - 14), (1080, r2)], "re-check", at=(850, r2 - 14), ly=-4, color=GOLD_INK)

    box(546, r3, 150, 82, "7  Escalation", ["stronger model", "+ page image", "Quality mode only"], "agent", "AGENT")
    box(712, r3, 180, 82, "8  Render", ["one layer per document,", "original fonts rebuilt,", "accents built, CJK once"])
    box(908, r3, 216, 82, "9  Read-back QA", ["reads the written PDF:", "overlapping lines, fonts used;", "an overlap fails the document"])
    arrow([(621, r2 + 96), (621, r3)], "if still failing", at=(621, r2 + 96), lx=8, ly=13, anchor="start", color=GOLD_INK)
    arrow([(696, r3 + 41), (712, r3 + 41)])
    arrow([(892, r3 + 41), (908, r3 + 41)])

    box(546, r4, 150, 70, "Recovery", ["every minute: requeue", "jobs whose worker died"])
    box(712, r4, 412, 70, "10  Report + Review", ["two scores (headline = the lower) · cost per call",
                                                   "decisions for the user → redo makes a new version"])
    arrow([(1016, r3 + 82), (1016, r4)])

    # ------------------------------------------------------------ to and from the external services
    arrow([(727, 116), (727, Y)], "agent calls", at=(727, 133), lx=8, ly=4, anchor="start", color=BLUE, both=True)
    arrow([(1024, 116), (1024, Y)], "fonts (cached on /data)", at=(1024, 133), lx=-8, ly=4, anchor="end", color=BLUE, both=True)

    # ------------------------------------------------------------ storage
    S = 690
    group(244, S - 34, 896, 132, "/data volume")
    box(260, S, 230, 80, "Huey queue", ["queue.sqlite3", "tasks, retries, schedule"], "store")
    box(560, S, 250, 80, "SQLite  translator.sqlite3", ["jobs · versions · events · counters", "WAL; readers poll data_version"], "store")
    box(826, S, 142, 80, "Job files", ["source.pdf", "translated.vN.pdf"], "store")
    box(984, S, 140, 80, "Font cache", ["same-family fonts", "from Google Fonts"], "store")

    arrow([(410, Y + 454), (410, S)], "enqueue", at=(410, Y + 454), lx=-8, ly=26, anchor="end")
    # dequeue runs up the gap between the web process and the workers
    arrow([(490, S + 22), (518, S + 22), (518, Y + 420), (530, Y + 420)], "dequeue", at=(494, S + 44), lx=0, ly=0, anchor="start")
    arrow([(460, Y + 470), (460, 638), (640, 638), (640, S)], "job rows; events for SSE / MCP", at=(530, 638), ly=-5, anchor="start", both=True)
    arrow([(760, Y + 470), (760, S)], "results, events", at=(760, Y + 470), lx=8, ly=26, anchor="start")
    arrow([(897, Y + 470), (897, S)], "PDFs", at=(897, Y + 470), lx=8, ly=26, anchor="start")

    # ------------------------------------------------------------ legend and flow
    L = 846
    for i, (kind, label) in enumerate((("agent", "AI agent (LLM, costs tokens)"), ("code", "deterministic code, zero tokens"),
                                       ("store", "storage"), ("external", "external service"), ("client", "client"))):
        fill, stroke = {"agent": (GOLD_SOFT, GOLD), "code": ("#ffffff", LINE), "store": (DARK, DARK),
                        "external": (BLUE_SOFT, BLUE), "client": ("#ffffff", INK)}[kind]
        x = 36 + i * 214
        out.append(f'<rect x="{x}" y="{L}" width="18" height="14" rx="3" fill="{fill}" stroke="{stroke}" stroke-width="1.4"/>')
        text(x + 26, L + 11.5, label, 12, 400, BODY)
    text(36, L + 42, "One job: upload or MCP call → validate → job row + queued task → worker: extract → analyse → per page "
                     "translate, check, judge, refine (→ escalate) → render → read back → report.", 12, 400, BODY)
    text(36, L + 60, "Every step writes an event to SQLite; the web process streams it to the browser (SSE) and to MCP "
                     "clients (progress notifications).", 12, 400, BODY)

    out.append("</svg>")
    return "\n".join(out)


if __name__ == "__main__":
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(draw() + "\n")
    print(f"wrote {OUT}")
