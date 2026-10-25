"""Phase 5b `insights` stage (decisions D-033).

Per company, in order:
  1. news activity summaries (extractor model, batched)
  2. behavioral insights from earnings call transcripts   (synthesis model, insights.yaml)
  3. strategic & risk insights                              (synthesis model)
  4. key insights, using 2-3                                (synthesis model)
  5. follow-up questions, using 2-4                         (synthesis model)
Each set is grounded (enrich/insights.py), then stored as a new current version unless its
input is unchanged, in which case nothing is written (and the LLM call came from the cache).
Sources, links between objects and the Supporting Evidence text are built by rules
(Phase 8, decisions D-036).
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Connection, Engine, and_, case, desc, insert, select, true, update

from uw_pipeline.clients.llm import LLMClient
from uw_pipeline.compute.registry import Registry
from uw_pipeline.config import CompanyConfig, IndustryConfig
from uw_pipeline.enrich.grounding import ungrounded_numbers
from uw_pipeline.enrich.insights import Cited, GroundingStats, TaskRunner, ground_items
from uw_pipeline.enrich.llm import PROMPTS_DIR, LLMConfig
from uw_pipeline.enrich.package import InsightsConfig, Package, Source, build_package
from uw_pipeline.enrich.schemas import (
    ActivitySummaries,
    BehavioralInsights,
    FollowUpQuestions,
    KeyInsights,
    KeyTakeaways,
    StrategicInsights,
)
from uw_pipeline.enrich.sources import SourceLabels, SourceRef, SourceResolver
from uw_pipeline.enrich.untrusted import one_line
from uw_pipeline.jobs.run_company import TaskResult
from uw_pipeline.jobs.run_xbrl import company_row
from uw_pipeline.schema import (
    activity,
    followup_question,
    followup_question_insight,
    followup_question_metric,
    followup_question_source,
    insight,
    insight_evidence,
    insight_link,
    insight_metric,
    llm_task_stat,
    metric_value,
)

log = logging.getLogger(__name__)

# Part of every stored set's input hash: bump when grounding rules change so that unchanged
# inputs are re-grounded (the LLM output itself still comes from the cache).
GROUNDING_VERSION = "7"


@dataclass
class CompanyStats:
    activities_summarised: int = 0
    tasks: dict[str, str] = field(default_factory=dict)  # task -> "kept/generated" or "unchanged"
    dropped_examples: list[str] = field(default_factory=list)


def _hash(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


# -- 1. news activity summaries ----------------------------------------------------------------


def summarise_activities(
    conn: Connection,
    runner: TaskRunner,
    llm: LLMConfig,
    cfg: InsightsConfig,
    company_id: int,
    company: str,
    since: datetime,
) -> int:
    version = cfg.prompt_versions["activity_summary"]
    rows = conn.execute(
        select(activity.c.activity_id, activity.c.title, activity.c.av_summary,
               activity.c.published_at, activity.c.summary_input_hash)
        .where(activity.c.company_id == company_id, activity.c.published_at >= since)
        .order_by(activity.c.published_at)
    ).all()  # fmt: skip
    todo = []
    for r in rows:
        text = f"{r.published_at:%Y-%m-%d} | {one_line(r.title)} | {one_line(r.av_summary or '')}"
        if r.summary_input_hash != _hash(version, text):
            todo.append((r.activity_id, text))
    done = 0
    for start in range(0, len(todo), cfg.activity_batch):
        batch = todo[start : start + cfg.activity_batch]
        user = f"Company: {company}\n\nid | date | headline | summary\n" + "\n".join(
            f"A{aid} | {text}" for aid, text in batch
        )
        out = runner.run(llm.extractor, "activity_summary", version, user, ActivitySummaries,
                         with_common=False)  # fmt: skip
        if out is None:
            continue
        texts = dict(batch)
        for a in out.activities:
            aid = int(a.activity_id.lstrip("A")) if a.activity_id.lstrip("A").isdigit() else -1
            if aid not in texts:
                continue
            if ungrounded_numbers(f"{a.summary} {a.uw_relevance}", [texts[aid]], []):
                continue  # a number the article doesn't contain
            conn.execute(
                update(activity)
                .where(activity.c.activity_id == aid)
                .values(summary=a.summary, uw_relevance=a.uw_relevance,
                        llm_sentiment=a.sentiment, summary_input_hash=_hash(version, texts[aid]))
            )  # fmt: skip
            done += 1
    return done


# -- storage -----------------------------------------------------------------------------------


def _current_hash(conn: Connection, company_id: int, kind: str) -> str | None:
    table = followup_question if kind == "FOLLOWUP" else insight
    cond = [table.c.company_id == company_id, table.c.is_current == true()]
    if kind != "FOLLOWUP":
        cond.append(insight.c.kind == kind)
    return conn.execute(select(table.c.input_hash).where(and_(*cond)).limit(1)).scalar()


def _retire(conn: Connection, company_id: int, kind: str) -> None:
    if kind == "FOLLOWUP":
        conn.execute(
            update(followup_question)
            .where(followup_question.c.company_id == company_id)
            .values(is_current=False)
        )
    else:
        conn.execute(
            update(insight)
            .where(insight.c.company_id == company_id, insight.c.kind == kind)
            .values(is_current=False)
        )


def source_ref(resolver: SourceResolver, s: Source) -> SourceRef | None:
    if s.ref is not None:
        return s.ref
    if s.kind == "METRIC":
        return resolver.metric(s.label, s.accession_no)
    if s.kind == "SIGNAL" and s.signal_id is not None:
        return resolver.signal(s.signal_id)
    if s.kind == "NEWS" and s.activity_id is not None:
        return resolver.news(s.activity_id)
    if s.doc_id is not None:
        return resolver.document(s.doc_id, s.section_code)
    return None


def _evidence_rows(  # type: ignore[no-untyped-def]
    insight_id: int, cited: Sequence[Cited], resolved: dict[str, list[Cited]],
    resolver: SourceResolver,
):  # fmt: skip
    """Rows for insight_evidence; citations of earlier insights resolve to their own sources."""
    rows: list[dict[str, Any]] = []
    for c in cited:
        chain = resolved.get(c.source.id, [c]) if c.source.kind == "INSIGHT" else [c]
        for cc in chain:
            s = cc.source
            ref = source_ref(resolver, s)
            rows.append(
                {
                    "insight_id": insight_id,
                    "source_kind": {"METRIC": "METRIC", "SIGNAL": "SIGNAL", "NEWS": "NEWS"}.get(
                        s.kind, "DOCUMENT"
                    ),
                    "doc_id": s.doc_id,
                    "section_code": s.section_code,
                    "metric_code": s.metric_code,
                    "signal_id": s.signal_id,
                    "activity_id": s.activity_id,
                    "snippet": cc.quote[:1000],
                    "source_title": ref.title[:500] if ref else None,
                    "source_url": ref.url if ref else None,
                    "source_date": ref.date if ref else None,
                }
            )
    return rows


def evidence_text(rows: Sequence[dict[str, Any]]) -> str:
    """Supporting Evidence, by rules: one line per distinct (source, quote)."""
    lines: list[str] = []
    for r in rows:
        line = f"{r['source_title'] or 'Source'}: \u201c{r['snippet']}\u201d"
        if line not in lines:
            lines.append(line)
    return "\n".join(lines)


def _store_insights(
    conn: Connection,
    company_id: int,
    kind: str,
    items: Sequence[dict[str, Any]],
    *,
    model: str,
    prompt_version: str,
    input_hash: str,
    now: datetime,
    resolved: dict[str, list[Cited]],
    resolver: SourceResolver,
) -> list[int]:
    _retire(conn, company_id, kind)
    ids = []
    for ordinal, item in enumerate(items, start=1):
        cited: list[Cited] = item.get("cited", [])
        rows = _evidence_rows(0, cited, resolved, resolver)
        dates = [r["source_date"] for r in rows if r["source_date"]]
        result = conn.execute(
            insert(insight).values(
                company_id=company_id, kind=kind, ordinal=ordinal,
                category=item.get("category"), subcategory=item.get("subcategory"),
                insight_type=item.get("insight_type"), title=item["title"],
                summary=item["summary"], uw_relevance=item.get("uw_relevance"),
                supporting_evidence=item.get("supporting_evidence"),
                supporting_evidence_text=evidence_text(rows) or None,
                event_date=item.get("event_date"),
                updated_at=item.get("event_date") or (max(dates) if dates else None),
                is_current=True, model=model, prompt_version=prompt_version,
                input_hash=input_hash, created_at=now,
            )
        )  # fmt: skip
        pk = result.inserted_primary_key
        assert pk is not None
        insight_id = int(pk[0])
        ids.append(insight_id)
        if rows:
            conn.execute(insert(insight_evidence), [r | {"insight_id": insight_id} for r in rows])
        # Metrics: the ones named with the insight plus the metric rows it cites.
        codes = set(item.get("metric_codes", []))
        codes |= {c.source.metric_code for c in cited if c.source.metric_code}
        for code in sorted(codes):
            conn.execute(insert(insight_metric).values(insight_id=insight_id, metric_code=code,
                                                       link_source="INSIGHT"))  # fmt: skip
        linked = {c.source.insight_id for c in cited if c.source.insight_id is not None}
        for other in sorted(linked):
            conn.execute(insert(insight_link).values(insight_id=insight_id,
                                                     linked_insight_id=other))  # fmt: skip
    return ids


def _stored_set(conn: Connection, company_id: int, kind: str) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in conn.execute(
            select(insight)
            .where(insight.c.company_id == company_id, insight.c.kind == kind,
                   insight.c.is_current == true())
            .order_by(insight.c.ordinal)
        ).mappings()
    ]  # fmt: skip


def _stored_evidence(conn: Connection, insight_id: int) -> list[Cited]:
    """An insight's stored evidence as citations of its original sources."""
    rows = conn.execute(
        select(insight_evidence).where(insight_evidence.c.insight_id == insight_id)
    ).mappings()
    return [
        Cited(
            Source(
                id=f"stored:{r['evidence_id']}",
                kind=r["source_kind"],
                label="",
                text=r["snippet"],
                doc_id=r["doc_id"],
                section_code=r["section_code"],
                metric_code=r["metric_code"],
                signal_id=r["signal_id"],
                activity_id=r["activity_id"],
                ref=SourceRef(r["source_title"], r["source_url"], r["source_date"], r["doc_id"])
                if r["source_title"]
                else None,
            ),
            r["snippet"],
        )
        for r in rows
    ]


def latest_metric_refs(
    conn: Connection, company_id: int, codes: Sequence[str], resolver: SourceResolver,
    display: dict[str, str],
) -> dict[str, SourceRef]:  # fmt: skip
    """The filing behind each metric's latest value (quarter first, else fiscal year)."""
    out: dict[str, SourceRef] = {}
    for code in codes:
        r = conn.execute(
            select(
                metric_value.c.period_type,
                metric_value.c.fiscal_year,
                metric_value.c.fiscal_quarter,
                metric_value.c.source_accn,
            )
            .where(metric_value.c.company_id == company_id, metric_value.c.metric_code == code)
            # quarter before fiscal year on the same period end (no boolean ORDER BY: SQL Server)
            .order_by(
                desc(metric_value.c.period_end),
                case((metric_value.c.period_type == "Q", 0), else_=1),
            )
            .limit(1)
        ).first()
        if r is None:
            continue
        period = f"FY{r.fiscal_year}" + (f" Q{r.fiscal_quarter}" if r.period_type == "Q" else "")
        out[code] = resolver.metric(f"{display.get(code, code)} {period}", r.source_accn)
    return out


def _store_followups(
    conn: Connection,
    company_id: int,
    questions: Sequence[Any],
    pkg: Package,
    insight_ids: dict[str, int],
    resolver: SourceResolver,
    *,
    model: str,
    prompt_version: str,
    input_hash: str,
    now: datetime,
) -> None:
    """Questions plus, by rules, their insight and metric links and their Sources: the evidence
    of the insights they follow from, then the filings behind their metrics."""
    _retire(conn, company_id, "FOLLOWUP")
    metric_ids = {s.id: s.metric_code for s in pkg.of("METRIC") if s.metric_code}
    display = {s.metric_code: s.label.rsplit(" FY", 1)[0] for s in pkg.of("METRIC")
               if s.metric_code}  # fmt: skip
    for ordinal, q in enumerate(questions, start=1):
        res = conn.execute(
            insert(followup_question).values(
                company_id=company_id, ordinal=ordinal, category=q.category,
                question=q.question, rationale=q.uw_relevance, summary=q.summary,
                following_risk=q.following_risk[:200], uw_relevance=q.uw_relevance,
                system_priority=q.system_priority, is_current=True, model=model,
                prompt_version=prompt_version, input_hash=input_hash, created_at=now,
            )
        )  # fmt: skip
        pk = res.inserted_primary_key
        assert pk is not None
        qid = int(pk[0])
        refs = sorted({r.strip() for r in q.related_insight_ids} & insight_ids.keys())
        for ref in refs:
            conn.execute(
                insert(followup_question_insight).values(question_id=qid,
                                                         insight_id=insight_ids[ref])
            )  # fmt: skip
        codes = sorted({metric_ids[m.strip()] for m in q.related_metric_ids
                        if m.strip() in metric_ids})  # fmt: skip
        for code in codes:
            conn.execute(insert(followup_question_metric).values(
                question_id=qid, metric_code=code, link_source="QUESTION"))  # fmt: skip
        sources: list[dict[str, Any]] = []
        seen: set[tuple[str, str | None]] = set()
        ev = conn.execute(
            select(insight_evidence.c.source_kind, insight_evidence.c.source_title,
                   insight_evidence.c.source_url, insight_evidence.c.source_date,
                   insight_evidence.c.doc_id, insight_evidence.c.metric_code)
            .where(insight_evidence.c.insight_id.in_([insight_ids[r] for r in refs]),
                   insight_evidence.c.source_title.is_not(None))
            .order_by(insight_evidence.c.evidence_id)
        ).all() if refs else []  # fmt: skip
        for e in ev:
            if (e.source_title, e.source_url) not in seen:
                seen.add((e.source_title, e.source_url))
                sources.append({"source_kind": e.source_kind, "title": e.source_title,
                                "url": e.source_url, "source_date": e.source_date,
                                "doc_id": e.doc_id, "metric_code": e.metric_code})  # fmt: skip
        for code, ref in latest_metric_refs(conn, company_id, codes, resolver, display).items():
            if (ref.title, ref.url) not in seen:
                seen.add((ref.title, ref.url))
                sources.append({"source_kind": "METRIC", "title": ref.title[:500],
                                "url": ref.url, "source_date": ref.date, "doc_id": ref.doc_id,
                                "metric_code": code})  # fmt: skip
        if sources:
            conn.execute(insert(followup_question_source),
                         [s | {"question_id": qid, "ordinal": i}
                          for i, s in enumerate(sources, start=1)])  # fmt: skip


# -- company pipeline --------------------------------------------------------------------------


def _within_limit(
    kept: list[tuple[Any, list[Cited]]], field: str, limit: int | None, g: GroundingStats,
    what: str,
) -> list[tuple[Any, list[Cited]]]:  # fmt: skip
    """Items whose `field` fits the app's limit (config text_limits, D-065); a longer one is
    dropped and noted, never cut mid-sentence. No limit: all."""
    if limit is None:
        return kept
    out = []
    for item, cited in kept:
        n = len(str(getattr(item, field) or "").strip())
        if n > limit:
            g.note(f"{what} '{getattr(item, 'title', '') or getattr(item, 'topic', '')}': "
                   f"{field} has {n} characters (at most {limit})")  # fmt: skip
        else:
            out.append((item, cited))
    g.kept = len(out)
    return out


def _insight_source(prefix: str, n: int, row: dict[str, Any]) -> Source:
    head = " / ".join(x for x in (row.get("category"), row.get("subcategory")) if x)
    text = f"{head} | {row['title']} | {row['summary']}"
    if row.get("uw_relevance"):
        text += f" | UW relevance: {row['uw_relevance']}"
    return Source(id=f"{prefix}{n}", kind="INSIGHT", label=row["title"], text=text,
                  insight_id=row["insight_id"])  # fmt: skip


def process_company(
    cfg: CompanyConfig,
    registry: Registry,
    industry: IndustryConfig,
    llm_pair: tuple[LLMClient, LLMConfig],
    icfg: InsightsConfig,
    labels: SourceLabels,
    conn: Connection,
    now: datetime,
) -> CompanyStats:
    client, llm = llm_pair
    row = company_row(conn, cfg)
    resolver = SourceResolver(conn, row, labels)
    company_id = row["company_id"]
    group = industry.groups[row["industry_group"]]
    metrics = registry.for_company(cfg.ticker, group).metrics
    common = (PROMPTS_DIR / f"{icfg.prompt_versions['common']}.md").read_text(encoding="utf-8")
    runner = TaskRunner(client, conn, lambda: now, common, commit=True)
    stats = CompanyStats()
    since = now.replace(tzinfo=None) - timedelta(days=icfg.news_days)
    stats.activities_summarised = summarise_activities(
        conn, runner, llm, icfg, company_id, row["legal_name"], since
    )
    pkg = build_package(conn, row, metrics, group.label, icfg, news_since=since)
    spec = icfg.synthesis
    versions = icfg.prompt_versions
    resolved: dict[str, list[Cited]] = {}

    def record(task: str, gstats: GroundingStats) -> None:
        stats.tasks[task] = f"{gstats.kept}/{gstats.generated}"
        stats.dropped_examples += gstats.examples
        conn.execute(
            insert(llm_task_stat).values(company_id=company_id, task=task,
                                         generated=gstats.generated, kept=gstats.kept,
                                         created_at=now)
        )  # fmt: skip

    def produce(kind: str, task: str, user: str, run: Callable[[], list[dict[str, Any]] | None],  # type: ignore[no-untyped-def]
                cited_ids: str = ""):  # fmt: skip
        """Store a new version unless the input is unchanged; return the current rows.
        `cited_ids` (database ids of citable earlier insights) is part of the hash so that links
        follow re-stored insights even when their text is the same."""
        input_hash = _hash(GROUNDING_VERSION, versions["common"], versions[task], spec.model, user,
                           cited_ids)  # fmt: skip
        if _current_hash(conn, company_id, kind) == input_hash:
            stats.tasks[task] = "unchanged"
            return _stored_set(conn, company_id, kind)
        items = run()
        if items is None:
            stats.tasks[task] = "no output"
            return _stored_set(conn, company_id, kind)
        _store_insights(conn, company_id, kind, items, model=spec.model,
                        prompt_version=versions[task], input_hash=input_hash, now=now,
                        resolved=resolved, resolver=resolver)  # fmt: skip
        return _stored_set(conn, company_id, kind)

    # 2. behavioral
    behavioral: list[dict[str, Any]] = []
    if len(pkg.of("TRANSCRIPT")) >= 2:
        user_b = pkg.render("METRIC", "TRANSCRIPT")

        def run_b() -> list[dict[str, Any]] | None:
            out = runner.run(spec, "behavioral", versions["behavioral"], user_b,
                             BehavioralInsights)  # fmt: skip
            if out is None:
                return None
            kept, g = ground_items(out.insights, pkg, ["title", "description", "uw_relevance"],
                                   "behavioral")  # fmt: skip
            record("behavioral", g)
            return [{"category": "Behavioral Insights", "subcategory": i.subcategory,
                     "title": i.title, "summary": i.description, "uw_relevance": i.uw_relevance,
                     "cited": c} for i, c in kept]  # fmt: skip

        behavioral = produce("BEHAVIORAL", "behavioral", user_b, run_b)

    # 3. strategic & risk
    user_r = pkg.render("METRIC", "SIGNAL", "DOCUMENT", "TRANSCRIPT", "NEWS")

    def run_r() -> list[dict[str, Any]] | None:
        out = runner.run(spec, "strategic_risk", versions["strategic_risk"], user_r,
                         StrategicInsights)  # fmt: skip
        if out is None:
            return None
        kept, g = ground_items(out.insights, pkg, ["title", "description", "uw_relevance"],
                               "strategic")  # fmt: skip
        record("strategic_risk", g)
        items = [{"category": i.category, "subcategory": i.subcategory, "title": i.title,
                  "summary": i.description, "uw_relevance": i.uw_relevance,
                  "event_date": i.event_date, "cited": c} for i, c in kept]  # fmt: skip
        reported = {i["category"] for i in items}
        items += [{"category": n.category, "subcategory": "No signal",
                   "title": "No material signal identified", "summary": n.rationale}
                  for n in out.no_signal if n.category not in reported]  # fmt: skip
        return items

    strategic = produce("STRATEGIC_RISK", "strategic_risk", user_r, run_r)

    # Earlier insights become citable items; citing one resolves to its own sources.
    for prefix, rows in (("B", behavioral), ("R", strategic)):
        for n, r in enumerate(rows, start=1):
            src = _insight_source(prefix, n, r)
            pkg.add(src)
            resolved[src.id] = _stored_evidence(conn, r["insight_id"])

    # 4. key insights
    user_k = pkg.render("METRIC", "SIGNAL", "INSIGHT", "NEWS")

    def run_k() -> list[dict[str, Any]] | None:
        out = runner.run(spec, "key_insights", versions["key_insights"], user_k, KeyInsights)
        if out is None:
            return None
        kept, g = ground_items(
            out.insights, pkg,
            ["title", "summary", "uw_relevance", "supporting_evidence"], "key insight",
        )  # fmt: skip
        kept = _within_limit(kept, "summary", icfg.text_limits.get("key_insights"), g,
                             "key insight")  # fmt: skip
        record("key_insights", g)
        known = {m.code for m in metrics}
        return [{"insight_type": i.type, "title": i.title, "summary": i.summary,
                 "uw_relevance": i.uw_relevance, "supporting_evidence": i.supporting_evidence,
                 "metric_codes": [c for c in i.related_metric_codes if c in known],
                 "cited": c} for i, c in kept]  # fmt: skip

    earlier = json.dumps([r["insight_id"] for r in (*behavioral, *strategic)])
    key = produce("KEY", "key_insights", user_k, run_k, earlier)
    lo, hi = icfg.counts["key_insights"]
    if not lo <= len(key) <= hi:
        log.warning("%s: %d key insights (expected %d-%d)", cfg.ticker, len(key), lo, hi)

    # 5. follow-up questions
    for n, r in enumerate(key, start=1):
        src = _insight_source("K", n, r)
        pkg.add(src)
        resolved[src.id] = _stored_evidence(conn, r["insight_id"])  # for the takeaways
    insight_ids = {f"{p}{n}": r["insight_id"]
                   for p, rows in (("K", key), ("R", strategic), ("B", behavioral))
                   for n, r in enumerate(rows, start=1)}  # fmt: skip
    user_q = pkg.render("METRIC", "INSIGHT")
    q_hash = _hash(GROUNDING_VERSION, versions["common"], versions["followups"], spec.model, user_q,
                   json.dumps(sorted(insight_ids.items())))  # fmt: skip
    if _current_hash(conn, company_id, "FOLLOWUP") == q_hash:
        stats.tasks["followups"] = "unchanged"
    else:
        out = runner.run(spec, "followups", versions["followups"], user_q, FollowUpQuestions)
        if out is None:
            stats.tasks["followups"] = "no output"
        else:
            values = [v for s in pkg.of("METRIC") for v in s.values]
            insight_text = [s.text for s in pkg.of("INSIGHT")]
            good = [
                q
                for q in out.questions
                if not ungrounded_numbers(
                    f"{q.question} {q.summary} {q.uw_relevance}", insight_text, values
                )
            ]
            stats.tasks["followups"] = f"{len(good)}/{len(out.questions)}"
            conn.execute(
                insert(llm_task_stat).values(company_id=company_id, task="followups",
                                             generated=len(out.questions), kept=len(good),
                                             created_at=now)
            )  # fmt: skip
            _store_followups(conn, company_id, good, pkg, insight_ids, resolver,
                             model=spec.model, prompt_version=versions["followups"],
                             input_hash=q_hash, now=now)  # fmt: skip

    # 6. key takeaways: 2-3 bullets for professional lines over all of the above, shown in the
    # app without links; grounded like the other insights, their evidence kept for audits. At
    # most `hi` are kept.
    user_t = pkg.render("METRIC", "SIGNAL", "INSIGHT")
    lo, hi = icfg.counts["key_takeaways"]

    def run_t() -> list[dict[str, Any]] | None:
        out = runner.run(spec, "key_takeaways", versions["key_takeaways"], user_t, KeyTakeaways)
        if out is None:
            return None
        kept, g = ground_items(out.takeaways, pkg, ["topic", "text"], "key takeaway")
        kept = _within_limit(kept, "text", icfg.text_limits.get("key_takeaways"), g,
                             "key takeaway")[:hi]  # fmt: skip
        g.kept = len(kept)
        record("key_takeaways", g)
        return [{"title": t.topic, "summary": t.text.strip(), "cited": c} for t, c in kept]

    takeaways = produce("TAKEAWAY", "key_takeaways", user_t, run_t,
                        json.dumps(sorted(insight_ids.items())))  # fmt: skip
    if not lo <= len(takeaways) <= hi:
        log.warning("%s: %d key takeaways (expected %d-%d)", cfg.ticker, len(takeaways), lo, hi)
    return stats


def run_insights_stage(
    companies: Sequence[CompanyConfig],
    registry: Registry,
    industry: IndustryConfig,
    llm: tuple[LLMClient, LLMConfig] | None,
    icfg: InsightsConfig,
    labels: SourceLabels,
    engine: Engine,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> list[TaskResult]:
    if llm is None:
        return [TaskResult(c.ticker, "insights", ok=False, detail="LLM not configured")
                for c in companies]  # fmt: skip
    results: list[TaskResult] = []
    for cfg in companies:
        try:
            # Short transactions (D-052): each group of writes commits before the next model
            # call, so companies can run in parallel even on SQLite; a failure rolls back only
            # the unfinished group, and a rerun picks up from the LLM cache.
            with engine.connect() as conn:
                stats = process_company(cfg, registry, industry, llm, icfg, labels, conn, now())
                conn.commit()
            for ex in stats.dropped_examples:
                log.info("%s: dropped %s", cfg.ticker, ex)
            log.info("%s: insights %s, %d activities summarised", cfg.ticker, stats.tasks,
                     stats.activities_summarised)  # fmt: skip
            detail = ", ".join(f"{k}={v}" for k, v in stats.tasks.items())
            results.append(TaskResult(cfg.ticker, "insights", ok=True, detail=detail))
        except Exception as exc:
            log.exception("%s: insights stage failed", cfg.ticker)
            results.append(TaskResult(cfg.ticker, "insights", ok=False, detail=str(exc)))
    return results


__all__ = ["Package", "run_insights_stage"]
