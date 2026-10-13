"""Filing HTML -> plain text with one line per block element.

Line breaks follow the document's block structure (paragraphs, divs, table rows), so item
headings land at line starts and can be found reliably. Hidden inline-XBRL header content is
dropped. Character offsets into the returned text are stable and used for citations.
"""

from __future__ import annotations

import re
from typing import cast

import lxml.html

_BLOCK_TAGS = frozenset(
    {
        "p", "div", "tr", "li", "ul", "ol", "table", "br", "hr", "section",
        "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "center",
    }
)  # fmt: skip
_CELL_TAGS = frozenset({"td", "th"})
_HIDDEN = re.compile(r"display\s*:\s*none", re.I)
_INLINE = re.compile(r"display\s*:\s*inline(?!-block)", re.I)
_SPACES = re.compile(r"[ \t\u00a0\u2002\u2003\u2009\u200b]+")


def html_to_text(html: bytes) -> str:
    doc = cast(lxml.html.HtmlElement, lxml.html.document_fromstring(html))
    for el in [*doc.iter()]:
        tag = el.tag if isinstance(el.tag, str) else ""
        drop = (
            tag in ("script", "style", "head", "title")
            or tag.endswith("header")  # ix:header (inline XBRL hidden facts)
            or _HIDDEN.search(el.get("style", "") or "") is not None
        )
        if drop and el.getparent() is not None:
            el.drop_tree()
    for el in doc.iter():
        tag = el.tag if isinstance(el.tag, str) else ""
        if tag in _BLOCK_TAGS and not _INLINE.search(el.get("style", "") or ""):
            el.tail = "\n" + (el.tail or "")
            if tag in ("p", "div", "li"):
                el.text = "\n" + (el.text or "")
        elif tag in _CELL_TAGS:
            el.tail = " " + (el.tail or "")
    raw = doc.text_content()
    lines = (_SPACES.sub(" ", line).strip() for line in raw.splitlines())
    return "\n".join(line for line in lines if line)
