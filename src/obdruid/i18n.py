"""Translations of the web UI texts, shared with the browser.

English texts are the keys. web/static/<lang>.json holds
  strings:  {english: translation}; texts may contain {named} placeholders
  patterns: [[regex, replacement]] for texts with variable parts ($1, $2 = groups,
            themselves translated when they have an entry)
  words:    word replacements for short summaries (used by the browser only)
The browser loads the same file, so there is one dictionary for both sides.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from .core import dtc_i18n

STATIC_DIR = Path(__file__).parent / "web" / "static"
LANGUAGES = ("en", "pl")


def normalize(lang: str | None) -> str:
    lang = (lang or "en").lower()[:2]
    return lang if lang in LANGUAGES else "en"


@lru_cache(maxsize=None)
def catalog(lang: str) -> dict:
    if lang == "en":
        return {"strings": {}, "patterns": []}
    data = json.loads((STATIC_DIR / f"{lang}.json").read_text(encoding="utf-8"))
    data["patterns"] = [(re.compile(pattern), to) for pattern, to in data.get("patterns", [])]
    return data


def tr(text, lang: str = "en", **params) -> str:
    """Translate a text (or template); unknown texts come back unchanged."""
    if not isinstance(text, str) or not text:
        return text
    data = catalog(normalize(lang))
    out = data["strings"].get(text)
    if out is None:
        out = text
        for pattern, to in data["patterns"]:
            match = pattern.match(text)
            if match:
                out = re.sub(r"\$(\d)", lambda m: tr(match.group(int(m.group(1))), lang), to)
                break
        else:
            out = dtc_i18n.translate(text, normalize(lang))  # trouble code descriptions; others stay as they are
    if params:
        out = re.sub(r"\{(\w+)\}", lambda m: str(params.get(m.group(1), m.group(0))), out)
    return out


def finding_text(finding: dict, lang: str = "en") -> dict:
    """Title, detail, area and value of a health finding (as a dict) in the given language."""
    template = finding.get("title_t")
    if template:
        params = {k: ", ".join(tr(x, lang) for x in v) if isinstance(v, list) else tr(v, lang)
                  for k, v in (finding.get("params") or {}).items()}
        title = tr(template, lang, **params)
        detail = tr(finding.get("detail_t") or "", lang, **params)
    else:  # reports saved before templates existed
        title, detail = tr(finding.get("title", ""), lang), tr(finding.get("detail", ""), lang)
    return {"title": title, "detail": detail, "area": tr(finding.get("area", ""), lang),
            "value": tr(finding.get("value", ""), lang)}
