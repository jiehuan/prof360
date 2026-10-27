"""Phase 9 `publish` stage (decisions D-037): pipeline outputs -> application tables.

Reads the pipeline database and writes the application database (schema_app.py) in one
transaction per company. Lookup tables (SIC codes, metrics, categories, insight/risk/loan/trend
types) are written as needed. Versioned objects (key insights, strategic risks, follow-up
questions) keep their app rows: new versions get new rows and older ones get isActive = 0, so
user data that points at a question (flags) never moves to a different question. Mapping
choices (categories, risk types, confidence rules) are in config/app_publish.yaml.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from sqlalchemy import Connection, Engine, desc, select, true

from uw_pipeline.compute.registry import MetricDef, Registry
from uw_pipeline.config import CompanyConfig, IndustryConfig
from uw_pipeline.enrich.metric_detail import definition_lines, period_label, shown_rows
from uw_pipeline.enrich.package import fmt_value
from uw_pipeline.enrich.sources import SourceLabels, SourceResolver
from uw_pipeline.jobs.run_company import TaskResult
from uw_pipeline.jobs.run_metric_detail import value_source
from uw_pipeline.jobs.run_xbrl import company_row
from uw_pipeline.load.app_writer import AppWriter
from uw_pipeline.load.upsert import delete_keys, upsert
from uw_pipeline.logos import load_logos
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
    loan_portfolio_detail,
    loan_portfolio_segment,
    metric_detail,
    metric_insight,
    metric_source,
    metric_value,
    news_item,
    news_risk_score,
    source_document,
    transcript_flag,
    transcript_tone,
)
from uw_pipeline.schema_app import (
    Activities,
    Company,
    CompanyMetricDetail,
    FollowUpQuestions,
    ItemLink,
    KeyInsights,
    KeyInsightsTypes,
    KeyTakeaways,
    LoanPortfolio,
    LoanTypes,
    MatrixData,
    News,
    NewsRiskScore,
    RiskTypes,
    SIC_Code,
    SIC_Preferences,
    StrategicRisk,
    TranscriptRiskFlag,
    TranscriptToneAnalysis,
    Trend,
    TrendTypes,
    metric_category,
    metrics,
)

log = logging.getLogger(__name__)


class RiskTypeRule(BaseModel):
    pattern: str
    type: str
    categories: list[str] | None = None


class InsightConfidence(BaseModel):
    one_source: float
    several_sources: float
    sec_filing_bonus: float
    max: float


class ActivityConfidence(BaseModel):
    one_article: float
    several_articles: float


class Confidence(BaseModel):
    derivation: dict[str, float]
    insight: InsightConfidence
    activity: ActivityConfidence


class LoanMetric(BaseModel):
    code: str
    name: str
    subtitle: str | None = None
    description: str


class PublishConfig(BaseModel):
    metric_categories: dict[str, str]
    key_insight_icons: dict[str, str]
    strategic_categories: dict[str, str]
    risk_types: list[RiskTypeRule]
    confidence: Confidence
    trend_metrics: list[str]
    loan_portfolio_metric: LoanMetric
    logos: dict[str, str] = Field(default_factory=dict)  # ticker -> data URI (config/logos/)

    def risk_type(self, subcategory: str | None, category: str) -> str:
        for rule in self.risk_types:
            if rule.categories and category not in rule.categories:
                continue
            if re.search(rule.pattern, subcategory or "", re.IGNORECASE):
                return rule.type
        return "Other"


def load_publish_config(config_dir: Path) -> PublishConfig:
    with (config_dir / "app_publish.yaml").open(encoding="utf-8") as f:
        cfg = PublishConfig.model_validate(yaml.safe_load(f))
    return cfg.model_copy(update={"logos": load_logos(config_dir)})


@dataclass
class Stats:
    counts: dict[str, int] = field(default_factory=dict)

    def add(self, name: str, n: int = 1) -> None:
        self.counts[name] = self.counts.get(name, 0) + n


def _json(x: Any) -> str:
    return json.dumps(x, ensure_ascii=False)


def _date(s: Any) -> date | None:
    if s is None:
        return None
    if isinstance(s, datetime):
        return s.date()
    if isinstance(s, date):
        return s
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


class Publisher:
    def __init__(
        self,
        src: Connection,
        w: AppWriter,
        cfg: PublishConfig,
        labels: SourceLabels,
        row: dict[str, Any],
        metric_defs: Sequence[MetricDef],
        names: dict[str, str],
        hidden: frozenset[str] = frozenset(),
    ) -> None:
        self.src, self.w, self.cfg, self.labels, self.row = src, w, cfg, labels, row
        self.company_id: int = row["company_id"]
        self.cik: str = row["cik"]
        self.p = f"{self.cik}:"  # key prefix of this company's objects
        self.defs = {m.code: m for m in metric_defs}
        self.hidden = hidden  # metric codes of hidden sets (D-054)
        self.names = names
        self.resolver = SourceResolver(src, row, labels)
        self.stats = Stats()
        self.metric_ids: dict[str, int] = {}
        self.app_company = 0

    # -- lookups -------------------------------------------------------------------------

    def _lookup(self, table: Any, key: str, values: dict[str, Any]) -> int:
        return self.w.put(table, key, values)

    def company(self) -> None:
        r = self.row
        sic_id = None
        if r["sic_code"]:
            sic = {"SIC_Code": r["sic_code"], "Description": r["sic_description"]}
            sic_id = self._lookup(SIC_Code, r["sic_code"], sic)
        location = ", ".join(x for x in (r["hq_city"], r["hq_state"]) if x) or None
        self.app_company = self.w.put(Company, self.cik, {
            "SIC_Id": sic_id, "CompanyName": r["name"], "NameAbbreviation": r["ticker"],
            "Location": location, "UpdatedAt": _date(r["last_refreshed_at"]), "CIK": self.cik,
            "logo": self.cfg.logos.get(r["ticker"]),  # config/logos/ (D-067)
        })  # fmt: skip
        self.sic_id = sic_id

    def _metric(self, code: str, name: str, section: str, subtitle: str | None,
                description: str | None, formula: str | None) -> int:  # fmt: skip
        cat = self.cfg.metric_categories.get(section, section)
        cat_id = self._lookup(metric_category, section, {"categoryName": cat})
        return self.w.put(metrics, code, {
            "metric_name": name, "metric_category_Id": cat_id, "subtitle": subtitle,
            "description": description, "Formula": formula,
        })  # fmt: skip

    def metrics(self) -> None:
        for m in self.defs.values():
            if not m.display:
                continue
            formula = " ".join(definition_lines(m, self.names))[:500] or None
            self.metric_ids[m.code] = self._metric(m.code, m.display_name, m.section,
                                                   m.subtitle, m.description, formula)  # fmt: skip
            if self.sic_id is not None:
                pref = {"metric_id": self.metric_ids[m.code], "SIC_Id": self.sic_id}
                self._lookup(SIC_Preferences, f"{self.row['sic_code']}:{m.code}", pref)
        if self.sic_id is not None and self.hidden:
            # Hidden metrics leave the SIC's metric list too (only these: another industry may
            # show the others for the same SIC code).
            keys = [f"{self.row['sic_code']}:{c}" for c in sorted(self.hidden)]
            self.stats.add("SIC_Preferences_removed", self.w.drop(SIC_Preferences, keys))
        if self.row["is_financial"]:
            lm = self.cfg.loan_portfolio_metric
            self.metric_ids[lm.code] = self._metric(lm.code, lm.name, "loan_portfolio",
                                                    lm.subtitle, lm.description, None)  # fmt: skip

    # -- metric values ---------------------------------------------------------------------

    def _confidence(self, r: dict[str, Any]) -> float:
        d = r["derivation"]
        m = self.defs.get(r["metric_code"])
        if d == "LLM_EXTRACTED" and m and m.extraction and m.extraction.dual_check:
            d = "LLM_EXTRACTED_DUAL"
        return self.cfg.confidence.derivation.get(d, 0.5)

    def matrix(self) -> None:
        # Results are read fully before the loop issues more queries (SQL Server allows one
        # active result per connection).
        rows = (
            self.src.execute(
                select(metric_value).where(metric_value.c.company_id == self.company_id)
            )
            .mappings()
            .all()
        )
        keep = []
        for r in rows:
            mid = self.metric_ids.get(r["metric_code"])
            if mid is None:
                continue
            key = (f"{self.p}{r['metric_code']}:{r['period_type']}:{r['fiscal_year']}:"
                   f"{r['fiscal_quarter']}")  # fmt: skip
            ref = self.resolver.filing(r["source_accn"]) if r["source_accn"] else None
            concepts, dims = [], []
            try:
                tags = json.loads(r["source_tags"] or "{}")
            except ValueError:
                tags = {}
            for v in tags.values():
                for t in v if isinstance(v, list) else [v]:
                    base, _, dim = str(t).partition("[")
                    concepts.append(base)
                    if dim:
                        dims.append(dim.rstrip("]"))
            self.w.put(MatrixData, key, {
                "CompanyId": self.app_company, "metric_id": mid, "cik": self.cik,
                "value": r["value"], "unit": r["unit"], "status": r["value_status"],
                "source_class": r["derivation"], "formula": r["formula"][:300],
                "period_start": None, "period_end": _date(r["period_end"]),
                "fiscal_year": r["fiscal_year"],
                "fiscal_period": "FY" if r["period_type"] == "FY" else f"Q{r['fiscal_quarter']}",
                "accession": _json([r["source_accn"]]) if r["source_accn"] else None,
                "form": ref.form if ref else None,
                "filed_date": _json([ref.date]) if ref and ref.date else None,
                "concept_or_section": "; ".join(dict.fromkeys(concepts))[:1000] or None,
                "context_or_dimension": _json(dims) if dims else None,
                "confidence": self._confidence(dict(r)), "notes": (r["evidence"] or None),
            })  # fmt: skip
            keep.append(key)
        self.stats.add("MatrixData", len(keep))
        self.stats.add("MatrixData_removed", self.w.drop_except(MatrixData, self.p, keep))

    # -- insights ----------------------------------------------------------------------------

    def _sources(self, insight_id: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for t, u, d in self.src.execute(
            select(insight_evidence.c.source_title, insight_evidence.c.source_url,
                   insight_evidence.c.source_date)
            .where(insight_evidence.c.insight_id == insight_id,
                   insight_evidence.c.source_title.is_not(None))
            .order_by(insight_evidence.c.evidence_id)
        ):  # fmt: skip
            item = {"title": t, "url": u, "date": d}
            if item not in out:
                out.append(item)
        return out

    def _insight_confidence(self, sources: list[dict[str, Any]]) -> float | None:
        if not sources:
            return None
        c = self.cfg.confidence.insight
        value = c.one_source if len(sources) == 1 else c.several_sources
        if any((s["url"] or "").startswith("https://www.sec.gov/") for s in sources):
            value += c.sec_filing_bonus
        return round(min(value, c.max), 2)

    def insights(self) -> dict[int, tuple[str, int]]:
        """Publish current insights; return pipeline insight_id -> (ItemLink type, app id)."""
        rows = self.src.execute(
            select(insight).where(insight.c.company_id == self.company_id,
                                  insight.c.is_current == true())
            .order_by(insight.c.kind, insight.c.ordinal)
        ).mappings().all()  # fmt: skip
        out: dict[int, tuple[str, int]] = {}
        keep_k, keep_r, keep_t = [], [], []
        for r in rows:
            key = f"{self.p}{r['insight_id']}"
            if r["kind"] == "TAKEAWAY":  # bullets only: no sources, no links (ItemLink)
                self.w.put(KeyTakeaways, key, {
                    "CompanyId": self.app_company, "Ordinal": r["ordinal"],
                    "Takeaway": r["summary"], "UpdatedDate": _date(r["created_at"]),
                    "isActive": True,
                })  # fmt: skip
                keep_t.append(key)
                continue
            sources = self._sources(r["insight_id"])
            first_url = next((s["url"] for s in sources if s["url"]), None)
            if r["kind"] == "KEY":
                type_id = self._lookup(KeyInsightsTypes, r["insight_type"] or "Info",
                                       {"TypeName": r["insight_type"] or "Info"})  # fmt: skip
                app_id = self.w.put(KeyInsights, key, {
                    "CompanyId": self.app_company, "title": r["title"], "summary": r["summary"],
                    "KeyInsightsType_Id": type_id,
                    "iconType": self.cfg.key_insight_icons.get(r["insight_type"] or "Info"),
                    "evidence": r["supporting_evidence_text"] or r["supporting_evidence"],
                    "underwriting": r["uw_relevance"], "source": _json(sources),
                    "updatedDate": _date(r["updated_at"]), "link": first_url,
                    "confidence": self._insight_confidence(sources), "isActive": True,
                })  # fmt: skip
                out[r["insight_id"]] = ("KEY_INSIGHT", app_id)
                keep_k.append(key)
            else:
                category = self.cfg.strategic_categories.get(r["category"] or "", "risk")
                rtype = self.cfg.risk_type(r["subcategory"], category)
                rtype_id = self._lookup(RiskTypes, rtype, {"RiskTypeName": rtype})
                app_id = self.w.put(StrategicRisk, key, {
                    "CompanyId": self.app_company, "Category": category,
                    "RiskType_Id": rtype_id, "UpdatedAt": _date(r["updated_at"]),
                    "Title": r["title"], "Summary": r["summary"],
                    "UnderwritingRelevance": r["uw_relevance"],
                    "SupportingEvidence": r["supporting_evidence_text"],
                    "sources": _json(sources), "isActive": True,
                })  # fmt: skip
                out[r["insight_id"]] = ("STRATEGIC_RISK", app_id)
                keep_r.append(key)
        self.stats.add("KeyInsights", len(keep_k))
        self.stats.add("StrategicRisk", len(keep_r))
        self.stats.add("KeyTakeaways", len(keep_t))
        self.stats.add("retired", self.w.deactivate_except(KeyInsights, self.p, keep_k))
        self.stats.add("retired", self.w.deactivate_except(StrategicRisk, self.p, keep_r))
        self.stats.add("retired", self.w.deactivate_except(KeyTakeaways, self.p, keep_t))
        return out

    def questions(self) -> dict[int, int]:
        rows = self.src.execute(
            select(followup_question).where(followup_question.c.company_id == self.company_id,
                                            followup_question.c.is_current == true())
            .order_by(followup_question.c.ordinal)
        ).mappings().all()  # fmt: skip
        out, keep = {}, []
        for q in rows:
            key = f"{self.p}{q['question_id']}"
            sources = [
                {"title": s.title, "url": s.url, "date": s.source_date}
                for s in self.src.execute(
                    select(followup_question_source)
                    .where(followup_question_source.c.question_id == q["question_id"])
                    .order_by(followup_question_source.c.ordinal)
                )
            ]  # fmt: skip
            out[q["question_id"]] = self.w.put(FollowUpQuestions, key, {
                "CompanyId": self.app_company, "category": q["category"], "title": q["question"],
                "summary": q["summary"], "followingRisk": q["following_risk"],
                "underwritingRelevance": q["uw_relevance"] or q["rationale"],
                "sources": _json(sources), "Priority": q["system_priority"], "isActive": True,
            })  # fmt: skip
            keep.append(key)
        self.stats.add("FollowUpQuestions", len(keep))
        self.stats.add("retired", self.w.deactivate_except(FollowUpQuestions, self.p, keep))
        return out

    def links(self, ins: dict[int, tuple[str, int]], qs: dict[int, int]) -> None:
        wanted: dict[str, dict[str, Any]] = {}

        def add(ft: str, fid: int, tt: str, tid: int, source: str) -> None:
            key = f"{self.p}{ft}:{fid}:{tt}:{tid}"
            wanted.setdefault(key, {"CompanyId": self.app_company, "FromType": ft,
                                    "FromId": fid, "ToType": tt, "ToId": tid,
                                    "LinkSource": source})  # fmt: skip

        ids = list(ins)
        for a, b in self.src.execute(select(insight_link.c.insight_id,
                                            insight_link.c.linked_insight_id)
                                     .where(insight_link.c.insight_id.in_(ids))):  # fmt: skip
            if a in ins and b in ins:
                add(*ins[a], *ins[b], "INSIGHT")
        for i, code, source in self.src.execute(
            select(insight_metric).where(insight_metric.c.insight_id.in_(ids))
        ):
            if code in self.metric_ids:
                add(*ins[i], "METRIC", self.metric_ids[code], source)
        qids = list(qs)
        fqi = followup_question_insight.c
        for q, i in self.src.execute(select(fqi.question_id, fqi.insight_id)
                                     .where(fqi.question_id.in_(qids))):  # fmt: skip
            if i in ins:
                add("FOLLOWUP", qs[q], *ins[i], "QUESTION")
        for q, code, source in self.src.execute(
            select(followup_question_metric)
            .where(followup_question_metric.c.question_id.in_(qids))
        ):  # fmt: skip
            if code in self.metric_ids:
                add("FOLLOWUP", qs[q], "METRIC", self.metric_ids[code], source)
        for key, values in wanted.items():
            self.w.put(ItemLink, key, values)
        self.stats.add("ItemLink", len(wanted))
        self.w.drop_except(ItemLink, self.p, wanted)

    # -- metric detail, trends, loans --------------------------------------------------------

    def metric_details(self) -> None:
        details = {r["metric_code"]: r for r in self.src.execute(
            select(metric_detail).where(metric_detail.c.company_id == self.company_id)
        ).mappings()}  # fmt: skip
        rel = {
            r.metric_code: r.uw_relevance
            for r in self.src.execute(
                select(metric_insight.c.metric_code, metric_insight.c.uw_relevance)
                .where(metric_insight.c.company_id == self.company_id,
                       metric_insight.c.is_current == true())
            )
        }  # fmt: skip
        values: dict[str, list[dict[str, Any]]] = {}
        for r in self.src.execute(
            select(metric_value).where(metric_value.c.company_id == self.company_id)
        ).mappings():
            values.setdefault(r["metric_code"], []).append(dict(r))
        keep = []
        for code, d in details.items():
            mid = self.metric_ids.get(code)
            if mid is None:
                continue
            m = self.defs[code]
            sources = [
                {"title": s.title, "url": s.url, "date": s.filed_at}
                for s in self.src.execute(
                    select(metric_source)
                    .where(metric_source.c.company_id == self.company_id,
                           metric_source.c.metric_code == code)
                    .order_by(desc(metric_source.c.period_type))
                )
            ]  # fmt: skip
            points = [
                {"label": period_label(r),
                 "text": f"{fmt_value(r['value'], m.unit, r['value_status'])} "
                         f"({self.labels.derivations.get(r['derivation'], r['derivation'])})"}
                for r in shown_rows(values.get(code, []))
            ]  # fmt: skip
            key = f"{self.p}{code}"
            self.w.put(CompanyMetricDetail, key, {
                "CompanyId": self.app_company, "metric_id": mid, "Methodology": d["methodology"],
                "DataSources": _json(sources), "DataPoints": _json(points),
                "UnderwritingRelevance": rel.get(code), "UpdatedAt": d["updated_at"],
            })  # fmt: skip
            keep.append(key)
        lm = self.cfg.loan_portfolio_metric.code
        loan = self.src.execute(
            select(loan_portfolio_detail)
            .where(loan_portfolio_detail.c.company_id == self.company_id)
        ).mappings().first()  # fmt: skip
        if loan is not None and lm in self.metric_ids:
            key = f"{self.p}{lm}"
            self.w.put(CompanyMetricDetail, key, {
                "CompanyId": self.app_company, "metric_id": self.metric_ids[lm],
                "Methodology": loan["methodology"], "DataSources": loan["data_sources_json"],
                "DataPoints": loan["raw_data_points_json"], "UnderwritingRelevance": None,
                "UpdatedAt": loan["updated_at"],
            })  # fmt: skip
            keep.append(key)
        self.stats.add("CompanyMetricDetail", len(keep))
        self.w.drop_except(CompanyMetricDetail, self.p, keep)

    def trends(self) -> None:
        keep = []
        if self.row["is_financial"]:
            for code in self.cfg.trend_metrics:
                m = self.defs.get(code)
                if m is None:
                    continue
                type_id = self._lookup(TrendTypes, code, {
                    "TrendName": m.display_name,
                    "Methodology": " ".join(definition_lines(m, self.names)) or None,
                })  # fmt: skip
                rows = self.src.execute(
                    select(metric_value).where(metric_value.c.company_id == self.company_id,
                                               metric_value.c.metric_code == code,
                                               metric_value.c.period_type == "FY")
                ).mappings().all()  # fmt: skip
                for r in rows:
                    ref = value_source(self.src, self.resolver, m.display_name, dict(r))
                    shown = fmt_value(r["value"], m.unit, r["value_status"])
                    point = {"label": period_label(dict(r)), "text": shown}
                    if r["evidence"]:
                        point["evidence"] = r["evidence"]
                    key = f"{self.p}{code}:{r['fiscal_year']}"
                    self.w.put(Trend, key, {
                        "TrendTypeId": type_id, "CompanyId": self.app_company,
                        "Year": r["fiscal_year"], "Value": r["value"],
                        "DataSources": _json([{"title": ref.title, "url": ref.url,
                                               "date": ref.date}]),
                        "DataPoints": _json([point]),
                    })  # fmt: skip
                    keep.append(key)
        self.stats.add("Trend", len(keep))
        self.w.drop_except(Trend, self.p, keep)

    def loans(self) -> None:
        keep = []
        for r in self.src.execute(
            select(loan_portfolio_segment)
            .where(loan_portfolio_segment.c.company_id == self.company_id)
        ).mappings():  # fmt: skip
            type_key = f"{r['parent_segment'] or ''}|{r['segment']}"
            loan_type = {"LoanTypeName": r["segment"], "ParentSegment": r["parent_segment"]}
            type_id = self._lookup(LoanTypes, type_key, loan_type)
            key = f"{self.p}{r['period_end']}:{r['segment']}"
            self.w.put(LoanPortfolio, key, {
                "LoanTypeId": type_id, "CompanyId": self.app_company,
                "LoanValue": round(r["outstanding"], 2), "UpdatedAt": _date(r["updated_at"]),
                "PeriodEnd": _date(r["period_end"]),
                "PctOfBook": round(r["pct_of_book"], 4) if r["pct_of_book"] is not None else None,
                "Nonaccrual": round(r["nonaccrual"], 2) if r["nonaccrual"] is not None else None,
                "NonaccrualRatio": round(r["nonaccrual_ratio"], 4)
                if r["nonaccrual_ratio"] is not None else None,
            })  # fmt: skip
            keep.append(key)
        self.stats.add("LoanPortfolio", len(keep))
        self.w.drop_except(LoanPortfolio, self.p, keep)

    # -- news ----------------------------------------------------------------------------------

    def news(self) -> None:
        c = self.cfg.confidence.activity
        keep_a: list[str] = []
        for a in (
            self.src.execute(select(activity).where(activity.c.company_id == self.company_id))
            .mappings()
            .all()
        ):
            articles = self.src.execute(
                select(news_item.c.source, news_item.c.title, news_item.c.url,
                       news_item.c.published_at)
                .where(news_item.c.activity_id == a["activity_id"])
                .order_by(news_item.c.published_at).limit(10)
            ).all()  # fmt: skip
            sources = [{"title": f"{s or 'News'}: {t}", "url": u, "date": f"{p:%Y-%m-%d}"}
                       for s, t, u, p in articles] or [
                {"title": f"{a['source_name']}: {a['title']}", "url": a["url"],
                 "date": f"{a['published_at']:%Y-%m-%d}"}]  # fmt: skip
            keep_a.append(f"{self.p}{a['activity_id']}")
            self.w.put(Activities, keep_a[-1], {
                "CompanyId": self.app_company, "title": a["title"],
                "updateDate": a["published_at"], "summary": a["summary"] or a["av_summary"],
                "sentiment": a["llm_sentiment"] or a["av_sentiment_label"],
                "underwriting": a["uw_relevance"], "sources": _json(sources),
                "confidence": c.several_articles if a["source_count"] > 1 else c.one_article,
            })  # fmt: skip
        keep_n: list[str] = []
        for n in self.src.execute(
            select(news_item).where(news_item.c.company_id == self.company_id)
        ).mappings():
            keep_n.append(f"{self.p}{n['news_id']}")
            self.w.put(News, keep_n[-1], {
                "CompanyId": self.app_company, "ArticleId": n["news_id"],
                "NewsTitle": n["title"], "Description": n["summary"],
                "NewsReleaseDateTime": n["published_at"],
            })  # fmt: skip
        self.stats.add("Activities", len(keep_a))
        self.stats.add("News", len(keep_n))
        # Articles the news stage pruned as unrelated (Phase 9) leave the app too. Their scores
        # reference News (FK, enforced on SQL Server), so they go first.
        kept = set(keep_n)
        gone = [a for k, a in self.w.keys(News, self.p).items() if k not in kept]
        for i in range(0, len(gone), 500):
            self.w.conn.execute(
                NewsRiskScore.delete().where(NewsRiskScore.c.NewsId.in_(gone[i : i + 500]))
            )
        self.w.drop_except(Activities, self.p, keep_a)
        self.w.drop_except(News, self.p, keep_n)

    # -- risk scores (9.4) -------------------------------------------------------------------

    def scores(self) -> None:
        dst = self.w.conn
        nr, ni = news_risk_score.c, news_item.c
        rows = self.src.execute(
            select(news_risk_score, ni.news_id)
            .join(news_item, (ni.activity_id == nr.activity_id) & (ni.dedup_method == "FIRST"))
            .where(nr.company_id == self.company_id)
        ).mappings().all()  # fmt: skip
        out = []
        for r in rows:
            news_id = self.w.lookup(News, f"{self.p}{r['news_id']}")
            if news_id is None:
                continue
            out.append({
                "NewsId": news_id, "runId": r["run_id"] or 0, "CompanyId": self.app_company,
                # the app's Activities.ActivityId, not the pipeline's activity id
                "ActivityId": self.w.lookup(Activities, f"{self.p}{r['activity_id']}"),
                "litigationEO": r["litigation"],
                "regulatory": r["regulatory"], "financialDistress": r["financial_distress"],
                "governance": r["governance"], "operational": r["operational"],
                "overallRiskScore": r["overall"], "rationale": r["rationale"],
                "modelVersion": f"{r['model']}/{r['prompt_version']}", "createdAt": r["created_at"],
            })  # fmt: skip
        nrs = NewsRiskScore.c
        for row in out:  # one current score per article: drop rows of earlier runs
            dst.execute(
                NewsRiskScore.delete().where(nrs.NewsId == row["NewsId"], nrs.runId != row["runId"])
            )
        upsert(dst, NewsRiskScore, out, key_cols=["NewsId", "runId"])
        self.stats.add("NewsRiskScore", len(out))

        keep = []
        for t in self.src.execute(
            select(transcript_tone, source_document.c.accession_no)
            .join(source_document, source_document.c.doc_id == transcript_tone.c.doc_id)
            .where(transcript_tone.c.company_id == self.company_id)
        ).mappings().all():  # fmt: skip
            run = t["run_id"] or 0
            key = f"{self.p}{t['doc_id']}"
            self.w.put(TranscriptToneAnalysis, key, {
                "TranscriptsId": t["doc_id"], "runId": run, "CompanyId": self.app_company,
                "Quarter": t["accession_no"].rsplit("-", 1)[-1],
                "overallTone": t["overall_tone"], "confidence": t["confidence"],
                "hedging": t["hedging"], "uncertainty": t["uncertainty"],
                "forwardLooking": t["forward_looking"], "liabilityCues": t["liability_cues"],
                "nChunks": t["n_chunks"], "NChunksAttempted": t["n_chunks_attempted"],
                "modelVersion": f"{t['model']}/{t['prompt_version']}", "createdAt": t["created_at"],
            })  # fmt: skip
            keep.append(key)
            self._flags(t["doc_id"], run)
        self.stats.add("TranscriptToneAnalysis", len(keep))
        kept = set(keep)  # flags of transcripts no longer scored go with their tone row
        gone = [(int(k[len(self.p) :]),) for k in self.w.keys(TranscriptToneAnalysis, self.p)
                if k not in kept]  # fmt: skip
        delete_keys(dst, TranscriptRiskFlag, ["TranscriptsId"], gone)
        self.w.drop_except(TranscriptToneAnalysis, self.p, keep)

    def _flags(self, doc_id: int, run: int) -> None:
        """A transcript's risk flags: written only when they changed, stale ones removed."""
        flags = {
            (run, f.flag_index): {"TranscriptsId": doc_id, "runId": run, "FlagIndex": f.flag_index,
                                  "FlagText": f.flag_text, "Category": f.category}
            for f in self.src.execute(
                select(transcript_flag).where(transcript_flag.c.doc_id == doc_id)
            ).all()
        }  # fmt: skip
        trf, key = TranscriptRiskFlag.c, ["TranscriptsId", "runId", "FlagIndex"]
        old = {
            (r["runId"], r["FlagIndex"]): dict(r)
            for r in self.w.conn.execute(
                select(TranscriptRiskFlag).where(trf.TranscriptsId == doc_id)
            ).mappings().all()
        }  # fmt: skip
        self.stats.add("TranscriptRiskFlag", len(flags))
        if old == flags:
            return
        delete_keys(self.w.conn, TranscriptRiskFlag, key,
                    [(doc_id, *k) for k in old if k not in flags])  # fmt: skip
        upsert(self.w.conn, TranscriptRiskFlag, list(flags.values()), key_cols=key)


def publish_company(
    cfg: CompanyConfig,
    registry: Registry,
    industry: IndustryConfig,
    pcfg: PublishConfig,
    labels: SourceLabels,
    src: Connection,
    dst: Connection,
    now: datetime,
) -> tuple[Stats, AppWriter]:
    row = company_row(src, cfg)
    group = industry.groups[row["industry_group"]]
    defs = registry.for_company(cfg.ticker, group).metrics
    names = {m.code: m.display_name for m in defs} | labels.concepts
    w = AppWriter(dst, now, scope=f"{row['cik']}:")
    p = Publisher(src, w, pcfg, labels, row, defs, names, registry.hidden_codes)
    p.company()
    p.metrics()
    p.matrix()
    ins = p.insights()
    qs = p.questions()
    p.links(ins, qs)
    p.metric_details()
    p.trends()
    p.loans()
    p.news()
    p.scores()
    return p.stats, w


def run_publish_stage(
    companies: Sequence[CompanyConfig],
    registry: Registry,
    industry: IndustryConfig,
    pcfg: PublishConfig,
    labels: SourceLabels,
    engine: Engine,
    app_engine: Engine,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> list[TaskResult]:
    results: list[TaskResult] = []
    for cfg in companies:
        try:
            with engine.connect() as src, app_engine.begin() as dst:
                stats, w = publish_company(cfg, registry, industry, pcfg, labels, src, dst, now())
            detail = (f"{w.written} written, {w.unchanged} unchanged; "
                      + ", ".join(f"{k}={v}" for k, v in stats.counts.items() if v))  # fmt: skip
            log.info("%s: publish %s", cfg.ticker, detail)
            results.append(TaskResult(cfg.ticker, "publish", ok=True, detail=detail[:500]))
        except Exception as exc:
            log.exception("%s: publish stage failed", cfg.ticker)
            results.append(TaskResult(cfg.ticker, "publish", ok=False, detail=str(exc)))
    return results
