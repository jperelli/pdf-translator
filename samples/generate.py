"""Regenerate the synthetic test corpus in this folder.

Every sample is small (<= 5 pages) and built to stress one thing. The Stark
VARG manual excerpt is cut from the real owner's manual (see --manual).

    python samples/generate.py                 # synthetic samples
    python samples/generate.py --manual X.pdf  # also cut the first 20 pages of X
"""

from __future__ import annotations

import argparse
import io
import json
from pathlib import Path

import pymupdf

HERE = Path(__file__).parent
A4 = pymupdf.paper_rect("a4")
GOLD = (0.81, 0.67, 0.34)
INK = (0.12, 0.12, 0.12)


def html(page: pymupdf.Page, rect, body: str, css: str = "") -> None:
    spare, _ = page.insert_htmlbox(pymupdf.Rect(rect), body, css=css)
    if spare < 0:
        raise RuntimeError(f"sample text does not fit in {rect}: {body[:60]}")


def illustration(width: int = 360, height: int = 200, label: str = "VARG") -> bytes:
    """A small raster 'photo' (rendered vector drawing) to test image preservation."""
    doc = pymupdf.open()
    page = doc.new_page(width=width, height=height)
    page.draw_rect(page.rect, fill=(0.88, 0.88, 0.88), color=None)
    for i in range(12):
        page.draw_rect(pymupdf.Rect(0, i * height / 12, width, (i + 1) * height / 12),
                       fill=(0.95 - i * 0.03, 0.93 - i * 0.03, 0.88 - i * 0.04), color=None)
    for cx in (90, 270):
        page.draw_circle((cx, 140), 42, color=INK, width=9)
        page.draw_circle((cx, 140), 8, fill=INK, color=None)
    page.draw_polyline([(90, 140), (150, 80), (230, 80), (270, 140)], color=INK, width=7)
    page.draw_polyline([(150, 80), (175, 130), (230, 80)], color=GOLD, width=6)
    page.draw_line((215, 60), (245, 55), color=INK, width=6)
    page.insert_text((20, 30), label, fontsize=18, fontname="hebo", color=GOLD)
    return page.get_pixmap(dpi=144).tobytes("png")


def header(page: pymupdf.Page, title: str, n: int) -> None:
    page.draw_rect(pymupdf.Rect(0, 0, A4.width, 6), fill=GOLD, color=None)
    page.insert_text((40, 34), "STARK FUTURE", fontsize=9, fontname="hebo", color=INK)
    html(page, (A4.width - 200, 25, A4.width - 40, 40),
         f"<p style='font-size:8pt;font-family:sans-serif;color:#666;text-align:right'>{title}</p>")
    page.insert_text((A4.width / 2 - 4, A4.height - 24), str(n), fontsize=8, fontname="helv")


# --------------------------------------------------------------------------- samples

def simple_en() -> pymupdf.Document:
    doc = pymupdf.open()
    p = doc.new_page()
    header(p, "Quick start guide", 1)
    html(p, (40, 60, 555, 110), "<h1 style='font-size:24pt;font-family:sans-serif'>Welcome to your new bike</h1>")
    html(p, (40, 115, 555, 200),
         "<p style='font-size:11pt;font-family:serif;line-height:1.35'>Thank you for choosing an electric "
         "motorcycle. This short guide explains the first steps you should follow before your first ride. "
         "Please read it carefully and keep it in a safe place for future reference.</p>")
    p.insert_image(pymupdf.Rect(40, 210, 330, 371), stream=illustration())
    html(p, (345, 215, 555, 371),
         "<p style='font-size:10pt;font-family:sans-serif;line-height:1.3'><b>Before you ride</b><br>"
         "Check the tyre pressure, test both brakes and make sure the battery is fully charged.</p>")
    html(p, (40, 390, 555, 520),
         "<ul style='font-size:11pt;font-family:serif;line-height:1.4'>"
         "<li>Charge the battery for at least four hours.</li>"
         "<li>Adjust the mirrors and the brake levers.</li>"
         "<li>Wear a helmet, gloves and protective boots.</li></ul>")
    html(p, (40, 540, 555, 600),
         "<p style='font-size:9pt;font-family:sans-serif;color:#666'>If you have any questions, contact "
         "your local dealer. Enjoy the ride!</p>")
    return doc


def tight_boxes_en() -> pymupdf.Document:
    """Short strings in fixed boxes: the worst case for text expansion (EN->DE ~ +200%)."""
    doc = pymupdf.open()
    p = doc.new_page()
    header(p, "Technical specifications", 1)
    html(p, (40, 55, 555, 90), "<h2 style='font-size:18pt;font-family:sans-serif'>Specifications</h2>")
    rows = [("Top speed", "120 km/h"), ("Peak power", "60 kW"), ("Torque", "938 Nm"),
            ("Weight", "118 kg"), ("Battery", "7.2 kWh"), ("Charge time", "2 h"), ("Range", "6 h trail")]
    y = 100
    for i, (k, v) in enumerate(rows):
        fill = (0.95, 0.94, 0.9) if i % 2 == 0 else (1, 1, 1)
        for x0, x1, txt, bold in ((40, 170, k, True), (170, 260, v, False)):
            r = pymupdf.Rect(x0, y, x1, y + 22)
            p.draw_rect(r, color=(0.7, 0.7, 0.7), fill=fill, width=0.5)
            w = "bold" if bold else "normal"
            html(p, r + (4, 4, -4, -2), f"<p style='font-size:9pt;font-family:sans-serif;font-weight:{w}'>{txt}</p>")
        y += 22
    # buttons / labels
    for i, label in enumerate(["Start", "Stop", "Reset", "Mode", "Lights", "Horn"]):
        r = pymupdf.Rect(300 + (i % 3) * 85, 110 + (i // 3) * 40, 375 + (i % 3) * 85, 138 + (i // 3) * 40)
        p.draw_rect(r, color=INK, fill=(0.2, 0.2, 0.2), width=0.5, radius=0.25)
        html(p, r + (2, 7, -2, -2), f"<p style='text-align:center;font-size:9pt;color:white;font-family:sans-serif;font-weight:bold'>{label}</p>")
    # callouts around an image
    p.insert_image(pymupdf.Rect(150, 300, 445, 466), stream=illustration(label="SPEC"))
    for (x, y0), txt in [((40, 310), "Brake lever"), ((40, 400), "Footpeg"), ((460, 310), "Throttle"),
                         ((460, 400), "Charge port")]:
        r = pymupdf.Rect(x, y0, x + 95, y0 + 16)
        p.draw_rect(r, color=GOLD, width=0.8)
        html(p, r + (3, 2, -3, 0), f"<p style='font-size:8pt;font-family:sans-serif'>{txt}</p>")
    html(p, (40, 490, 555, 530), "<p style='font-size:7pt;font-family:sans-serif;color:#555'>"
         "Values measured under test conditions. Real values may vary.</p>")

    p2 = doc.new_page()
    header(p2, "Technical specifications", 2)
    html(p2, (40, 55, 555, 90), "<h2 style='font-size:18pt;font-family:sans-serif'>Dashboard icons</h2>")
    icons = ["Low battery", "Overheat", "Service due", "Traction control", "Ride mode", "Neutral",
             "Side stand down", "Phone connected"]
    for i, txt in enumerate(icons):
        col, row = i % 4, i // 4
        r = pymupdf.Rect(40 + col * 130, 110 + row * 90, 160 + col * 130, 180 + row * 90)
        p2.draw_rect(r, color=(0.6, 0.6, 0.6), width=0.5)
        p2.draw_circle(r.tl + (60, 22), 12, color=GOLD, fill=GOLD)
        html(p2, pymupdf.Rect(r.x0 + 4, r.y0 + 44, r.x1 - 4, r.y1 - 4),
             f"<p style='text-align:center;font-size:8.5pt;font-family:sans-serif'>{txt}</p>")
    return doc


def multi_style_en() -> pymupdf.Document:
    doc = pymupdf.open()
    for n in (1, 2):
        p = doc.new_page()
        header(p, "Maintenance notes", n)
        html(p, (40, 55, 555, 95), f"<h1 style='font-size:20pt;font-family:serif;font-style:italic'>"
             f"{'Caring for your machine' if n == 1 else 'Storage and transport'}</h1>")
        col = ("<p style='font-size:10pt;font-family:serif;line-height:1.35;text-align:justify'>"
               "Regular maintenance keeps your motorcycle safe and reliable. <b>Clean the chain</b> after every "
               "muddy ride and lubricate it with a <i>biodegradable</i> chain oil. Inspect the brake pads monthly; "
               "replace them when the friction material is thinner than two millimetres.</p>")
        html(p, (40, 105, 290, 330), col)
        html(p, (305, 105, 555, 330), col.replace("Clean the chain", "Check the spokes")
             .replace("chain oil", "spoke wrench"))
        box = pymupdf.Rect(40, 345, 555, 420)
        p.draw_rect(box, color=(0.75, 0.2, 0.2), fill=(1, 0.93, 0.9), width=1)
        html(p, box + (10, 8, -10, -6), "<p style='font-size:10pt;font-family:sans-serif;color:#8a1c1c'>"
             "<b>WARNING:</b> Never wash the motorcycle with a high-pressure cleaner pointed directly at the "
             "battery, the motor or the electrical connectors.</p>")
        html(p, (40, 435, 555, 470), "<p style='font-family:monospace;font-size:9pt;color:#333'>"
             "Error code E-042: motor temperature sensor fault</p>")
        p.insert_image(pymupdf.Rect(40, 480, 300, 624), stream=illustration(label="CARE"))
        html(p, (310, 490, 555, 624), "<p style='font-size:8pt;font-family:sans-serif;color:#555;"
             "font-style:italic'>Figure: keep the motorcycle upright and dry when it is stored for more than "
             "two weeks. Charge the battery to 60 % before storage.</p>")
        html(p, (40, 640, 555, 700), "<p style='font-size:12pt;font-family:sans-serif;color:#9b7a2c'>"
             "Tip: write down the date of each service in the logbook.</p>")
    return doc


def _single_lang(title: str, paras: list[str], direction: str = "ltr", label: str = "") -> pymupdf.Document:
    doc = pymupdf.open()
    p = doc.new_page()
    header(p, label, 1)
    align = "right" if direction == "rtl" else "left"
    html(p, (40, 60, 555, 110), f"<h1 dir='{direction}' style='font-size:22pt;text-align:{align}'>{title}</h1>")
    y = 120
    for para in paras:
        html(p, (40, y, 555, y + 90), f"<p dir='{direction}' style='font-size:11pt;line-height:1.4;"
             f"text-align:{align}'>{para}</p>")
        y += 95
    p.insert_image(pymupdf.Rect(150, y + 10, 445, y + 176), stream=illustration())
    return doc


def source_de() -> pymupdf.Document:
    return _single_lang(
        "Sicherheitshinweise für Ihr Motorrad",
        ["Lesen Sie diese Bedienungsanleitung vollständig durch, bevor Sie das Fahrzeug zum ersten Mal benutzen. "
         "Die Nichtbeachtung der Sicherheitshinweise kann zu schweren Verletzungen führen.",
         "Überprüfen Sie vor jeder Fahrt den Reifendruck, die Bremsflüssigkeit und die Funktion der "
         "Beleuchtungsanlage. Die Höchstgeschwindigkeit beträgt 120 km/h.",
         "Für Schäden, die durch unsachgemäße Wartung entstehen, übernimmt der Hersteller keine Haftung. "
         "Größere Reparaturen dürfen ausschließlich von autorisierten Fachwerkstätten durchgeführt werden."],
        label="Bedienungsanleitung")


def source_ar() -> pymupdf.Document:
    """Built with WeasyPrint, not MuPDF: MuPDF writes Arabic with a lossy text layer (presentation forms, no
    spaces), while WeasyPrint produces the kind of text layer real Arabic PDFs have (logical order, but
    lam-alef ligatures and numbers in visual order still come out scrambled, as in the wild)."""
    import base64

    from weasyprint import HTML

    img = base64.b64encode(illustration()).decode()
    paras = [
        "اقرأ دليل المستخدم هذا بالكامل قبل استخدام الدراجة لأول مرة. قد يؤدي عدم اتباع تعليمات السلامة إلى "
        "إصابات خطيرة.",
        "تحقق من ضغط الإطارات والفرامل والأضواء قبل كل رحلة. السرعة القصوى هي 120 كم/ساعة.",
        "يجب شحن البطارية لمدة أربع ساعات على الأقل قبل الاستخدام الأول. استخدم الشاحن الأصلي فقط.",
    ]
    body = "".join(f"<p>{p}</p>" for p in paras)
    page_html = f"""<html dir="rtl"><head><meta charset="utf-8"><style>
      @page {{ size: A4; margin: 40pt; }}
      body {{ font-family: 'DejaVu Sans', sans-serif; font-size: 11pt; line-height: 1.5; color: #1f1f1f; }}
      .top {{ border-top: 6pt solid #cfac57; margin: -40pt -40pt 20pt; padding: 14pt 40pt 0;
              font-size: 8pt; color: #666; }}
      h1 {{ font-size: 20pt; margin: 10pt 0 14pt; }} p {{ margin: 0 0 22pt; }}
      img {{ display: block; margin: 10pt auto; width: 295pt; }}
    </style></head><body><div class="top">دليل المستخدم — STARK FUTURE</div>
    <h1>تعليمات السلامة للدراجة النارية</h1>{body}<img src="data:image/png;base64,{img}"></body></html>"""
    return pymupdf.open("pdf", HTML(string=page_html).write_pdf())


def source_zh() -> pymupdf.Document:
    return _single_lang(
        "摩托车安全须知",
        ["首次使用车辆前，请完整阅读本使用手册。不遵守安全须知可能导致严重伤害。",
         "每次骑行前，请检查轮胎气压、制动液以及照明系统是否正常。最高时速为每小时120公里。",
         "首次使用前，电池至少需要充电四个小时。请仅使用原厂充电器。"],
        label="使用手册")


def jargon_en() -> pymupdf.Document:
    """Brand names and motocross slang that must NOT be translated literally (Stark != 'strong')."""
    doc = pymupdf.open()
    p = doc.new_page()
    header(p, "Rider tips", 1)
    html(p, (40, 60, 555, 105), "<h1 style='font-size:22pt;font-family:sans-serif'>Ride the Stark VARG like a pro</h1>")
    html(p, (40, 115, 555, 330),
         "<p style='font-size:11pt;font-family:serif;line-height:1.4'>The Stark VARG MX delivers instant torque, "
         "so nailing the holeshot is easier than on any 450. Use the Stark phone on the handlebar to pick one of "
         "the 100 power modes. On the whoops, stay on the pegs and let the suspension soak up the bumps. "
         "In tight berms, keep your inside leg up and look for the line through the ruts. "
         "When the track gets rough, a little brap on the throttle helps the rear wheel find traction. "
         "Your Stark Future account keeps every ride, lap time and setting in sync.</p>")
    html(p, (40, 340, 555, 420),
         "<p style='font-size:10pt;font-family:sans-serif;color:#555'>Pro tip: lower the regen setting for "
         "smoother entries into the berm and raise it for steep downhill sections.</p>")
    p.insert_image(pymupdf.Rect(150, 430, 445, 596), stream=illustration(label="MX"))
    return doc


def _words(page: pymupdf.Page, x: float, y: float, text: str, size: float, font: str = "helv",
           color=(0, 0, 0), align_right: float | None = None) -> float:
    """Write each word as its own text object with no space characters, the way many CV builders and
    browsers' "print to PDF" do. Returns the x after the last word."""
    f = pymupdf.Font(font)
    gap = f.text_length(" ", size)
    width = sum(f.text_length(w, size) for w in text.split()) + gap * (len(text.split()) - 1)
    if align_right is not None:
        x = align_right - width
    for w in text.split():
        page.insert_text((x, y), w, fontsize=size, fontname=font, color=color)
        x += f.text_length(w, size) + gap
    return x


def cv_en() -> pymupdf.Document:
    """CV-like page reproducing what broke in a real CV: words without space characters, a two-column
    header on a dark band, right-aligned contact lines with underlined links, a summary whose line breaks
    carry the structure, separators, an underlined section title, a job row with a right-aligned date,
    and arrows in a different font inside paragraphs."""
    doc = pymupdf.open()
    p = doc.new_page()
    W = A4.width
    _words(p, 0, 34, "Last update Sep 2026", 6, align_right=W - 120)
    band = pymupdf.Rect(0, 46, W, 132)
    p.draw_rect(band, fill=(0.33, 0.34, 0.42), color=None)
    p.draw_circle((88, 89), 32, fill=(0.75, 0.72, 0.68), color=(1, 1, 1), width=1.5)
    white = (1, 1, 1)
    _words(p, 130, 76, "Jordan Example", 13, "hebo", white)
    _words(p, 130, 93, "Principal Software Engineer", 11, color=white)
    _words(p, 130, 112, "Italian/Argentinian in Spain", 8, color=white)
    _words(p, 130, 123, "16 years of experience in javascript, python, +", 8, color=white)
    right = W - 60
    for i, (line, link) in enumerate([("jordan@example.com", False), ("+34 600 000 000", False),
                                      ("Example Street 1, 2-3", False), ("Barcelona, Spain", False),
                                      ("https://github.com/example", True), ("https://example.com", True)]):
        y = 60 + i * 11.5
        x1 = _words(p, 0, y, line, 6.5, color=white, align_right=right)
        if link:
            p.draw_line((x1 - pymupdf.Font("helv").text_length(line, 6.5) - 1.5, y + 1.5), (right, y + 1.5),
                        color=(0.85, 0.8, 0.5), width=0.5)
    # summary with a vertical rule on the left and separators between parts
    p.draw_line((70, 150), (70, 300), color=(0.75, 0.75, 0.75), width=0.6)
    _words(p, 78, 166, "16 years of experience building web, frontend, backend, databases, mobile.", 8.5)
    _words(p, 78, 177, "Team player. Coder, architect and ad hoc management. Autonomous problem-solver.", 8.5)
    p.draw_line((76, 184), (480, 184), color=(0.85, 0.85, 0.85), width=0.5)
    _words(p, 78, 196, "Alma Mater: National Technological University, Argentina", 8.5)
    p.draw_line((76, 203), (480, 203), color=(0.85, 0.85, 0.85), width=0.5)
    _words(p, 78, 215, "Top Programming skills:", 8.5)
    _words(p, 90, 226, "Javascript/Typescript, CSS/SCSS, Python/Django, Postgresql/Mysql/Mongodb.", 8.5)
    _words(p, 78, 238, "Top Soft skills:", 8.5)
    _words(p, 90, 249, "Mentoring. Team communication facilitator, fostering collaboration and unity.", 8.5)
    _words(p, 90, 260, "Requirements analysis with stakeholders to simplify engineering complexity.", 8.5)
    _words(p, 90, 271, "Very autonomous and persistent problem-solver.", 8.5)
    # section title, underlined with a vector line
    x1 = _words(p, 62, 322, "Work Experience", 9.5, "hebo")
    p.draw_line((62, 324.5), (x1 - 2, 324.5), color=(0, 0, 0), width=0.6)
    # job row: logo, bold title + right date, company + right detail
    p.draw_rect(pymupdf.Rect(62, 334, 84, 356), fill=(0.55, 0.8, 0.7), color=None)
    p.draw_rect(pymupdf.Rect(86, 334, 108, 356), fill=(0.75, 0.1, 0.15), color=None)
    _words(p, 114, 343, "Principal Software Engineer", 8.5, "hebo")
    _words(p, 340, 343, "2023 - current (4 years)", 7)
    _words(p, 114, 354, "examplebet.ag [Example subsidiary] - Online gaming", 8.5)
    _words(p, 340, 354, "Remote, distributed team", 7)
    # paragraphs with inline bold and arrows in another font; each "- " item starts a new line
    html(p, (62, 366, 520, 470),
         "<div style='font-family:sans-serif;font-size:8.5pt;line-height:1.3'>"
         "I serve as the link between the Architects team, the Product Owner and the developers adding technical "
         "details and scope of tasks for an engineering team of 8+. I also help the team <b>adopt AI tools and "
         "processes</b> and solve the most <b>complex technical</b> challenges.<br>"
         "<u>Key achievements:</u><br>"
         "- DevEx: Cut backend build times from 15 <span style='font-family:serif'>→</span> 5 minutes, introduced "
         "HMR and css precompilation to reduce dev feedback loop from 10 min <span style='font-family:serif'>→</span>"
         " 10 sec, <b>massively boosting developer velocity</b>.<br>"
         "- Increased TypeScript coverage from 20% <span style='font-family:serif'>→</span> 80% (backend) and 0% "
         "<span style='font-family:serif'>→</span> 40% (frontend), reducing bugs and <b>improving code "
         "maintainability</b>.<br>"
         "- <b>Mentored team members</b> via 1:1 hands-on sessions and drove adoption of technical best "
         "practices.</div>")
    p.draw_line((62, 478), (520, 478), color=(0.85, 0.85, 0.85), width=0.5)
    x1 = _words(p, 62, 494, "Technologies", 9, "hebo")
    p.draw_line((62, 496.5), (x1 - 2, 496.5), color=(0, 0, 0), width=0.6)
    _words(p, 62, 508, "Javascript/Typescript, Nodejs, NestJS, MongoDB, NextJS, Svelte, CSS, Vite, Jest, Bash,", 8.5)
    _words(p, 62, 519, "Kubernetes, Docker compose, K6, ArgoCD, Jenkins.", 8.5)
    p.draw_line((62, 530), (520, 530), color=(0.85, 0.85, 0.85), width=0.5)
    return doc


def subset_heading_en() -> pymupdf.Document:
    """Headings in an embedded font subset that only has the letters of the English text (no umlauts,
    no accents), like the Stark manual's StarkSans: translating to German/Spanish needs glyphs it lacks."""
    doc = pymupdf.open()
    p = doc.new_page()
    header(p, "Appendixes", 1)
    tw = pymupdf.TextWriter(p.rect)
    heading = pymupdf.Font(fontbuffer=pymupdf.Font("cobo").buffer)  # Courier-Bold as a distinctive face
    tw.append((60, 110), "APPENDIXES", font=heading, fontsize=22)
    tw.append((60, 330), "CONTROLS AND DISPLAY", font=heading, fontsize=18)
    tw.write_text(p)
    for i, (num, txt, pg) in enumerate([("I", "Declaration of conformity", 118), ("II", "FCC statement", 120),
                                        ("III", "License agreement", 122), ("IV", "Data protection", 127),
                                        ("V", "Warranty", 138), ("VI", "Disclaimer", 141)]):
        y = 150 + i * 22
        p.insert_text((60, y), num, fontsize=8, fontname="helv", color=(0.5, 0.5, 0.5))
        p.insert_text((100, y), txt, fontsize=11, fontname="helv")
        p.insert_text((440, y), str(pg), fontsize=10, fontname="hebo", color=(0.4, 0.4, 0.4))
    html(p, (60, 345, 540, 420), "<p style='font-size:11pt;font-family:sans-serif'>Use the display to switch "
         "between ride modes. Hold the mode button for three seconds to open the settings menu.</p>")
    doc.subset_fonts()  # embed only the glyphs used: the heading font loses every letter it did not need
    return doc


def scanned_en() -> pymupdf.Document:
    """A text-only-as-pixels version of the simple sample: forces the OCR path."""
    src = simple_en()
    pix = src[0].get_pixmap(dpi=150, colorspace=pymupdf.csGRAY)
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_image(p.rect, stream=pix.tobytes("png"))
    return doc


def invalid_samples() -> None:
    bad = HERE / "invalid"
    bad.mkdir(exist_ok=True)
    good = simple_en().tobytes(garbage=4, deflate=True)
    (bad / "truncated.pdf").write_bytes(good[: len(good) // 3])
    (bad / "not-a-pdf.pdf").write_text("This is plain text with a .pdf extension.\n")
    enc = simple_en()
    buf = io.BytesIO()
    enc.save(buf, garbage=4, deflate=True, encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="secret")
    (bad / "encrypted.pdf").write_bytes(buf.getvalue())


SAMPLES = {
    "01-simple-en.pdf": (simple_en, "Happy path: headings, paragraphs, bullets, one image.", ["es", "de"]),
    "02-tight-boxes-en.pdf": (tight_boxes_en, "Short labels in fixed boxes, table cells, buttons, callouts. "
                              "Overflow torture test (EN->DE expands 30-300%).", ["de", "ar", "zh"]),
    "03-multi-style-en.pdf": (multi_style_en, "Two columns, serif/sans/mono, bold/italic inline, colored "
                              "warning box, caption.", ["de", "ar", "zh"]),
    "04-source-de.pdf": (source_de, "German source: umlauts, eszett, long compounds.", ["en", "es"]),
    "05-source-ar.pdf": (source_ar, "Arabic source: RTL extraction and shaping.", ["en", "es"]),
    "06-source-zh.pdf": (source_zh, "Chinese source: logographic text expands a lot into Latin scripts.",
                         ["en", "de"]),
    "07-scanned-en.pdf": (scanned_en, "Scanned page with no text layer: OCR path.", ["es"]),
    "08-jargon-en.pdf": (jargon_en, "Brand names (Stark, VARG) and MX slang (holeshot, whoops, berm, brap).",
                         ["es", "de"]),
    "10-cv-like-en.pdf": (cv_en, "CV layout: words stored without spaces, two-column header on a dark band, "
                          "underlined links and titles, separators, structured summary, symbol-font arrows. "
                          "Collision regression test.", ["es", "de"]),
    "11-subset-heading-en.pdf": (subset_heading_en, "Headings in a font subset without umlauts/accents: "
                                 "missing-glyph regression test.", ["de", "es"]),
}


def cut_manual(path: Path, pages: int = 20) -> None:
    src = pymupdf.open(path)
    out = pymupdf.open()
    out.insert_pdf(src, from_page=0, to_page=pages - 1)
    out.save(HERE / "09-stark-varg-manual-20p.pdf", garbage=4, deflate=True, deflate_images=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manual", type=Path, help="full Stark VARG owner's manual PDF to cut")
    args = ap.parse_args()
    manifest = {}
    for name, (fn, desc, targets) in SAMPLES.items():
        doc = fn()
        doc.set_metadata({"title": name, "producer": "pdf-translator samples/generate.py"})
        doc.save(HERE / name, garbage=4, deflate=True)
        manifest[name] = {"description": desc, "targets": targets, "pages": doc.page_count}
    if args.manual:
        cut_manual(args.manual)
    manifest["09-stark-varg-manual-20p.pdf"] = {
        "description": "First 20 pages of the real Stark VARG Owner's Manual (InDesign, subset fonts, "
                       "photos, TOC with tab leaders, warnings). The final boss.",
        "targets": ["es", "de", "ar", "zh"], "pages": 20,
        "source": "https://australianelectricmotorco.com/wp-content/uploads/2023/06/VARG-Owners-Manual-1.pdf",
    }
    invalid_samples()
    (HERE / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {len(manifest)} samples to {HERE}")


if __name__ == "__main__":
    main()
