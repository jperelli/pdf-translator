"""Layout engine: extraction + re-rendering, no AI involved."""

import pymupdf
import pytest

from app.formats.pdf import PdfDocument
from app.formats.pdf.render import plain_text, sanitize_markup
from app.languages import get_language


def load(path):
    return PdfDocument(path, max_pages=60, ocr_language="eng", min_font_scale=0.72)


@pytest.mark.parametrize("name", ["01-simple-en.pdf", "02-tight-boxes-en.pdf", "03-multi-style-en.pdf",
                                  "05-source-ar.pdf", "09-stark-varg-manual-20p.pdf"])
def test_identity_round_trip_fits(samples, name):
    """Writing the original text back must fit its own box: the baseline for every translation."""
    doc = load(samples / name)
    target = get_language("ar" if "-ar" in name else "en")
    units = [u for p in range(doc.page_count) for u in doc.units(p)]
    doc.prepare(target)
    statuses = [doc.measure(u, u.text, target).status for u in units]
    assert units
    assert statuses.count("fit") / len(statuses) >= 0.97, statuses


def test_paragraphs_styles_and_containers(samples):
    units = load(samples / "03-multi-style-en.pdf").units(0)
    body = next(u for u in units if u.text.startswith("Regular maintenance"))
    assert "<b>Clean the chain</b>" in body.text and "<i>biodegradable</i>" in body.text
    assert body.style.family == "serif" and body.style.align == "justify"
    warning = next(u for u in units if "WARNING" in u.text)
    assert warning.container is not None, "text inside the warning frame must be bound to it"
    assert next(u for u in units if "E-042" in u.text).style.family == "monospace"


def test_ligatures_are_expanded(samples):
    texts = [u.text for u in load(samples / "02-tight-boxes-en.pdf").units(0)]
    assert "Technical specifications" in texts and not any("\ufb01" in t for t in texts)


def test_rtl_source_paragraphs(samples):
    units = load(samples / "05-source-ar.pdf").units(0)
    assert all(u.style.align == "right" for u in units)
    assert len(units) == 5  # wrapped lines of a paragraph are merged, not split


def test_scanned_page_goes_through_ocr(samples):
    doc = load(samples / "07-scanned-en.pdf")
    assert doc.scanned_pages == [0]
    units = doc.units(0)
    assert all(u.ocr for u in units)
    assert any("Welcome to your new bike" in u.text for u in units)


def test_full_page_photo_is_not_ocred(samples):
    assert load(samples / "09-stark-varg-manual-20p.pdf").scanned_pages == []


def test_long_word_overflow_is_detected(samples):
    doc = load(samples / "02-tight-boxes-en.pdf")
    unit = next(u for u in doc.units(0) if u.text == "Start")  # a button label
    doc.prepare(get_language("de"))
    assert doc.measure(unit, "Starten", get_language("de")).status in ("fit", "expanded", "shrunk")
    assert doc.measure(unit, "Inbetriebnahmevorgangseinleitung", get_language("de")).status == "overflow"


@pytest.mark.parametrize("bad", ["<b>open", "a < b & c", "<script>x</script>", "<i>x</b>"])
def test_markup_is_sanitized(bad):
    out = sanitize_markup(bad)
    assert "<script>" not in out and "<b>open" not in out
    assert sanitize_markup("<b>bold</b> & <i>it</i>") == "<b>bold</b> &amp; <i>it</i>"
    assert plain_text(sanitize_markup("<b>bold</b> & <i>it</i>")) == "bold & it"


def test_render_keeps_images(samples, tmp_path):
    doc = load(samples / "01-simple-en.pdf")
    target = get_language("de")
    units = doc.units(0)
    doc.prepare(target)
    tr = {u.id: u.text + " übersetzt" for u in units}
    info = doc.render(tr, target, tmp_path / "out.pdf")
    assert info["images_preserved"] and not info["output_overlaps"]
    text = pymupdf.open(tmp_path / "out.pdf")[0].get_text()
    assert "übersetzt" in text and "Welcome to your new bike" in text


def test_words_without_space_characters_get_spaces(samples):
    texts = [u.text for u in load(samples / "10-cv-like-en.pdf").units(0)]
    assert "Jordan Example" in texts and "Principal Software Engineer" in texts


def test_structure_and_underlines_are_kept(samples):
    units = load(samples / "10-cv-like-en.pdf").units(0)
    contact = next(u for u in units if "jordan@example.com" in u.text)
    assert contact.text.count("<br>") == 5 and contact.style.align == "right"
    assert "<u>https://github.com/example</u>" in contact.text and len(contact.decorations) == 2
    title = next(u for u in units if "Work Experience" in u.text)
    assert title.text == "<u>Work Experience</u>"
    # separators split the summary into its parts instead of one merged paragraph
    assert any(u.text.startswith("Alma Mater") for u in units)
    # arrows in another font stay inside their paragraph
    para = next(u for u in units if u.text.startswith("I serve"))
    assert para.text.count("→") == 4 and "<u>Key achievements:</u>" in para.text


def test_glyphs_are_built_from_the_fonts_own_pieces(samples):
    from app.formats.pdf.render import FontBook

    doc = pymupdf.open(samples / "09-stark-varg-manual-20p.pdf")
    fb = FontBook(doc)
    stark = fb.fonts["StarkSans-Medium"]
    assert not stark.has("Ä") and stark.ensure({"Ä", "Ö", "Ü", "É"}) == {"É"}  # no acute shape to borrow
    assert stark.has("Ä") and stark.built == ["Ä", "Ö", "Ü"]
    page = pymupdf.open().new_page()
    arch = pymupdf.Archive()
    arch.add(stark.ttf(), "f.ttf")
    page.insert_htmlbox(pymupdf.Rect(20, 20, 500, 80), "<div>ANHÄNGE</div>", archive=arch,
                        css="@font-face{font-family:f;src:url(f.ttf);} div{font-family:f;font-size:20px}")
    fonts = {s["font"] for b in page.get_text("dict")["blocks"] for ln in b["lines"] for s in ln["spans"]}
    assert len(fonts) == 1, fonts  # no per-glyph fallback any more


def test_heading_font_fallback_is_reported(samples, tmp_path):
    doc = load(samples / "09-stark-varg-manual-20p.pdf")
    target = get_language("es")
    [doc.units(p) for p in range(doc.page_count)]
    doc.prepare(target)
    unit = next(u for u in doc.units(10) if u.style.font == "StarkSans-Medium")
    info = doc.render({unit.id: "APÉNDICES"}, target, tmp_path / "o.pdf")
    cats = {i.category for i in info["layout_issues"][unit.id]}
    assert "font_substituted" in cats  # É cannot be built: the whole block uses a generic font, and says so
