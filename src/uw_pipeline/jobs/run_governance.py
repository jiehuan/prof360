"""`governance` stage: board composition from the latest proxy statement (decisions D-045).

Both models read the board passages of the proxy (DEF 14A). A fact is kept only where its
quote is found in the proxy, a number or name in it appears in the quote, and both models give
the same value; otherwise it is NULL. Stored in `board_composition`; the signals stage turns it
into governance signals and the insight package carries it as a source. board_v2 (Phase 15f,
D-050) adds the auditor fee table, dual-class shares and the CEO's start year.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Connection, Engine, desc, select

from uw_pipeline.clients.llm import LLMClient
from uw_pipeline.clients.storage import Storage
from uw_pipeline.config import CompanyConfig
from uw_pipeline.enrich.grounding import amount_in, is_grounded, normalize
from uw_pipeline.enrich.llm import PROMPTS_DIR, LLMConfig, cached_call
from uw_pipeline.enrich.schemas import BoardFacts, strict_json_schema
from uw_pipeline.enrich.text_metrics import select_chunks
from uw_pipeline.enrich.untrusted import data_block
from uw_pipeline.jobs.run_company import TaskResult
from uw_pipeline.jobs.run_xbrl import company_row
from uw_pipeline.load.upsert import upsert
from uw_pipeline.normalize.html_text import html_to_text
from uw_pipeline.normalize.sections import chunk
from uw_pipeline.schema import board_composition, source_document

log = logging.getLogger(__name__)

PROMPT_VERSION = "board_v2"
SCHEMA = strict_json_schema(BoardFacts)
MAX_CHARS = 55_000
KEYWORDS = [
    ["independent"],
    ["lead independent director", "presiding director", "independent chair"],
    ["chair of the board", "chairman of the board", "board chair", "executive chair"],
    ["committee", "chair"],
    ["director nominees", "nominees for director"],
    ["audit fees", "audit-related fees", "tax fees", "all other fees"],
    ["class b", "votes per share", "one vote", "dual-class", "dual class"],
    ["chief executive officer since", "ceo since", "officer (since", "served as our president",
     "became chief executive", "named chief executive", "appointed chief executive",
     "hiring of our ceo"],
]  # fmt: skip
_WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
          "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
          "eighteen", "nineteen", "twenty"]  # fmt: skip


def _number_in(n: int, quote: str) -> bool:
    q = normalize(quote)
    words = [str(n)] + ([_WORDS[n]] if n < len(_WORDS) else [])
    return any(re.search(rf"(?<![\w.]){re.escape(w)}(?![\w.])", q) for w in words)


def _name_in(name: str, quote: str) -> bool:
    """The surname (last word) of a name appears in the quote."""
    parts = re.findall(r"[A-Za-z][A-Za-z'\-]+", name)
    return bool(parts) and parts[-1].lower() in normalize(quote)


_FEES = ("audit", "audit_related", "tax", "all_other")


def _fees(facts: BoardFacts, ok: Callable[[str], bool]) -> dict[str, Any] | None:
    """The fee table, kept whole only when every amount is in its quote. The year is often a
    column heading outside the quote; both models must give the same year (see `agreed`)."""
    f = facts.fees
    if f is None or f.audit is None or f.fiscal_year is None or not ok(f.quote):
        return None
    amounts = {k: getattr(f, k) for k in _FEES}
    # parts=3: a category the table splits into lines (consolidated and statutory audits).
    bad = [v for v in amounts.values() if v is not None and not amount_in(v, f.quote, parts=3)]
    if bad or any(v is not None and v < 0 for v in amounts.values()):
        return None
    return {"fiscal_year": f.fiscal_year, **amounts}


def _name_near(name: str, quote: str, source: str, before: int = 1_500) -> bool:
    """The surname is in the quote, or in the text just before it (a director's biography
    names the person in its heading, then says "Chief Executive Officer since 2019")."""
    if _name_in(name, quote):
        return True
    src, q = normalize(source), normalize(quote).strip("\"'")
    at = src.find(q[:60])
    return at >= 0 and _name_in(name, src[max(0, at - before) : at + len(q)])


def checked(facts: BoardFacts, source: str) -> dict[str, Any]:
    """The facts whose quote is in the proxy and supports the value; others None."""

    def ok(quote: str) -> bool:
        return bool(quote.strip()) and is_grounded(quote, source)

    lid = facts.lead_independent_director

    def number(value: int | None, quote: str) -> int | None:
        return value if value is not None and ok(quote) and _number_in(value, quote) else None

    out: dict[str, Any] = {
        "board_size": number(facts.board_size, facts.board_size_quote),
        "independent_directors": number(facts.independent_directors, facts.independent_quote),
        "ceo_is_chair": facts.ceo_is_chair
        if facts.ceo_is_chair is not None and ok(facts.ceo_chair_quote) else None,
        "lead_independent_director": None,
        "committees": {},
        "fees": _fees(facts, ok),
        "dual_class": facts.dual_class
        if facts.dual_class is not None and ok(facts.dual_class_quote) else None,
        "ceo_name": None,
        "ceo_since": None,
        "evidence": {},
    }  # fmt: skip
    if facts.ceo_name and ok(facts.ceo_quote) and _name_near(facts.ceo_name, facts.ceo_quote,
                                                             source):  # fmt: skip
        out["ceo_name"] = facts.ceo_name.strip()
        if facts.ceo_since is not None and str(facts.ceo_since) in facts.ceo_quote:
            out["ceo_since"] = facts.ceo_since
    if lid is not None and ok(facts.lead_quote):
        if lid.strip().lower() == "none":
            out["lead_independent_director"] = ""  # confirmed: there is none
        elif _name_in(lid, facts.lead_quote):
            out["lead_independent_director"] = lid.strip()
    for c in facts.committees:
        if c.chair and ok(c.quote) and _name_in(c.chair, c.quote):
            out["committees"][c.name.strip().lower()] = (c.name.strip(), c.chair.strip())
    quotes = {"board_size": facts.board_size_quote, "independent_directors":
              facts.independent_quote, "ceo_is_chair": facts.ceo_chair_quote,
              "lead_independent_director": facts.lead_quote,
              "fees": facts.fees.quote if facts.fees else "",
              "dual_class": facts.dual_class_quote, "ceo_name": facts.ceo_quote}  # fmt: skip
    out["evidence"] = {k: q for k, q in quotes.items() if out[k] is not None}
    return out


def agreed(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Facts both models read the same way (names compared by surname)."""
    out: dict[str, Any] = {}
    for key in ("board_size", "independent_directors", "ceo_is_chair"):
        out[key] = a[key] if a[key] is not None and a[key] == b[key] else None
    la, lb = a["lead_independent_director"], b["lead_independent_director"]
    same = la is not None and lb is not None and (
        la == lb or (la and lb and _name_in(la, lb) and _name_in(lb, la)))  # fmt: skip
    out["lead_independent_director"] = la if same else None
    committees = []
    for key, (name, chair) in a["committees"].items():
        other = b["committees"].get(key)
        if other and _name_in(chair, other[1]):
            committees.append({"name": name, "chair": chair})
    out["committees"] = committees
    fa, fb = a["fees"], b["fees"]
    out["fees"] = fa if fa and fb and fa["fiscal_year"] == fb["fiscal_year"] and all(
        _same_amount(fa[k], fb[k]) for k in _FEES) else None  # fmt: skip
    out["dual_class"] = a["dual_class"] if a["dual_class"] == b["dual_class"] else None
    ca, cb = a["ceo_name"], b["ceo_name"]
    same_ceo = bool(ca and cb and _name_in(ca, cb) and _name_in(cb, ca))
    out["ceo_name"] = ca if same_ceo else None
    out["ceo_since"] = a["ceo_since"] if same_ceo and a["ceo_since"] == b["ceo_since"] else None
    out["evidence"] = {k: q for k, q in a["evidence"].items() if out.get(k) is not None}
    return out


def _same_amount(x: float | None, y: float | None) -> bool:
    if x is None or y is None:
        return x is y
    return abs(x - y) <= max(0.5, max(abs(x), abs(y)) * 0.005)


def _latest_proxy(conn: Connection, company_id: int) -> Any:
    return conn.execute(
        select(source_document)
        .where(source_document.c.company_id == company_id,
               source_document.c.form_type == "DEF 14A",
               source_document.c.raw_path.is_not(None))
        .order_by(desc(source_document.c.filed_at))
        .limit(1)
    ).mappings().first()  # fmt: skip


def process_company(
    cfg: CompanyConfig, client: LLMClient, llm: LLMConfig, storage: Storage, conn: Connection,
    now: datetime,
) -> str:  # fmt: skip
    row = company_row(conn, cfg)
    proxy = _latest_proxy(conn, row["company_id"])
    if proxy is None:
        return "no proxy statement"
    chunks = [c.text for c in chunk(html_to_text(storage.read_bytes(proxy["raw_path"])))]
    # Keywords are matched on collapsed whitespace: table cells break lines ("Audit\nFees").
    flat = [" ".join(c.split()) for c in chunks]
    idx = select_chunks(flat, KEYWORDS, MAX_CHARS, per_group=3, round_robin=True)
    if not idx:
        return "no board passages"
    source = "\n".join(chunks[i] for i in idx)
    user = (f"Company: {row['legal_name']}\nDocument: proxy statement filed {proxy['filed_at']}"
            f"\n\n{data_block('excerpt', source)}")  # fmt: skip
    system = (PROMPTS_DIR / f"{PROMPT_VERSION}.md").read_text(encoding="utf-8")
    reads = []
    for spec in (llm.extractor, llm.tiebreaker):
        raw = cached_call(conn, client, spec, task="board", prompt_version=PROMPT_VERSION,
                          system=system, user=user, schema=SCHEMA, now=now,
                          validate=BoardFacts.model_validate)  # fmt: skip
        if raw is None:
            return "a model gave no usable answer"
        reads.append(checked(BoardFacts.model_validate(raw), source))
    facts = agreed(reads[0], reads[1])
    fees = facts["fees"]
    input_hash = hashlib.sha256(f"{PROMPT_VERSION}\n{user}".encode()).hexdigest()
    upsert(conn, board_composition, [{
        "company_id": row["company_id"], "doc_id": proxy["doc_id"],
        "board_size": facts["board_size"], "independent_directors": facts["independent_directors"],
        "ceo_is_chair": facts["ceo_is_chair"],
        "lead_independent_director": facts["lead_independent_director"],
        "committees_json": json.dumps(facts["committees"]),
        "fees_fiscal_year": fees["fiscal_year"] if fees else None,
        "audit_fees": fees["audit"] if fees else None,
        "audit_related_fees": fees["audit_related"] if fees else None,
        "tax_fees": fees["tax"] if fees else None,
        "other_fees": fees["all_other"] if fees else None,
        "dual_class": facts["dual_class"], "ceo_name": facts["ceo_name"],
        "ceo_since": facts["ceo_since"],
        "evidence_json": json.dumps(facts["evidence"]),
        "model": f"{llm.extractor.model}+{llm.tiebreaker.model}",
        "prompt_version": PROMPT_VERSION, "input_hash": input_hash, "updated_at": now,
    }], key_cols=["company_id"])  # fmt: skip
    keys = ("board_size", "independent_directors", "ceo_is_chair", "lead_independent_director",
            "fees", "dual_class", "ceo_name", "ceo_since")  # fmt: skip
    kept = sum(facts[k] is not None for k in keys)
    return f"{kept}/{len(keys)} facts, {len(facts['committees'])} committee chairs"


def run_governance_stage(
    companies: Sequence[CompanyConfig],
    llm: tuple[LLMClient, LLMConfig] | None,
    storage: Storage,
    engine: Engine,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> list[TaskResult]:
    if llm is None:
        return [TaskResult(c.ticker, "governance", ok=False, detail="LLM not configured")
                for c in companies]  # fmt: skip
    results = []
    for cfg in companies:
        try:
            with engine.begin() as conn:
                detail = process_company(cfg, llm[0], llm[1], storage, conn, now())
            log.info("%s: governance %s", cfg.ticker, detail)
            results.append(TaskResult(cfg.ticker, "governance", ok=True, detail=detail))
        except Exception as exc:
            log.exception("%s: governance stage failed", cfg.ticker)
            results.append(TaskResult(cfg.ticker, "governance", ok=False, detail=str(exc)))
    return results
