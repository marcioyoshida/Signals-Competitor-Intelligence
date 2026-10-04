"""Deterministic backstops run on an LLM briefing BEFORE citation enforcement.

The model writes fluent pt-BR, but three failure shapes reached the live feed (2026-10-03):

1. **Regulator as an invented actor.** "O Banco Central assume o controle da operação do Nubank
   Parque" (09-20): the only news source said *Nubank* takes over the stadium operation; the
   Banco Central came from a BCB-IFDATA market-share row in the same cluster. A sentence that
   names a regulator as the one ACTING is kept only when a source actually involves that
   regulator: an official-act lens from it, or the regulator named in a source's own text.
2. **Past dates in the future tense.** An August SEC 6-K said "on September 10 we will disclose";
   the 09-21 and 09-27 briefings repeated "em 10 de setembro de 2026, divulgará". A sentence that
   puts a date before the run date in the future tense is dropped.
3. **SEC glossed as CVM.** "Comissão de Valores Mobiliários (SEC)" names Brazil's regulator for
   the US one; rewritten to the SEC's own name.

Each guard returns the text plus what it removed/changed, so the narrative record can say so.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any

# --- 1. regulator as subject ------------------------------------------------------------------

# Patterns are matched on accent-folded, lower-cased text.
REGULATORS: dict[str, dict[str, Any]] = {
    "bcb": {"names": (r"banco central(?: do brasil)?", r"\bbcb\b", r"\bbacen\b", r"\bbc\b"),
            "labels": ("bcb",)},
    "cvm": {"names": (r"\bcvm\b", r"comissao de valores mobiliarios"), "labels": ("cvm",)},
    "cade": {"names": (r"\bcade\b", r"conselho administrativo de defesa economica"),
             "labels": ("cade",)},
    "susep": {"names": (r"\bsusep\b", r"superintendencia de seguros privados"), "labels": ("susep",)},
    "previc": {"names": (r"\bprevic\b",), "labels": ("previc",)},
    "ans": {"names": (r"\bans\b", r"agencia nacional de saude suplementar"), "labels": ("ans",)},
    "anpd": {"names": (r"\banpd\b",), "labels": ("anpd",)},
}
# Lenses whose rows are the regulator's OWN act (a BCB authorisation, a CVM rule, a CADE ruling,
# a DOU act). Data lenses (market/pix/juros/inf_diario/funds) carry a regulator's NAME as their
# publisher ("BCB-IFDATA") without the regulator having done anything: they never count.
_OFFICIAL_LENSES = frozenset({"regulatory", "entrants", "dou", "cvm_normas", "sanctions",
                              "antitrust", "ofertas"})
# Verbs a regulator performs (3rd person, present/past/future/periphrastic), folded.
_ACTION = (r"(?:assum\w*|decret\w*|interv\w*|liquid\w*|multa\w*|aplic\w*|autoriz\w*|aprov\w*|"
           r"determin\w*|proib\w*|proibe|suspend\w*|suspen\w*|conden\w*|pun\w*|abre|abriu|abrira|abriram|"
           r"investig\w*|cass\w*|bloque\w*|sancion\w*|instaur\w*|tom\w* (?:o )?controle|vet\w*|"
           r"impo\w*|impos\w*|exig\w*|orden\w*|fech\w*|encerr\w*|afast\w*|declar\w*|"
           r"homolog\w*|julg\w*|arquiv\w*|notific\w*|intim\w*|regulament\w*|edit\w*)")
# "<regulator> [do Brasil|,] <up to 2 words> <verb>" — subject position only. "à CVM", "pela CVM",
# "monitorado pelo Banco Central" (passive / object) do not match.
# "à" (crase) is kept as "@" before folding: "à CVM" is an object, "A CVM" is a subject.
_OBJECT_MARKERS = re.compile(r"(?:(?:@|\bao|\bpela|\bpelo|\bpara|\bna|\bno|\bda|\bdo|\bjunto)\s+)$")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-ZÁÉÍÓÚÂÊÔÃÕÇ\"“(])")


def _fold(s: Any) -> str:
    return unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()


def _source_text(src: dict[str, Any]) -> str:
    keys = ("title", "subject", "name", "company", "institution", "issuer", "organ", "authority",
            "summary", "headline", "ementa", "doc_type", "fund_name", "description")
    return _fold(" ".join(str(src.get(k) or "") for k in keys))


def regulators_supported(sources: list[dict[str, Any]]) -> set[str]:
    """Regulators that a source actually involves: named in a source's own text, or the
    publisher of an official-act row (not of a data row)."""
    out: set[str] = set()
    for s in sources or []:
        if not isinstance(s, dict):
            continue
        text = _source_text(s)
        label = _fold(s.get("source"))
        official = s.get("_lens") in _OFFICIAL_LENSES
        for key, spec in REGULATORS.items():
            if any(re.search(p, text) for p in spec["names"]):
                out.add(key)
            elif official and any(label.startswith(l) or ("-" + l) in label for l in spec["labels"]):
                out.add(key)
    return out


def acting_regulators(sentence: str) -> set[str]:
    """Regulators the sentence names in SUBJECT position followed by an action verb."""
    folded = _fold(str(sentence or "").replace("à", "@").replace("À", "@"))
    found: set[str] = set()
    for key, spec in REGULATORS.items():
        for p in spec["names"]:
            for m in re.finditer(p, folded):
                if _OBJECT_MARKERS.search(folded[max(0, m.start() - 8):m.start()]):
                    continue
                tail = folded[m.end():m.end() + 60]
                tail = re.sub(r"^\s*(?:\([^)]{0,40}\)|do brasil)?\s*,?\s*", "", tail)
                if re.match(r"(?:[\w,]+\s+){0,3}" + _ACTION + r"\b", tail):
                    found.add(key)
    return found


def drop_unsupported_regulator_claims(text: str, sources: list[dict[str, Any]]) -> tuple[str, list[str]]:
    supported = regulators_supported(sources)
    kept, dropped = [], []
    for sent in _SENTENCE_SPLIT.split(text or ""):
        if acting_regulators(sent) - supported:
            dropped.append(sent)
        else:
            kept.append(sent)
    return " ".join(kept), dropped


# --- 2. past dates in the future tense ----------------------------------------------------------

_MONTHS = {"janeiro": 1, "fevereiro": 2, "marco": 3, "abril": 4, "maio": 5, "junho": 6, "julho": 7,
           "agosto": 8, "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12}
_DATE_PT = re.compile(r"\b(\d{1,2})(?:º|o)? de (%s)(?: de (\d{4}))?" % "|".join(_MONTHS))
_DATE_NUM = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")
_DATE_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
# Simple future 3rd person (divulgará, será, fará, divulgarão, serão) and "vai/vão/irá + infinitive".
# Matched on the ACCENTED lower-case text: folding would make "financeira"/"brasileira" look like
# "-erá" futures (the first replay dropped 40 TBF/TR sentences for exactly that), and the plural
# needs a vowel before "rão" so "padrão" is not a verb.
_FUTURE = re.compile(r"(?<!\w)(?:\w*[aei]rá|\w*[aei]rão|vai \w+r|vão \w+r|irá \w+r)(?!\w)")
_URL = re.compile(r"https?://\S+")


def _dates_in(sentence: str, year_hint: int) -> list[str]:
    s = _fold(_URL.sub(" ", sentence))
    out = []
    for d, mon, y in _DATE_PT.findall(s):
        out.append("%04d-%02d-%02d" % (int(y) if y else year_hint, _MONTHS[mon], int(d)))
    for d, m, y in _DATE_NUM.findall(s):
        if 1 <= int(m) <= 12:
            out.append("%04d-%02d-%02d" % (int(y), int(m), int(d)))
    for y, m, d in _DATE_ISO.findall(s):
        out.append(f"{y}-{m}-{d}")
    return out


def drop_stale_future(text: str, run_date: str) -> tuple[str, list[str]]:
    """Drop sentences that put a date BEFORE ``run_date`` in the future tense."""
    year = int(run_date[:4])
    kept, dropped = [], []
    for sent in _SENTENCE_SPLIT.split(text or ""):
        past = [d for d in _dates_in(sent, year) if d < run_date]
        if past and _FUTURE.search(_URL.sub(" ", sent).lower()):
            dropped.append(sent)
        else:
            kept.append(sent)
    return " ".join(kept), dropped


# --- 3. SEC glossed as CVM ----------------------------------------------------------------------

_SEC_AS_CVM = re.compile(
    r"Comiss[ãa]o de Valores Mobili[áa]rios\s*\(\s*SEC\s*\)"
    r"|SEC\s*\(\s*Comiss[ãa]o de Valores Mobili[áa]rios\s*\)"
    r"|CVM\s*\(\s*SEC\s*\)|SEC\s*\(\s*CVM\s*\)", re.IGNORECASE)
SEC_NAME = "SEC (a comissão de valores mobiliários dos EUA)"


def fix_sec_cvm(text: str) -> tuple[str, int]:
    return _SEC_AS_CVM.subn(SEC_NAME, text or "")


# --- all ----------------------------------------------------------------------------------------

def apply(text: str, sources: list[dict[str, Any]], run_date: str) -> dict[str, Any]:
    """Run every guard. ``text`` is "" when nothing survived (the caller falls back)."""
    text, n_sec = fix_sec_cvm(text)
    text, reg = drop_unsupported_regulator_claims(text, sources)
    text, stale = drop_stale_future(text, run_date)
    return {"text": text.strip(), "dropped_regulator": reg, "dropped_stale_future": stale,
            "sec_fixed": n_sec}
