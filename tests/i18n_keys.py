"""Collect every English text of the web UI that needs a translation (used by test_i18n.py)."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

STATIC = Path(__file__).parent.parent / "src" / "obdruid" / "web" / "static"
ATTRS = ("title", "placeholder", "aria-label")
VOID = {"meta", "link", "input", "br", "img", "hr"}


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


class _Collector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.keys: set[str] = set()
        self.stack: list[str] = []
        self.skip = 0          # inside <script>/<style>/<head>
        self.rich: list[str] | None = None   # collecting the inner HTML of a data-i18n-html element
        self.rich_depth = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if self.rich is not None:
            self.rich.append(f"<{tag}>")
            if tag not in VOID:
                self.rich_depth += 1
            return
        if tag in ("script", "style", "head"):
            self.skip += 1
        for name in ATTRS:
            if attrs.get(name):
                self.keys.add(_squash(attrs[name]))
        if "data-i18n-html" in attrs:
            self.rich, self.rich_depth = [], 1

    def handle_endtag(self, tag):
        if self.rich is not None:
            self.rich_depth -= 1
            if self.rich_depth == 0:
                self.keys.add(_squash("".join(self.rich)))
                self.rich = None
            else:
                self.rich.append(f"</{tag}>")
            return
        if tag in ("script", "style", "head"):
            self.skip -= 1

    def handle_data(self, data):
        if self.rich is not None:
            # the browser's innerHTML keeps these three escaped
            self.rich.append(data.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                             .replace("\xa0", "&nbsp;"))
        elif not self.skip and _squash(data):
            self.keys.add(_squash(data))


def html_keys() -> set[str]:
    c = _Collector()
    c.feed((STATIC / "index.html").read_text(encoding="utf-8"))
    return {k for k in c.keys if re.search(r"[A-Za-z]{2}", k) and k not in HTML_IGNORE}


# texts of index.html that stay the same in every language
HTML_IGNORE = {"OBDT1-…"}  # placeholder showing the supporter key format


_JS_STRING = re.compile(r'"((?:[^"\\\n]|\\.)*)"')
# strings that are code, not text: selectors, css classes, keys, urls, units...
_NOT_TEXT = re.compile(r"^([#.\[/$]|https?:|[a-z0-9_.:-]+$|[A-Z0-9_]+$|obd\.|use strict|Content-Type|text/|"
                       r"application/|en-US|pl-PL|2d$|http://www\.w3\.org)")


# string literals of app.js that are code (css classes, selectors), not text
JS_IGNORE = {"btn btn-danger", "btn btn-primary", "card all-clear", "card freeze", "card health-summary",
             "mon-bar none", "mono muted", "muted group-note", "script, style, [data-i18n-html]", "table wrap",
             "viewBox", "not supported", "should work", "{email}", "Bluetooth", "BLE"}
# lower-case words the script shows that the literal filter cannot tell from code
JS_EXTRA = {"complete", "incomplete", "simulator", "ON", "off", "petrol", "diesel", "lpg", "OK"}


def js_keys() -> set[str]:
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    source = re.sub(r"^\s*//.*$", "", source, flags=re.M)
    keys = set()
    for raw in _JS_STRING.findall(source):
        text = raw.replace('\\"', '"').replace("\\n", "\n")
        if not re.search(r"[A-Za-z]{2}", text) or _NOT_TEXT.match(text) or "<path" in text or "\\" in text:
            continue
        if text in JS_IGNORE or text.endswith((" ", "-")) or text.startswith(" "):
            continue
        keys.add(text)
    return keys | JS_EXTRA
