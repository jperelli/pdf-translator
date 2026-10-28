from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Language:
    code: str
    name: str
    native: str
    rtl: bool = False
    script: str = "latin"  # used by the deterministic "is this really the target language" check
    probe: str = ""  # letters beyond ASCII the language needs (font coverage check)


LANGUAGES: dict[str, Language] = {
    lang.code: lang
    for lang in [
        Language("es", "Spanish", "Español", probe="áéíóúñüÁÉÍÓÚÑ¿¡"),
        Language("en", "English", "English"),
        Language("de", "German", "Deutsch", probe="äöüßÄÖÜ"),
        Language("fr", "French", "Français", probe="àâçéèêëîïôûùüÿœÀÇÉÈ"),
        Language("it", "Italian", "Italiano", probe="àèéìòù"),
        Language("pt", "Portuguese", "Português", probe="ãõáâàéêíóôúçÃÕÁÉÍÓÚÇ"),
        Language("nl", "Dutch", "Nederlands", probe="ëïéè"),
        Language("sv", "Swedish", "Svenska", probe="åäöÅÄÖ"),
        Language("fi", "Finnish", "Suomi", probe="äöÄÖ"),
        Language("pl", "Polish", "Polski", probe="ąćęłńóśźżĄĆĘŁŃÓŚŹŻ"),
        Language("ru", "Russian", "Русский", script="cyrillic"),
        Language("ar", "Arabic", "العربية", rtl=True, script="arabic"),
        Language("he", "Hebrew", "עברית", rtl=True, script="hebrew"),
        Language("zh", "Chinese (Simplified)", "简体中文", script="cjk"),
        Language("ja", "Japanese", "日本語", script="cjk"),
        Language("ko", "Korean", "한국어", script="hangul"),
    ]
}

SCRIPT_PATTERNS = {
    "latin": re.compile(r"[A-Za-zÀ-ÿĀ-ž]"),
    "cyrillic": re.compile(r"[\u0400-\u04FF]"),
    "arabic": re.compile(r"[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]"),
    "hebrew": re.compile(r"[\u0590-\u05FF]"),
    "cjk": re.compile(r"[\u3040-\u30FF\u3400-\u4DBF\u4E00-\u9FFF]"),
    "hangul": re.compile(r"[\uAC00-\uD7AF]"),
}


def get_language(code: str) -> Language:
    try:
        return LANGUAGES[code]
    except KeyError as exc:
        raise ValueError(f"Unsupported target language: {code!r}") from exc
