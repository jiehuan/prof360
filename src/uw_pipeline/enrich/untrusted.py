"""Filing and news text is data, never instructions (decisions D-044).

Two layers, both mechanical:
- `neutralize`: invisible and direction-changing characters (zero-width, bidi overrides, other
  control and format characters) become visible markers such as "[U+202E]", so hidden text
  cannot steer the model and a reviewer sees what the model saw. Applied to every user prompt
  in `enrich.llm.cached_call`.
- `data_block`: wraps one source in a named block and escapes that block's own tags inside the
  text, so the text cannot close the block and continue as prompt.
The task rules live in the system prompt, with `prompts/untrusted_data_v1.md` appended to it,
which is the higher-priority instruction boundary. Whatever the model then answers still has
to pass the quote, number, cell and identity checks.
"""

from __future__ import annotations

import re
import unicodedata

GUARD_PROMPT = "untrusted_data_v1"
_KEEP = {"\n", "\t"}


def neutralize(text: str) -> str:
    """Control (Cc) and format (Cf) characters other than newline and tab as visible markers."""
    if text.isascii() and all(c >= " " or c in _KEEP for c in text):
        return text
    out = []
    for ch in text:
        if ch not in _KEEP and unicodedata.category(ch) in ("Cc", "Cf"):
            out.append(f"[U+{ord(ch):04X}]")
        else:
            out.append(ch)
    return "".join(out)


def data_block(name: str, text: str, **attrs: str) -> str:
    """`<name attr="...">text</name>` with any `<name` / `</name` inside the text escaped."""
    escaped = re.sub(rf"<(/?)({re.escape(name)})\b", r"&lt;\1\2", text, flags=re.I)
    head = "".join(f' {k}="{one_line(v).replace(chr(34), chr(39))}"' for k, v in attrs.items())
    return f"<{name}{head}>\n{escaped}\n</{name}>"


def one_line(text: str) -> str:
    """A field of a one-item-per-line list: line breaks inside it cannot start a fake item."""
    return " ".join(text.split())
