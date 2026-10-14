"""Rule-based signal detection. Pure functions over already-loaded documents, sections and
metric values; database I/O lives in jobs/run_signals.py."""

from __future__ import annotations

import itertools
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from uw_pipeline.normalize.sections import auditor_since
from uw_pipeline.signals.rules import KeywordRule, SignalRules, SignalSpec

SNIPPET_CHARS = 600

# Abbreviations that end with a period but don't end a sentence.
_ABBREV = re.compile(r"(?:\b(?:Mr|Ms|Mrs|Dr|Jr|Sr|Inc|Co|Corp|Ltd|No|St|U\.S|vs|etc|[A-Z])\.)$")


def sentences(text: str) -> list[str]:
    """Split into sentences on . ; and line breaks, without splitting after "Mr.", "Jr." etc."""
    out: list[str] = []
    for line in text.split("\n"):
        buf = ""
        for piece in re.split(r"(?<=[.;])\s+", line):
            buf = f"{buf} {piece}" if buf else piece
            if not _ABBREV.search(buf):
                out.append(buf.strip())
                buf = ""
        if buf:
            out.append(buf.strip())
    return [x for x in out if x]


@dataclass(frozen=True)
class Detected:
    rule_id: str
    category: str
    subcategory: str
    severity: str
    title: str
    description: str
    event_date: str
    accession_no: str | None = None
    doc_id: int | None = None
    section_code: str | None = None
    snippet: str | None = None
    key_extra: str = ""  # disambiguates several signals of one rule from one source
    method: str = "RULE"  # RULE | VOTE (decisions D-028)
    review_status: str = "RULE_ONLY"  # RULE_ONLY | UNANIMOUS | MAJORITY | DISPUTED
    votes_json: str | None = None


@dataclass(frozen=True)
class Doc:
    doc_id: int
    form_type: str
    doc_role: str
    accession_no: str
    filed_at: str
    period_end: str | None
    items: str | None


def _from_spec(spec: SignalSpec, rule_id: str, **kw: Any) -> Detected:
    fields = {
        "category": spec.category,
        "subcategory": spec.subcategory,
        "severity": spec.severity,
        "title": spec.title,
    } | kw
    return Detected(rule_id=rule_id, **fields)


def sentence_around(text: str, start: int, end: int, limit: int = SNIPPET_CHARS) -> str:
    """The sentence containing text[start:end], clipped to `limit` characters around it."""
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    line_end = len(text) if line_end == -1 else line_end
    pos = line_start
    for sentence in sentences(text[line_start:line_end]):
        found = text.find(sentence, pos, line_end)
        if found == -1:
            break
        if found <= start < found + len(sentence) + 1:
            if len(sentence) <= limit:
                return sentence
            rel = start - found
            lo = max(0, min(rel - limit // 2, len(sentence) - limit))
            return sentence[lo : lo + limit].strip()
        pos = found + len(sentence)
    return text[max(line_start, start - limit // 2) : min(line_end, end + limit // 2)].strip()


# -- 8-K items ---------------------------------------------------------------------------------


def _role_spans(
    sentence: str, pattern: str, own_names: Sequence[str], own: str
) -> list[tuple[int, int]]:
    """Positions where the sentence names the role at this company, i.e. not
    "CEO of <another entity or business unit>"."""
    names = [n.replace("\u2019", "'") for n in own_names if n]
    own_re = "|".join([own, *(re.escape(n) for n in names)])
    spans = []
    for m in re.finditer(pattern, sentence, re.I):
        after = sentence[m.end() : m.end() + 120]
        of = re.match(r"\s*(?:\(\W*\w+\W*\))?,?\s+of\s+(.*)", after, re.I)
        if of is None or re.match(own_re, of.group(1), re.I):
            spans.append((m.start(), m.end()))
    return spans


def _near(sentence: str, verb: str, span: tuple[int, int], proximity: int) -> bool:
    window = sentence[max(0, span[0] - proximity) : span[1] + proximity]
    return re.search(verb, window, re.I) is not None


def _leadership(
    text: str, rules: SignalRules, own_names: Sequence[str]
) -> list[tuple[str, str, str]]:
    """(role, kind, sentence) where a change verb sits next to a CEO/CFO mention."""
    lr = rules.leadership
    found: list[tuple[str, str, str]] = []
    for sentence in sentences(text):
        sentence = sentence.replace("\u2019", "'")
        if re.search(lr.exclude, sentence, re.I):
            continue
        for role, pattern in lr.roles.items():
            spans = _role_spans(sentence, pattern, own_names, lr.own_entity)
            if any(_near(sentence, lr.departure, sp, lr.proximity) for sp in spans):
                found.append((role, "departure", sentence.strip()))
            elif any(_near(sentence, lr.appointment, sp, lr.proximity) for sp in spans):
                found.append((role, "appointment", sentence.strip()))
    return found


def eight_k_signals(
    docs: Iterable[Doc],
    sections: Mapping[int, Mapping[str, str]],
    rules: SignalRules,
    own_names: Sequence[str] = (),
) -> list[Detected]:
    """`own_names`: names the company goes by in filings (e.g. "Ford", "Marriott")."""
    out: list[Detected] = []
    for doc in docs:
        if not doc.form_type.startswith("8-K") or doc.doc_role != "PRIMARY":
            continue
        for item in (i.strip() for i in (doc.items or "").split(",")):
            spec = rules.eight_k_items.get(item)
            if spec is None:
                continue
            body = sections.get(doc.doc_id, {}).get(f"8K-{item}")
            snippet = body[:SNIPPET_CHARS] if body else None
            base = {
                "event_date": doc.filed_at,
                "accession_no": doc.accession_no,
                "doc_id": doc.doc_id,
                "section_code": f"8K-{item}" if body else None,
            }
            changes = _leadership(body, rules, own_names) if item == "5.02" and body else []
            if changes:
                # One signal per role: departure and appointment together are a succession.
                by_role: dict[str, tuple[str, str]] = {}
                for role, kind, sentence in changes:
                    if role not in by_role:
                        by_role[role] = (kind, sentence)
                    elif by_role[role][0] not in (kind, "succession"):
                        evidence = sentence if kind == "departure" else by_role[role][1]
                        by_role[role] = ("succession", evidence)
                for role, (kind, sentence) in sorted(by_role.items()):
                    out.append(
                        _from_spec(
                            spec,
                            f"8k_{item}_{role.lower()}_{kind}",
                            severity=rules.leadership.severity[kind],  # type: ignore[index]
                            title=f"{role} {kind}",
                            description=f"8-K Item {item}: {role} {kind}.",
                            snippet=sentence[:SNIPPET_CHARS],
                            key_extra=role,
                            **base,
                        )
                    )
                continue
            dg = spec.downgrade
            if dg and body and re.search(dg.pattern, body, re.I):
                m = re.search(dg.pattern, body, re.I)
                assert m is not None
                out.append(
                    _from_spec(
                        spec,
                        f"8k_{item}",
                        severity=dg.severity,
                        subcategory=dg.subcategory,
                        title=dg.title,
                        description=f"8-K Item {item}: {dg.title}.",
                        snippet=sentence_around(body, m.start(), m.end()),
                        **base,
                    )
                )
                continue
            out.append(
                _from_spec(
                    spec,
                    f"8k_{item}",
                    description=f"8-K Item {item}: {spec.title}.",
                    snippet=snippet,
                    **base,
                )
            )
    return out


# -- governance --------------------------------------------------------------------------------


@dataclass(frozen=True)
class AuditorInfo:
    doc: Doc
    name: str | None  # dei:AuditorName
    since: int | None  # "served as auditor since YYYY"
    snippet: str | None


def _norm_firm(name: str) -> str:
    return re.sub(r"[^a-z]", "", name.lower().replace("llp", ""))


def auditor_signals(infos: Sequence[AuditorInfo]) -> list[Detected]:
    """Current auditor and tenure from the latest 10-K, and a change between 10-Ks."""
    dated = sorted((i for i in infos if i.name or i.since), key=lambda i: i.doc.filed_at)
    if not dated:
        return []
    out: list[Detected] = []
    cur = dated[-1]
    tenure = f" (since {cur.since})" if cur.since else ""
    out.append(
        Detected(
            rule_id="auditor",
            category="Governance & Leadership",
            subcategory="Auditor",
            severity="Info",
            title=f"Auditor: {cur.name or 'not tagged'}{tenure}",
            description=(
                f"Independent auditor per the {cur.doc.form_type} filed {cur.doc.filed_at}"
                f"{f'; auditor since {cur.since}' if cur.since else ''}."
            ),
            event_date=cur.doc.filed_at,
            accession_no=cur.doc.accession_no,
            doc_id=cur.doc.doc_id,
            section_code="10K-AUDIT" if cur.snippet else None,
            snippet=cur.snippet,
        )
    )
    named = [i for i in dated if i.name]
    for prev, nxt in itertools.pairwise(named):
        assert prev.name and nxt.name
        if _norm_firm(prev.name) != _norm_firm(nxt.name):
            out.append(
                Detected(
                    rule_id="auditor_change",
                    category="Governance & Leadership",
                    subcategory="Auditor Change",
                    severity="Warning",
                    title=f"Auditor changed from {prev.name} to {nxt.name}",
                    description=(
                        f"dei:AuditorName changed between the 10-Ks filed {prev.doc.filed_at} "
                        f"and {nxt.doc.filed_at}."
                    ),
                    event_date=nxt.doc.filed_at,
                    accession_no=nxt.doc.accession_no,
                    doc_id=nxt.doc.doc_id,
                )
            )
    return out


def auditor_info(doc: Doc, name: str | None, audit_text: str | None) -> AuditorInfo:
    since = auditor_since(audit_text) if audit_text else None
    snippet = None
    if audit_text and since:
        m = re.search(rf"[^.\n]*since (?:at least )?{since}[^.\n]*\.?", audit_text)
        snippet = m.group(0).strip() if m else None
    return AuditorInfo(doc, name, since, snippet)


# -- keywords ----------------------------------------------------------------------------------


def _keyword_hit(rule: KeywordRule, text: str) -> tuple[int, int, str] | None:
    for m in re.finditer(rule.pattern, text, re.I):
        sentence = sentence_around(text, m.start(), m.end())
        if rule.exclude and re.search(rule.exclude, sentence, re.I):
            continue
        return m.start(), m.end(), sentence
    return None


def keyword_signals(
    docs: Iterable[Doc], sections: Mapping[int, Mapping[str, str]], rules: SignalRules
) -> list[Detected]:
    out: list[Detected] = []
    for doc in docs:
        if doc.doc_role != "PRIMARY" or doc.form_type not in ("10-K", "10-Q"):
            continue
        for rule in rules.keywords:
            for code, text in sorted(sections.get(doc.doc_id, {}).items()):
                if not any(code.startswith(p) for p in rule.sections):
                    continue
                hit = _keyword_hit(rule, text)
                if hit is None:
                    continue
                out.append(
                    _from_spec(
                        rule,
                        rule.id,
                        description=f"{rule.title} ({doc.form_type} filed {doc.filed_at}).",
                        event_date=doc.filed_at,
                        accession_no=doc.accession_no,
                        doc_id=doc.doc_id,
                        section_code=code,
                        snippet=hit[2],
                    )
                )
                break  # one signal per rule per document
    return out


# -- metrics -----------------------------------------------------------------------------------


def metric_signals(values: Sequence[Mapping[str, Any]], rules: SignalRules) -> list[Detected]:
    """`values`: the company's quarterly metric_value rows."""
    quarters = sorted(
        {(v["fiscal_year"], v["fiscal_quarter"]) for v in values if v["period_type"] == "Q"}
    )
    if not quarters:
        return []
    by_key = {
        (v["metric_code"], v["fiscal_year"], v["fiscal_quarter"]): v
        for v in values
        if v["period_type"] == "Q"
    }
    # The latest quarter with an equity value: a newer quarter that only has PRELIMINARY rows
    # from an earnings release (no balance sheet yet) must not make the signal disappear.
    with_equity = [q for q in quarters
                   if (by_key.get(("equity", *q)) or {}).get("value") is not None]  # fmt: skip
    if not with_equity:
        return []
    latest = with_equity[-1]
    quarters = [q for q in quarters if q <= latest]
    equity = by_key[("equity", *latest)]
    if equity["value"] > 0:
        return []
    label = f"FY{latest[0]} Q{latest[1]}"
    spec = rules.metrics.negative_equity
    out = [
        _from_spec(
            spec,
            "negative_equity",
            description=(
                f"Stockholders' equity was {equity['value'] / 1e6:,.0f}M USD at {label} "
                f"({equity['period_end']}); debt-to-equity is reported as N/M."
            ),
            event_date=equity["period_end"],
            accession_no=equity["source_accn"],
            key_extra=label,
        )
    ]
    ttm = [by_key.get(("buybacks", *q)) for q in quarters[-4:]]
    total = sum(v["value"] for v in ttm if v is not None and v["value"] is not None)
    bb = rules.metrics.buybacks_with_negative_equity
    if len(quarters) >= 4 and total > bb.min_buybacks_ttm:
        out.append(
            _from_spec(
                bb,
                "buybacks_with_negative_equity",
                description=(
                    f"Share repurchases of {total / 1e6:,.0f}M USD over the four quarters to "
                    f"{label} while stockholders' equity is negative."
                ),
                event_date=equity["period_end"],
                accession_no=equity["source_accn"],
                key_extra=label,
            )
        )
    return out


def pay_signals(values: Sequence[Mapping[str, Any]], rules: SignalRules) -> list[Detected]:
    """CEO pay up while shareholder return fell, in the latest proxy year (decisions D-042).

    `values`: the company's fiscal-year metric_value rows. TSR is the value of a fixed $100
    investment, so a lower value than the year before means a negative return that year."""
    spec = rules.metrics.pay_vs_performance
    if spec is None:
        return []
    by_key = {(v["metric_code"], v["fiscal_year"]): v for v in values
              if v["period_type"] == "FY" and v["value"] is not None}  # fmt: skip
    years = sorted(fy for code, fy in by_key if code == "ceo_total_comp")
    if not years:
        return []
    fy = years[-1]
    pay, pay0 = by_key.get(("ceo_total_comp", fy)), by_key.get(("ceo_total_comp", fy - 1))
    tsr, tsr0 = by_key.get(("tsr_value_100", fy)), by_key.get(("tsr_value_100", fy - 1))
    if not (pay and pay0 and tsr and tsr0) or pay0["value"] <= 0:
        return []
    growth = pay["value"] / pay0["value"] - 1
    if growth < spec.min_pay_growth or tsr["value"] >= tsr0["value"]:
        return []
    return [
        _from_spec(
            spec,
            "pay_vs_performance",
            description=(
                f"CEO total compensation rose {growth:.0%} in FY{fy} "
                f"({pay0['value'] / 1e6:,.1f}M to {pay['value'] / 1e6:,.1f}M USD) while the value "
                f"of $100 invested in the stock fell from {tsr0['value']:,.2f} to "
                f"{tsr['value']:,.2f} (pay-versus-performance table, proxy statement)."
            ),
            event_date=pay["period_end"],
            accession_no=pay["source_accn"],
            key_extra=f"FY{fy}",
        )
    ]
