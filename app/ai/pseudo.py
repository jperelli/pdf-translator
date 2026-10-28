"""A local stand-in for the OpenAI model, implementing the Agents SDK `Model` interface.

It lets the *real* agent code path (Runner, structured outputs, guardrails, tool calls, tracing) run with
zero tokens. Translations are pseudo-localizations, the classic i18n trick (Netflix, Microsoft): text is
made longer and written in the target script, so layout problems (overflow, RTL, CJK fonts) show up
without paying for a model. It also injects a few deterministic mistakes on the first pass (a dropped
number, a translated brand name) so the evaluate -> refine loop has something real to fix.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from typing import Any

from agents.items import ModelResponse
from agents.models.interface import Model
from agents.usage import Usage
from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText

TOKEN = re.compile(r"(</?[biu]>|<br>|https?://\S+|[\w.+-]+@[\w-]+\.[\w.]+|\d[\d.,:/]*|\s+|[^\w\s]|\w+)", re.UNICODE)
NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
ACCENTS = str.maketrans("aeiouycnAEIOUYCN", "áéíöüýçñÁÉÍÖÜÝÇÑ")
ARABIC = dict(zip("abcdefghijklmnopqrstuvwxyz", "ابتثجحخدذرزسشصضطظعغفقكلمنه"))
CYRILLIC = dict(zip("abcdefghijklmnopqrstuvwxyz", "абцдефгхийклмнопярстуввхыз"))
CJK = "的一是不了人我在有他这中大来上国个到说们为子和你地出道也时年得就那要下以生会自着去之过家学对可她里后小么心多天而能好都然没日于起还发成事只作当想看文无开手十用主行方又如前所本见经头面公同三已老从动两长知民样现分将外但身些与高意进把法此实回二理美点月明其种声全工己话儿者向情部正名定女问力机给等几很业最间新什打便位因重被走电四第门相次东政海口使教西再平真听世气信北少关并内加化由却代军产入先山五太水万市眼体别处总才场师书比住员九笑性通目华报立马命张活难神数件安表原车白应路期叫死常提感金何更反合放做系计或司利受光王果亲界及今京务制解各任至清物台象记边共风战干接它许八特觉望直服毛林题建南度统色字请交爱让认算论百吃义科怎元社术结六功指思非流每青管夫连远资队跟带花快条院变联言权往展该领传近留红治决周保达办运武半候七必城父强步完革深区即求品士转量空甚众技轻程告江语英基派满式李息写呢识极令黄德收脸钱党倒未持取设始版双历越史商千片容研像找友孩站广改议形委早房音火际则首单据导影失拿网香似斯专石若兵弟谁校读志飞观争究包组造落视济喜离虽坏兴切晚毫怕整冲欢易"

FACTORS = {"de": 1.35, "fi": 1.4, "fr": 1.25, "es": 1.22, "it": 1.2, "pt": 1.22, "nl": 1.3, "pl": 1.3,
           "sv": 1.15, "ru": 1.25, "en": 1.0, "ar": 1.2, "he": 1.1}
SCRIPTS = {"ar": "arabic", "he": "arabic", "ru": "cyrillic", "zh": "cjk", "ja": "cjk", "ko": "cjk"}
JARGON = {"holeshot": "holeshot", "whoops": "whoops", "berm": "berm", "berms": "berms", "brap": "brap",
          "regen": "regen", "ruts": "ruts"}
STOPWORDS = {
    "en": {"the", "and", "of", "to", "is", "your", "for", "with", "this"},
    "de": {"der", "die", "und", "das", "sie", "ist", "nicht", "für", "mit"},
    "es": {"el", "la", "de", "que", "y", "los", "las", "para", "con"},
    "fr": {"le", "la", "les", "et", "des", "est", "pour", "vous", "avec"},
}
NAMES = {"en": "English", "de": "German", "es": "Spanish", "fr": "French", "ar": "Arabic", "zh": "Chinese"}


def _h(s: str) -> int:
    return int(hashlib.md5(s.encode()).hexdigest()[:8], 16)


def _accents(target: str) -> dict[int, str]:
    """Accent only with letters the target language really uses (German: äöü, Spanish: áéíóúñ...)."""
    import unicodedata

    from app.languages import LANGUAGES

    lang = LANGUAGES.get(target)
    if not lang or not lang.probe:
        return {}
    table = {}
    for ch in lang.probe:
        base = unicodedata.normalize("NFD", ch)[0]
        if base.isalpha() and len(unicodedata.normalize("NFD", ch)) > 1 and ord(base) not in table:
            table[ord(base)] = ch
    return table


def _word(w: str, target: str, i: int, pad: bool) -> str:
    script = SCRIPTS.get(target, "latin")
    if script == "latin" and not re.search(r"[A-Za-zÀ-ÿ]", w):  # non-Latin source word: fake a transliteration
        return "".join(("ka", "lo", "mi", "ne", "ru", "sa", "to", "vi")[_h(w + str(k)) % 8]
                       for k in range(max(1, len(w) // 2)))
    if script == "cjk":
        return "".join(CJK[_h(w + str(k)) % len(CJK)] for k in range(max(1, len(w) // 4)))
    if script == "arabic":
        return "".join(ARABIC.get(c.lower(), c) for c in w)
    if script == "cyrillic":
        out = "".join(CYRILLIC.get(c.lower(), c) for c in w)
    else:
        out = w.translate(_accents(target))
    if pad and len(w) > 2:
        out += ("en", "ung", "ör", "ïa", "esë")[i % 5]
    return out


def pseudo_translate(text: str, target: str, protected: list[str], *, budget: int | None = None,
                     inject: bool = False) -> str:
    """Deterministic fake translation that respects tags, numbers, URLs and protected terms."""
    keep: dict[str, str] = {}
    for n, term in enumerate(sorted(protected, key=len, reverse=True)):
        if term and term in text:
            key = f"\x00{n}\x00"
            text = text.replace(term, key)
            keep[key] = term
    factor = FACTORS.get(target, 1.25)
    tokens = re.split(r"(\x00\d+\x00)", text)
    out: list[str] = []
    words = 0
    for tok in tokens:
        if tok in keep:
            out.append(keep[tok])
            continue
        for m in TOKEN.finditer(tok):
            t = m.group(0)
            if t[0].isalpha() and not t.startswith("<") and not t.startswith("http"):
                words += 1
                pad = budget is None and (words * 7 % 10) < (factor - 1) * 20
                t = JARGON.get(t.lower(), _word(t, target, words, pad))
            out.append(t)
    result = "".join(out)
    if SCRIPTS.get(target) == "cjk":
        result = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", result)
    if inject:
        nums = NUMBER.findall(result)
        if nums:
            result = result.replace(nums[0], "", 1)
        elif protected and any(p in result for p in protected):
            p = next(p for p in protected if p in result)
            result = result.replace(p, _word(p, target, 0, True), 1)
    if budget and len(result) > budget:
        parts = result.split(" ")
        result = " ".join(_abbrev(p, protected, 5) for p in parts)
    return result


def _abbrev(word: str, protected: list[str], keep: int) -> str:
    if len(word) <= keep or "<" in word or any(c.isdigit() for c in word) or any(word in p for p in protected):
        return word
    return word[: keep - 1] + "."


def _user_payload(items: list[Any]) -> dict[str, Any]:
    for item in reversed(items):
        if isinstance(item, dict) and item.get("role") == "user":
            content = item["content"]
            text = content if isinstance(content, str) else next(c["text"] for c in content if c["type"] == "input_text")
            obj, _ = json.JSONDecoder().raw_decode(text.strip())
            return obj
    return {}


def _protected_terms(instructions: str) -> list[str]:
    """The quoted terms of the TERMS section ("Protected (copy exactly as written...): "A", "B")."""
    for line in instructions.splitlines():
        if line.startswith("Protected ("):
            return re.findall(r'"([^"]+)"', line)
    return []


def _target(instructions: str) -> str:
    m = re.search(r"\(([a-z]{2})\)", instructions)
    if m:
        return m.group(1)
    m = re.search(r"Target language: ([^.]+)\.", instructions)
    names = {v: k for k, v in NAMES.items()}
    return names.get(m.group(1).split(" (")[0], "es") if m else "es"


def _analysis(payload: dict[str, Any]) -> dict[str, Any]:
    text = " ".join(b["text"] for b in payload.get("blocks", []))
    words = re.findall(r"[^\W\d_]+", text)
    lower = Counter(w.lower() for w in words)
    if re.search(r"[\u0600-\u06FF]", text):
        lang = "ar"
    elif re.search(r"[\u4e00-\u9fff]", text):
        lang = "zh"
    else:
        lang = max(STOPWORDS, key=lambda k: sum(lower[w] for w in STOPWORDS[k]))
    # all-caps headings are not names: only count capitals inside mixed-case blocks
    mixed = " ".join(b["text"] for b in payload.get("blocks", []) if not b["text"].isupper())
    caps = Counter(w for w in re.findall(r"[^\W\d_]+", mixed) if len(w) >= 3 and (w.isupper() or w[0].isupper()))
    sentence_starts = {m.group(1) for m in re.finditer(r"(?:^|[.!?:]\s+)(\w+)", text)}
    protected = sorted(w for w, n in caps.items()
                       if (w.isupper() and len(w) <= 6 and n >= 1) or (n >= 2 and w not in sentence_starts
                                                                        and w.lower() not in lower.keys() - {w}))
    protected += sorted(set(re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+|\b[\w-]+\.com\b", text)))
    for multi in ("Stark Future", "Stark VARG"):
        if multi in text:
            protected.append(multi)
    jargon = [{"term": t, "translation": JARGON[t], "note": "motocross slang, usually kept in English"}
              for t in JARGON if t in lower]
    return {
        "source_language": lang, "source_language_name": NAMES.get(lang, lang), "tone": "technical",
        "domain": "motorcycle documentation", "summary": f"Pseudo analysis of {len(words)} words.",
        "style_notes": "Direct, second person, imperative instructions.",
        "protected_terms": [{"term": t, "kind": "brand", "reason": "capitalised name"} for t in dict.fromkeys(protected)],
        "jargon": jargon,
    }


def _judge(payload: dict[str, Any], protected: list[str]) -> dict[str, Any]:
    out = []
    for b in payload.get("blocks", []):
        src, tr = b["source"], b["translation"]
        errors = []
        for p in protected:
            if p in src and p not in tr:
                errors.append({"severity": "critical", "category": "terminology", "span": p,
                               "explanation": f"Protected term '{p}' was translated.", "suggestion": f"Keep '{p}'."})
        missing = Counter(NUMBER.findall(src)) - Counter(NUMBER.findall(tr))
        for n in missing:
            errors.append({"severity": "major", "category": "omission", "span": n,
                           "explanation": f"The number {n} is missing.", "suggestion": f"Restore {n}."})
        if tr.strip() == src.strip() and len(src.split()) > 2:
            errors.append({"severity": "major", "category": "untranslated", "span": tr[:40],
                           "explanation": "Block left untranslated.", "suggestion": ""})
        if errors:  # like the real judge: only blocks with errors are listed
            out.append({"id": b["id"], "errors": errors})
    return {"blocks": out}


class PseudoModel(Model):
    def __init__(self, role: str):
        self.role = role

    async def get_response(self, system_instructions, input, model_settings, tools, output_schema, handoffs,
                           tracing, *, previous_response_id=None, conversation_id=None, prompt=None, **_):
        items = [{"role": "user", "content": input}] if isinstance(input, str) else list(input)
        payload = _user_payload(items)
        instr = system_instructions or ""
        target, protected = _target(instr), _protected_terms(instr)
        outputs: list[Any]
        if self.role == "analyst":
            outputs = [_message(_analysis(payload))]
        elif self.role == "judge":
            outputs = [_message(_judge(payload, protected))]
        elif self.role == "translator":
            def inject(bid: str, text: str) -> bool:  # ~11% of blocks, plus a third of those with numbers
                return _h(bid) % 9 == 0 or (bool(NUMBER.search(text)) and _h(bid) % 3 == 0)
            outputs = [_message({"translations": [
                {"id": b["id"], "text": pseudo_translate(b["text"], target, protected, inject=inject(b["id"], b["text"]))}
                for b in payload.get("blocks", [])]})]
        else:  # refiner / escalation: call the check_fit tool once per block, then answer
            outputs = self._refine(items, payload, target, protected, tools)
        size = sum(len(json.dumps(i, default=str)) for i in items)
        out_size = sum(len(o.model_dump_json()) for o in outputs)
        return ModelResponse(output=outputs, usage=Usage(requests=1, input_tokens=size // 4,
                                                         output_tokens=out_size // 4,
                                                         total_tokens=(size + out_size) // 4),
                             response_id=None)

    def _refine(self, items, payload, target, protected, tools) -> list[Any]:
        calls = {i.get("call_id"): i for i in items if isinstance(i, dict) and i.get("type") == "function_call"}
        results = {i.get("call_id"): i for i in items if isinstance(i, dict) and i.get("type") == "function_call_output"}
        blocks = payload.get("blocks", [])
        if not results and tools:
            out = []
            for n, b in enumerate(blocks):
                budget = None if b.get("fits", True) else int(b.get("max_chars", 40))
                if "shorter" in b.get("instruction", ""):
                    budget = max(4, int(len(b.get("current", "")) * 0.6))
                text = pseudo_translate(b["source"], target, protected, budget=budget)
                m = re.search(r"Use ONLY these characters[^:]*: (.*?)\. Choose", b.get("instruction", ""))
                if m:
                    allowed = set(m.group(1)) | {" "}
                    import unicodedata
                    text = "".join(c if c in allowed or c in "<>/" else
                                   (unicodedata.normalize("NFD", c)[0] if unicodedata.normalize("NFD", c)[0] in allowed else "")
                                   for c in text)
                out.append(ResponseFunctionToolCall(
                    id=f"fc_{n}", call_id=f"call_{n}_{_h(b['id'])}", type="function_call", name="check_fit",
                    arguments=json.dumps({"block_id": b["id"], "text": text}), status="completed"))
            return out
        final = {}
        for cid, call in calls.items():
            args = json.loads(call["arguments"])
            res = json.loads(results.get(cid, {}).get("output", "{}") or "{}")
            text = args["text"]
            if res.get("fits") is False:
                text = " ".join(_abbrev(w, protected, 4) for w in text.split(" "))
            final[args["block_id"]] = text
        return [_message({"translations": [{"id": b["id"], "text": final.get(b["id"]) or
                                            pseudo_translate(b["source"], target, protected)} for b in blocks]})]

    def stream_response(self, *args, **kwargs):  # pragma: no cover - the app never streams
        raise NotImplementedError


def _message(obj: dict[str, Any]) -> ResponseOutputMessage:
    return ResponseOutputMessage(
        id="msg_pseudo", type="message", role="assistant", status="completed",
        content=[ResponseOutputText(type="output_text", text=json.dumps(obj, ensure_ascii=False), annotations=[])])
