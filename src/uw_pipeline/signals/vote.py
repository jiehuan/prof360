"""Rule + LLM voting on judgment signals (decisions D-028).

For each in-scope document, every configured topic (e.g. "leadership:CFO" in an 8-K Item 5.02)
gets three possible votes:
  1. the rules (already computed),
  2. the extractor LLM (config/llm.yaml) reading the section text,
  3. only when 1 and 2 disagree: the tie-breaker LLM (a stronger model), asked the same question
     independently.
The tie-breaker sees both claims (unlabelled) with their evidence and rules on the text. The
majority answer wins. When all three differ, the topic is DISPUTED and left for a human.
A rule detection that the two LLMs vote down is kept as OVERRULED rather than deleted, so a
correct rule can't silently disappear when the two models make the same mistake.
LLM answers count only when their evidence quote is found in the source text.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from pydantic import ValidationError
from sqlalchemy import Connection

from uw_pipeline.clients.llm import LLMClient
from uw_pipeline.enrich.grounding import is_grounded
from uw_pipeline.enrich.llm import LLMConfig, ModelSpec, cached_call
from uw_pipeline.enrich.schemas import SectionSignals, TieBreak, strict_json_schema
from uw_pipeline.signals.detect import Detected, Doc
from uw_pipeline.signals.rules import SignalRules, SignalSpec

NONE = "none"
INVALID = "invalid"  # tie-break answer that is not allowed for the topic or not grounded
SCHEMA = strict_json_schema(SectionSignals)
TIEBREAK_SCHEMA = strict_json_schema(TieBreak)


def allowed_answers(topic: str) -> tuple[str, ...]:
    if topic.startswith("leadership:"):
        return ("departure", "appointment", "succession", NONE)
    if topic == "listing":
        return ("adverse", "voluntary_transfer", NONE)
    return ("present", NONE)


_FINDING_TOPIC = {
    "going_concern": ("going_concern", "present"),
    "material_weakness": ("material_weakness", "present"),
    "controls_not_effective": ("controls_not_effective", "present"),
    "listing_adverse": ("listing", "adverse"),
    "listing_voluntary_transfer": ("listing", "voluntary_transfer"),
}


@dataclass(frozen=True)
class Answer:
    value: str
    evidence: str | None = None
    detail: str | None = None  # e.g. person and effective date for leadership changes


@dataclass(frozen=True)
class VoteRow:
    doc_id: int
    topic: str
    rule_value: str
    extractor_value: str | None
    tiebreak_value: str | None
    outcome: str
    status: str  # UNANIMOUS | MAJORITY | OVERRULED | DISPUTED
    evidence: str | None


# -- answers from each voter -------------------------------------------------------------------


def rule_answers(detected: Sequence[Detected], topics: Sequence[str]) -> dict[str, Answer]:
    """What the rules decided for one document, per topic."""
    out = {t: Answer(NONE) for t in topics}
    for d in detected:
        if d.rule_id.startswith("8k_5.02_"):
            role, kind = d.rule_id.split("_")[2:4]
            topic = f"leadership:{role.upper()}"
            if topic in out:
                out[topic] = Answer(kind, d.snippet)
        elif d.rule_id == "8k_3.01" and "listing" in out:
            value = "voluntary_transfer" if d.subcategory == "Listing Transfer" else "adverse"
            out["listing"] = Answer(value, d.snippet)
        elif d.rule_id in out:
            out[d.rule_id] = Answer("present", d.snippet)
    return out


def llm_answers(
    parsed: SectionSignals, topics: Sequence[str], source: str
) -> tuple[dict[str, Answer], int]:
    """Grounded LLM answers per topic, and the number of items dropped for bad evidence."""
    out = {t: Answer(NONE) for t in topics}
    dropped = 0
    for lc in parsed.leadership_changes:
        topic = f"leadership:{lc.role}"
        if topic not in out:
            continue
        if not is_grounded(lc.evidence, source):
            dropped += 1
            continue
        detail = ", ".join(x for x in (lc.person, lc.effective_date) if x) or None
        prev = out[topic]
        if prev.value in (NONE, lc.change):
            out[topic] = Answer(lc.change, prev.evidence or lc.evidence, prev.detail or detail)
        else:  # a departure and an appointment to the same role
            evidence = lc.evidence if lc.change == "departure" else prev.evidence
            details = "; ".join(x for x in (prev.detail, detail) if x) or None
            out[topic] = Answer("succession", evidence, details)
    for f in parsed.findings:
        topic, value = _FINDING_TOPIC[f.topic]
        if topic not in out:
            continue
        if not is_grounded(f.evidence, source):
            dropped += 1
            continue
        if not (topic == "listing" and out[topic].value == "adverse"):
            out[topic] = Answer(value, f.evidence)
    return out, dropped


# -- combining votes ---------------------------------------------------------------------------


def _combine(*agreeing: Answer) -> Answer:
    """One answer from voters that agree: the first quote found, and the first detail found
    (only LLMs extract details such as the person's name)."""
    evidence = next((a.evidence for a in agreeing if a.evidence), None)
    detail = next((a.detail for a in agreeing if a.detail), None)
    return Answer(agreeing[0].value, evidence, detail)


def decide(
    rule: Answer, extractor: Answer, tiebreak: Answer | None
) -> tuple[str, str, Answer | None]:
    """(outcome, status, answer carrying the evidence) from up to three votes.

    OVERRULED: the rules found something and both LLMs said none. The outcome is "none", but
    the rule's answer is returned so the signal is kept for review."""
    if rule.value == extractor.value:
        return rule.value, "UNANIMOUS", _combine(rule, extractor)
    if tiebreak is None:
        raise ValueError("disagreement needs a tie-break vote")
    if tiebreak.value == extractor.value == NONE and rule.value != NONE:
        return NONE, "OVERRULED", rule
    for winner in (rule, extractor):
        if tiebreak.value == winner.value:
            return winner.value, "MAJORITY", _combine(winner, tiebreak)
    proposal = rule if rule.value != NONE else extractor
    return "disputed", "DISPUTED", proposal


def _spec_for(topic: str, value: str, rules: SignalRules) -> tuple[str, SignalSpec, str]:
    """(rule_id, spec, title) for a positive outcome."""
    if topic.startswith("leadership:"):
        role = topic.split(":")[1]
        spec = rules.eight_k_items["5.02"]
        severity = rules.leadership.severity[value]  # type: ignore[index]
        rule_id = f"8k_5.02_{role.lower()}_{value}"
        return rule_id, spec.model_copy(update={"severity": severity}), f"{role} {value}"
    if topic == "listing":
        spec = rules.eight_k_items["3.01"]
        if value == "voluntary_transfer" and spec.downgrade:
            dg = spec.downgrade
            down = spec.model_copy(
                update={"severity": dg.severity, "subcategory": dg.subcategory, "title": dg.title}
            )
            return "8k_3.01", down, dg.title
        return "8k_3.01", spec, spec.title
    kw = next(k for k in rules.keywords if k.id == topic)
    return kw.id, kw, kw.title


def signal_for(
    doc: Doc,
    topic: str,
    outcome: str,
    status: str,
    answer: Answer | None,
    votes: Mapping[str, str | None],
    rules: SignalRules,
    section_code: str,
) -> Detected | None:
    if outcome == NONE and status != "OVERRULED":
        return None
    value = answer.value if (status in ("DISPUTED", "OVERRULED") and answer) else outcome
    rule_id, spec, title = _spec_for(topic, value, rules)
    lead = title if title[:2].isupper() else title[0].lower() + title[1:]  # keep "CFO"
    if status == "DISPUTED":
        title = f"Possible {lead} (disputed)"
    elif status == "OVERRULED":
        title = f"Possible {lead} (rule match overruled by LLM review)"
    detail = f" ({answer.detail})" if answer and answer.detail else ""
    return Detected(
        rule_id=rule_id,
        category=spec.category,
        subcategory=spec.subcategory,
        severity=spec.severity,
        title=title,
        description=f"{title}{detail}; {doc.form_type} filed {doc.filed_at}.",
        event_date=doc.filed_at,
        accession_no=doc.accession_no,
        doc_id=doc.doc_id,
        section_code=section_code,
        snippet=answer.evidence if answer else None,
        key_extra=topic.split(":")[1] if topic.startswith("leadership:") else "",
        method="VOTE",
        review_status=status,
        votes_json=json.dumps(dict(votes)),
    )


# -- per-document voting -----------------------------------------------------------------------


class Voter:
    def __init__(
        self,
        client: LLMClient,
        config: LLMConfig,
        rules: SignalRules,
        conn: Connection,
        now: Callable[[], datetime],
    ) -> None:
        self.client = client
        self.config = config
        self.rules = rules
        self.conn = conn
        self.now = now
        self.system = config.prompt()
        self.tiebreak_system = config.tiebreak_prompt()
        self.stats = {
            "documents": 0, "tiebreaks": 0, "overruled": 0, "disputed": 0,
            "ungrounded": 0, "skipped_long": 0,
        }  # fmt: skip

    def _ask(self, spec: ModelSpec, user: str) -> SectionSignals | None:
        raw = cached_call(
            self.conn, self.client, spec,
            task="signals", prompt_version=self.config.prompt_version,
            system=self.system, user=user, schema=SCHEMA, now=self.now(),
            validate=SectionSignals.model_validate,
        )  # fmt: skip
        if raw is None:
            return None
        try:
            return SectionSignals.model_validate(raw)
        except ValidationError:
            return None

    def _tiebreak(
        self,
        user: str,
        disputes: Mapping[str, tuple[Answer, Answer]],
        source: str,
    ) -> dict[str, Answer] | None:
        """The tie-breaker's ruling per disputed topic, given both claims and their evidence."""
        lines = ["", "Disagreements:"]
        for topic, pair in disputes.items():
            # Neutral, stable order so neither claim is marked as the rules' or a model's.
            a, b = sorted(pair, key=lambda x: (x.value, x.evidence or ""))
            lines += [
                f"- topic: {topic} (allowed answers: {', '.join(allowed_answers(topic))})",
                f"  Claim A: {a.value}; quote: {a.evidence or '(none)'}",
                f"  Claim B: {b.value}; quote: {b.evidence or '(none)'}",
            ]
        raw = cached_call(
            self.conn, self.client, self.config.tiebreaker,
            task="tiebreak", prompt_version=self.config.tiebreak_prompt_version,
            system=self.tiebreak_system, user=user + "\n".join(lines),
            schema=TIEBREAK_SCHEMA, now=self.now(), validate=TieBreak.model_validate,
        )  # fmt: skip
        if raw is None:
            return None
        try:
            parsed = TieBreak.model_validate(raw)
        except ValidationError:
            return None
        out = {t: Answer(INVALID) for t in disputes}
        for r in parsed.rulings:
            if r.topic not in disputes or r.answer not in allowed_answers(r.topic):
                continue
            if r.answer != NONE and not is_grounded(r.evidence, source):
                self.stats["ungrounded"] += 1
                continue
            out[r.topic] = Answer(r.answer, r.evidence or None)
        return out

    def vote_document(
        self,
        doc: Doc,
        company_name: str,
        sections: Mapping[str, str],
        rule_detected: Sequence[Detected],
    ) -> tuple[list[Detected], list[VoteRow]] | None:
        """Voted signals replacing the rules' judgment signals for this document, or None to
        keep the rule result (nothing in scope, input too long, or no usable LLM answer)."""
        in_scope = {c: t for c, t in sections.items() if c in self.config.topics}
        if not in_scope:
            return None
        topics = [t for c in sorted(in_scope) for t in self.config.topics[c]]
        source = "\n\n".join(in_scope[c] for c in sorted(in_scope))
        if len(source) > self.config.max_input_chars:
            self.stats["skipped_long"] += 1
            return None
        user = (
            f"Company: {company_name}\nFiling: {doc.form_type} filed {doc.filed_at} "
            f"(accession {doc.accession_no})\n\n"
            + "\n\n".join(
                f'<excerpt section="{c}">\n{in_scope[c]}\n</excerpt>' for c in sorted(in_scope)
            )
        )
        extracted = self._ask(self.config.extractor, user)
        if extracted is None:
            return None
        self.stats["documents"] += 1
        rule = rule_answers(rule_detected, topics)
        ext, dropped = llm_answers(extracted, topics, source)
        self.stats["ungrounded"] += dropped

        tb: dict[str, Answer] = {}
        disputes = {t: (rule[t], ext[t]) for t in topics if rule[t].value != ext[t].value}
        if disputes:
            self.stats["tiebreaks"] += 1
            ruling = self._tiebreak(user, disputes, source)
            if ruling is None:
                return None
            tb = ruling

        signals: list[Detected] = []
        rows: list[VoteRow] = []
        section_of = {t: c for c in in_scope for t in self.config.topics[c]}
        for topic in topics:
            tb_answer = tb.get(topic)
            outcome, status, answer = decide(rule[topic], ext[topic], tb_answer)
            if status in ("OVERRULED", "DISPUTED"):
                self.stats[status.lower()] += 1
            votes = {
                "rules": rule[topic].value,
                self.config.extractor.model: ext[topic].value,
                **({self.config.tiebreaker.model: tb_answer.value} if tb_answer else {}),
            }
            rows.append(
                VoteRow(
                    doc_id=doc.doc_id,
                    topic=topic,
                    rule_value=rule[topic].value,
                    extractor_value=ext[topic].value,
                    tiebreak_value=tb_answer.value if tb_answer else None,
                    outcome=outcome,
                    status=status,
                    evidence=answer.evidence if answer else None,
                )
            )
            sig = signal_for(
                doc, topic, outcome, status, answer, votes, self.rules, section_of[topic]
            )
            if sig is not None:
                signals.append(sig)
        return signals, rows


def judgment_rule_ids(detected: Detected) -> bool:
    """Rule signals that voting replaces (judgments, not structural facts like item numbers)."""
    return (
        detected.rule_id.startswith("8k_5.02")
        or detected.rule_id == "8k_3.01"
        or detected.rule_id in ("going_concern", "material_weakness", "controls_not_effective")
    )


def merge(
    rule_signals: Sequence[Detected], voted: Mapping[int, list[Detected]], rules: SignalRules
) -> list[Detected]:
    """Replace the rules' judgment signals of voted documents with the voted ones.

    Item numbers are facts from SEC data, so every voted filing keeps one Item 5.02 / 3.01
    signal: the voted specific one if any, else the generic item signal."""
    out: list[Detected] = []
    for doc_id, signals in voted.items():
        mine = [d for d in rule_signals if d.doc_id == doc_id]
        for item, prefix in (("5.02", "8k_5.02"), ("3.01", "8k_3.01")):
            had_item = any(d.rule_id.startswith(prefix) for d in mine)
            if had_item and not any(v.rule_id.startswith(prefix) for v in signals):
                spec = rules.eight_k_items[item]
                base = next(d for d in mine if d.rule_id.startswith(prefix))
                out.append(
                    replace(
                        base,
                        rule_id=prefix,
                        severity=spec.severity,
                        subcategory=spec.subcategory,
                        title=spec.title,
                        description=f"8-K Item {item}: {spec.title}.",
                        key_extra="",
                        method="RULE",  # the item number is a fact, not a judgment
                        review_status="RULE_ONLY",
                        votes_json=None,
                    )
                )
        out.extend(signals)
    for d in rule_signals:
        if d.doc_id is not None and d.doc_id in voted and judgment_rule_ids(d):
            continue
        out.append(d)
    return out
