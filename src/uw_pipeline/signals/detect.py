"""Rule-based signal detection. Pure functions over already-loaded documents, sections and
metric values; database I/O lives in jobs/run_signals.py."""

from __future__ import annotations

import itertools
import re
from bisect import bisect_right
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from uw_pipeline.normalize.sections import auditor_since
from uw_pipeline.signals.rules import (
    LEGAL_TYPES,
    KeywordRule,
    SignalRules,
    SignalSpec,
    ownership_rule_id,
)

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
    firm_id: str | None = None  # dei:AuditorFirmId (PCAOB ID), Phase 15f


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
                f"{f' (PCAOB ID {cur.firm_id})' if cur.firm_id else ''}"
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
        # The PCAOB firm ID when both 10-Ks give it; a firm's name can be written differently.
        changed = (prev.firm_id != nxt.firm_id) if prev.firm_id and nxt.firm_id \
            else _norm_firm(prev.name) != _norm_firm(nxt.name)  # fmt: skip
        if changed:
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


def auditor_info(
    doc: Doc, name: str | None, audit_text: str | None, firm_id: str | None = None
) -> AuditorInfo:
    since = auditor_since(audit_text) if audit_text else None
    snippet = None
    if audit_text and since:
        m = re.search(rf"[^.\n]*since (?:at least )?{since}[^.\n]*\.?", audit_text)
        snippet = m.group(0).strip() if m else None
    return AuditorInfo(doc, name, since, snippet, firm_id)


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


def ownership_signals(
    docs: Iterable[Doc], rules: SignalRules, filers: Mapping[str, str] | None = None
) -> list[Detected]:
    """Schedule 13D filings others made about the company (Phase 15d): activist stakes.
    `filers`: accession -> the investor's name (from the filing's index headers)."""
    out = []
    for d in docs:
        spec = rules.ownership_forms.get(d.form_type)
        if spec is None:
            continue
        out.append(_from_spec(
            spec, ownership_rule_id(d.form_type),
            description=f"{(filers or {}).get(d.accession_no) or 'An investor'} filed a "
                        f"{d.form_type} about the company on {d.filed_at} (accession "
                        f"{d.accession_no}).",
            event_date=d.filed_at, accession_no=d.accession_no, doc_id=d.doc_id,
        ))  # fmt: skip
    return out


def market_signals(
    prices: Sequence[tuple[str, float]],
    benchmark: Sequence[tuple[str, float]],
    earnings: Sequence[Mapping[str, Any]],
    rules: SignalRules,
    today: str,
) -> list[Detected]:
    """Stock drops beyond the market and drops after an earnings miss (Phase 15a), within the
    lookback. `prices`, `benchmark`: (date, adjusted close), any order; `earnings`: rows of
    earnings_report. Every signal can be checked against the stored prices."""
    m = rules.market
    since = (date.fromisoformat(today) - timedelta(days=m.lookback_days)).isoformat()
    days = sorted(prices)
    bench = dict(benchmark)
    bench_days = sorted(bench)
    out: list[Detected] = []
    if m.stock_drop is not None:
        rule = m.stock_drop
        for (d0, p0), (d1, p1) in itertools.pairwise(days):
            if d1 < since or p0 <= 0:
                continue
            ret = p1 / p0 - 1
            j = bisect_right(bench_days, d1) - 1
            if ret > -rule.min_drop or j < 1 or bench_days[j] != d1:
                continue
            bret = bench[d1] / bench[bench_days[j - 1]] - 1
            if ret - bret <= -rule.min_excess:
                out.append(_from_spec(
                    rule, "stock_drop",
                    description=f"Shares fell {-ret:.1%} on {d1} (close {p0:,.2f} on {d0} to "
                                f"{p1:,.2f}, adjusted) while {m.benchmark} moved {bret:+.1%}.",
                    event_date=d1, key_extra=d1,
                ))  # fmt: skip
    if m.earnings_miss_drop is not None:
        rule2 = m.earnings_miss_drop
        dates = [d for d, _ in days]
        for e in earnings:
            rep, est, when = e.get("reported_eps"), e.get("estimated_eps"), e.get("reported_date")
            if rep is None or est is None or not when or when < since or rep >= est:
                continue
            base = bisect_right(dates, when) - 1  # the last close before the report day
            if base >= 0 and dates[base] == when:
                base -= 1
            end = base + rule2.days
            if base < 0 or end >= len(days) or days[base][1] <= 0:
                continue
            fall = days[end][1] / days[base][1] - 1
            if fall <= -rule2.min_drop:
                out.append(_from_spec(
                    rule2, "earnings_miss_drop",
                    description=f"EPS {rep} missed the estimate of {est} (quarter ended "
                                f"{e['fiscal_date_ending']}, reported {when}); shares fell "
                                f"{-fall:.1%} from {dates[base]} to {dates[end]}.",
                    event_date=when, key_extra=str(e["fiscal_date_ending"]),
                ))  # fmt: skip
    return out


def insider_signals(
    trades: Sequence[Mapping[str, Any]],
    later: Sequence[Detected],
    rules: SignalRules,
    today: str,
) -> list[Detected]:
    """Clusters of officers and directors selling on the open market (Phase 15b). `trades`:
    insider_transaction rows; `later`: market signals (a stock drop or an earnings-miss drop
    soon after a cluster makes it a Warning)."""
    rule = rules.insiders.insider_cluster_selling
    if rule is None:
        return []
    since = (date.fromisoformat(today) - timedelta(days=rule.lookback_days)).isoformat()
    sales = sorted(
        (r for r in trades if r["code"] == "S" and (r["is_officer"] or r["is_director"])
         and not r["is_10b5_1"] and r["trade_date"] and r["trade_date"] >= since),
        key=lambda r: r["trade_date"],
    )  # fmt: skip
    events = sorted((s.event_date, s.title) for s in later
                    if s.rule_id in ("stock_drop", "earnings_miss_drop"))  # fmt: skip

    def who(r: Mapping[str, Any]) -> str:
        return str(r["owner_cik"] or r["owner_name"])

    out: list[Detected] = []
    cluster_end = ""
    for i, sale in enumerate(sales):
        day = sale["trade_date"]
        if day <= cluster_end:
            continue  # part of the episode already reported
        start = (date.fromisoformat(day) - timedelta(days=rule.window_days)).isoformat()
        window = [r for r in sales[: i + 1] if r["trade_date"] >= start]
        if len({who(r) for r in window}) < rule.min_insiders:
            continue
        # One signal per episode: window_days from its first sale, keyed on that date so a
        # still-growing episode updates the same signal on the next run.
        first = window[0]["trade_date"]
        cluster_end = (date.fromisoformat(first) + timedelta(days=rule.window_days)).isoformat()
        episode = [r for r in sales if first <= r["trade_date"] <= cluster_end]
        last = episode[-1]["trade_date"]
        people = {who(r) for r in episode}
        value = sum((r["shares"] or 0) * (r["price"] or 0) for r in episode)
        names = sorted({r["owner_name"] or r["owner_cik"] for r in episode})
        until = (date.fromisoformat(last) + timedelta(days=rule.escalate_days)).isoformat()
        follow = [e for e in events if day < e[0] <= until]
        note = f" Followed by: {follow[0][1].lower()} on {follow[0][0]}." if follow else ""
        out.append(_from_spec(
            rule, "insider_cluster_selling",
            severity="Warning" if follow else rule.severity,
            description=f"{len(people)} officers or directors sold {value / 1e6:,.1f}M USD of "
                        f"shares on the open market between {first} and {last}, outside "
                        f"Rule 10b5-1 plans: {', '.join(names)}.{note}",
            event_date=last, key_extra=first,
        ))  # fmt: skip
    return out


_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august",
           "september", "october", "november", "december")  # fmt: skip


def _started(text: str | None) -> tuple[str | None, str | None]:
    """(latest date the wording allows, the exact date if it gives a day) for a matter's start
    as the filing writes it: "March 5, 2024", "March 2024", "2024", "the 2025 second quarter"."""
    low = (text or "").lower()
    year = re.search(r"\b(?:19|20)\d{2}\b", low)
    if year is None:
        return None, None
    month = next((i for i, m in enumerate(_MONTHS, 1) if m in low), None)
    if month is None:
        return f"{year[0]}-12-31", None
    day = re.search(rf"{_MONTHS[month - 1]}\s+(\d{{1,2}}),", low)
    if day:
        exact = f"{year[0]}-{month:02d}-{int(day[1]):02d}"
        return exact, exact
    return f"{year[0]}-{month:02d}-28", None


def legal_signals(matters: Sequence[Mapping[str, Any]], rules: SignalRules,
                  today: str) -> list[Detected]:  # fmt: skip
    """Recent securities, derivative and government matters still disclosed (Phase 15e).
    `matters`: legal_matter rows a reviewer has not rejected, with the last filing's
    `accession_no`."""
    lr = rules.legal
    day = date.fromisoformat(today)
    since = (day - timedelta(days=lr.lookback_days)).isoformat()
    active = (day - timedelta(days=lr.active_days)).isoformat()
    out = []
    for m in matters:
        if m["status"] in ("dismissed", "closed") or m["last_seen"] < active:
            continue
        kind = "wells_notice" if m["wells_notice"] else m["matter_type"]
        spec = getattr(lr, kind, None) if kind in LEGAL_TYPES else None
        if spec is None:
            continue
        latest, exact = _started(m["started"])
        if (latest < since) if latest else m["status"] != "open":
            continue  # began before the window, or no date and not known to be open
        agency = f" ({m['agency']})" if m["agency"] else ""
        began = f", began {m['started']}" if m["started"] else ""
        out.append(_from_spec(
            spec, f"legal_{kind}",
            description=f"{m['name']}{agency}: {m['status']}{began}; disclosed in filings from "
                        f"{m['first_disclosed']} to {m['last_seen']}.",
            event_date=exact or m["first_disclosed"], accession_no=m.get("accession_no"),
            doc_id=m["last_doc_id"], snippet=m["quote"][:SNIPPET_CHARS],
            key_extra=m["matter_key"],
        ))  # fmt: skip
    return out


_OPS = {"<": lambda a, b: a < b, "<=": lambda a, b: a <= b, ">": lambda a, b: a > b,
        ">=": lambda a, b: a >= b}  # fmt: skip


def threshold_signals(
    values: Sequence[Mapping[str, Any]], rules: SignalRules, hidden: frozenset[str] = frozenset()
) -> list[Detected]:
    """User-defined threshold rules (D-049): the metric's latest quarter (or fiscal year) value
    crossing the threshold. `values`: the company's metric_value rows; N/M values never fire.
    Values are in stored units (fraction 0.05 = 5%, ratio 1.2 = 1.2x, USD). A metric in
    `hidden` (hidden metric sets, D-054) raises nothing: the signal's text would show it."""
    out = []
    for rule in rules.thresholds:
        if rule.metric in hidden:
            continue
        rows = [v for v in values if v["metric_code"] == rule.metric
                and v["period_type"] == rule.period]  # fmt: skip
        if not rows:
            continue
        latest = max(rows, key=lambda v: (v["fiscal_year"], v["fiscal_quarter"]))
        if latest["value"] is None or not _OPS[rule.op](latest["value"], rule.value):
            continue
        label = (f"FY{latest['fiscal_year']}" if rule.period == "FY"
                 else f"FY{latest['fiscal_year']} Q{latest['fiscal_quarter']}")  # fmt: skip
        out.append(_from_spec(
            rule, rule.id,
            description=(f"{rule.metric} was {latest['value']:,.4g} at {label} "
                         f"({latest['period_end']}), {rule.op} {rule.value:,.4g}."),
            event_date=latest["period_end"], accession_no=latest["source_accn"], key_extra=label,
        ))  # fmt: skip
    return out


def board_signals(board: Mapping[str, Any] | None, proxy_date: str, accession_no: str | None,
                  rules: SignalRules) -> list[Detected]:  # fmt: skip
    """Board composition signals from the latest proxy (D-045). Only facts both models agreed
    on are present; an unknown fact (None) never raises a signal."""
    if board is None:
        return []
    out = []
    spec = rules.metrics.ceo_chair_without_lead_director
    if spec and board["ceo_is_chair"] is True and board["lead_independent_director"] == "":
        out.append(_from_spec(
            spec, "ceo_chair_without_lead_director",
            description="The CEO is also chair of the board and the proxy names no lead "
                        "independent director.",
            event_date=proxy_date, accession_no=accession_no, key_extra=proxy_date,
        ))  # fmt: skip
    ind = rules.metrics.board_independence
    size, independent = board["board_size"], board["independent_directors"]
    if ind and size and independent is not None and independent / size < ind.min_ratio:
        out.append(_from_spec(
            ind, "board_independence",
            description=f"{independent} of {size} directors are independent "
                        f"({independent / size:.0%}, below {ind.min_ratio:.0%}).",
            event_date=proxy_date, accession_no=accession_no, key_extra=proxy_date,
        ))  # fmt: skip
    dual = rules.metrics.dual_class_shares
    if dual and board.get("dual_class") is True:
        out.append(_from_spec(
            dual, "dual_class_shares",
            description="The company has more than one class of common stock with different "
                        "voting rights per share.",
            event_date=proxy_date, accession_no=accession_no, key_extra=proxy_date,
        ))  # fmt: skip
    fee = rules.metrics.non_audit_fees
    audit = board.get("audit_fees")
    if fee and audit is not None:
        other = sum(board.get(k) or 0.0 for k in ("audit_related_fees", "tax_fees", "other_fees"))
        total = audit + other
        if total > 0 and other / total >= fee.min_share:
            out.append(_from_spec(
                fee, "non_audit_fees",
                description=f"Non-audit fees were {other / 1e6:,.1f}M USD of {total / 1e6:,.1f}M "
                            f"USD paid to the auditor in FY{board.get('fees_fiscal_year')} "
                            f"({other / total:.0%}, at or above {fee.min_share:.0%}).",
                event_date=proxy_date, accession_no=accession_no, key_extra=proxy_date,
            ))  # fmt: skip
    return out
