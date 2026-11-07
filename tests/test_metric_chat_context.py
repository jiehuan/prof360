"""Metric chat context (Phase 19.A, D-090): formula inputs recorded by compute, the context JSON
(data-point ids, tags, N/M reasons, overrides, peers), its storage and publishing, and the
fallback_denominator DQ rule."""

import json
from datetime import UTC, datetime

from sqlalchemy import create_engine, func, insert, select

from tests import test_insights as ti
from tests import test_metric_detail as tm
from tests import test_publish as tp
from tests.conftest import CONFIG_DIR
from tests.test_metrics import p, registry, run
from uw_pipeline.compute.registry import CompanyRegistry, MetricDef, TagRef, load_registry
from uw_pipeline.config import CompanyConfig, load_industry_config
from uw_pipeline.enrich.metric_chat_context import (
    CONTEXT_VERSION,
    CompanyData,
    build_context,
    context_json,
    id_label,
    peer_medians,
)
from uw_pipeline.enrich.sources import SourceRef, load_source_labels
from uw_pipeline.jobs.run_metric_detail import run_metric_detail_stage
from uw_pipeline.jobs.run_xbrl import inputs_json
from uw_pipeline.quality.dq import FallbackDenominator, fallback_denominator
from uw_pipeline.schema import company, metadata, metric_chat_context, metric_input, metric_value
from uw_pipeline.schema_app import MetricChatContext

NOW = datetime(2026, 11, 7, tzinfo=UTC)
GM_TAG = "us-gaap:InterestExpenseNonoperating[gm:BusinessGroupAxis=gm:AutomotiveMember]"
PAID = "cash interest paid, not interest expense"


# -- compute: inputs ---------------------------------------------------------------------------


def test_compute_records_each_value_s_inputs() -> None:
    reg = registry(
        {"oi": ["us-gaap:OperatingIncomeLoss"], "ie": ["us-gaap:InterestExpense"],
         "assets": ["us-gaap:Assets"], "rev": ["us-gaap:Revenues"]},
        [{"code": "ebit", "formula": "oi"},
         {"code": "cover", "formula": "ebit / ie", "nm_when": "ebit <= 0", "unit": "ratio"},
         {"code": "roa", "formula": "oi / ((assets + assets_py) / 2)", "unit": "fraction"},
         {"code": "growth", "kind": "growth", "base": "rev", "compare": "prior_year",
          "unit": "fraction"}],
    )  # fmt: skip
    out = run([
        p("us-gaap:OperatingIncomeLoss", "FY", 2025, 0, 2909, accn="k25"),
        p("us-gaap:OperatingIncomeLoss", "FY", 2024, 0, -5, accn="k24"),
        p("us-gaap:InterestExpense", "FY", 2025, 0, 727, accn="k25"),
        p("us-gaap:InterestExpense", "FY", 2024, 0, 800, accn="k24"),
        p("us-gaap:Assets", "FY", 2025, 0, 100, accn="k25"),
        p("us-gaap:Assets", "FY", 2024, 0, 80, accn="k24"),
        p("us-gaap:Revenues", "FY", 2025, 0, 110, accn="k25"),
        p("us-gaap:Revenues", "FY", 2024, 0, 100, accn="k24"),
    ], reg)  # fmt: skip
    fy25, fy24 = ("FY", 2025, 0), ("FY", 2024, 0)
    cover = out["cover"][fy25]
    assert [(n, k, v.value) for n, k, v in cover.inputs] == [("ebit", fy25, 2909),
                                                             ("ie", fy25, 727)]  # fmt: skip
    # N/M keeps its inputs: the chat explains why (ebit <= 0).
    assert out["cover"][fy24].status == "NM"
    assert [n for n, _, _ in out["cover"][fy24].inputs] == ["ebit", "ie"]
    # `assets_py` is assets of the year before, named as assets.
    assert [(n, k) for n, k, _ in out["roa"][fy25].inputs] == [
        ("oi", fy25), ("assets", fy25), ("assets", fy24)]  # fmt: skip
    assert [(n, k) for n, k, _ in out["growth"][fy25].inputs] == [("rev", fy25), ("rev", fy24)]
    rows = json.loads(inputs_json(cover))
    assert rows[1] == {"name": "ie", "period": "FY2025", "value": 727.0, "status": "OK",
                       "derivation": "REPORTED", "period_end": "2025-12-31", "accn": "k25",
                       "formula": "us-gaap:InterestExpense",
                       "tags": {"ie": ["us-gaap:InterestExpense"]}}  # fmt: skip
    # Inputs do not take part in equality (values are compared as before).
    assert out["cover"][fy25] == type(cover)(**{**cover.__dict__, "inputs": ()})


# -- the context -------------------------------------------------------------------------------


def _creg() -> CompanyRegistry:
    interest = [TagRef(tag="us-gaap:InterestExpenseNonoperating",
                       dims={"gm:BusinessGroupAxis": "gm:AutomotiveMember"})]  # fmt: skip
    concepts = {
        "operating_income": [TagRef(tag="us-gaap:OperatingIncomeLoss")],
        "interest_expense": interest,
        # A proxy candidate, for the test only.
        "revenue": [TagRef(tag="us-gaap:Revenues"),
                    TagRef(tag="us-gaap:InterestPaidNet", proxy=PAID)],
    }  # fmt: skip
    metrics = [
        MetricDef(code="ebit", display_name="EBIT", section="x", unit="USD",
                  formula="operating_income"),
        MetricDef(code="interest_expense", display_name="Interest Expense", section="x",
                  unit="USD", formula="interest_expense"),
        MetricDef(code="interest_coverage", display_name="Interest Coverage", section="x",
                  unit="ratio", formula="ebit / interest_expense", nm_when="ebit <= 0"),
    ]  # fmt: skip
    notes = {"interest_expense": "Automotive interest only (D-087)."}
    return CompanyRegistry(
        concepts=concepts, metrics=metrics, overridden={"interest_expense"}, notes=notes
    )


def _row(code, ptype, fy, fq, value, *, status="OK", derivation="COMPUTED", formula="",
         tags=None, accn="0001-26-000013", unit="ratio"):  # fmt: skip
    end = f"{fy}-12-31" if ptype == "FY" else f"{fy}-{3 * fq:02d}-30"
    return {"metric_code": code, "period_type": ptype, "fiscal_year": fy, "fiscal_quarter": fq,
            "period_end": end, "value": value, "value_status": status, "unit": unit,
            "derivation": derivation, "formula": formula, "source_tags": json.dumps(tags or {}),
            "source_accn": accn, "evidence": None}  # fmt: skip


def _input(name, period, value, tags, *, status="OK", derivation="REPORTED"):
    return {"name": name, "period": period, "value": value, "status": status,
            "derivation": derivation, "period_end": "", "accn": "0001-26-000013",
            "formula": "", "tags": tags}  # fmt: skip


def _data(**over) -> CompanyData:
    labels = load_source_labels(CONFIG_DIR)
    oi = {"operating_income": ["us-gaap:OperatingIncomeLoss"]}
    ie = {"interest_expense": [GM_TAG]}
    values = {
        "interest_coverage": [
            _row("interest_coverage", "FY", 2025, 0, 4.0013755158,
                 formula="ebit / interest_expense", tags=oi | ie),
            _row("interest_coverage", "Q", 2025, 4, None, status="NM",
                 formula="ebit / interest_expense", tags=oi | ie),
        ],
        "interest_expense": [
            _row("interest_expense", "FY", 2025, 0, 727e6, derivation="REPORTED", formula=GM_TAG,
                 tags=ie, unit="USD"),
        ],
    }  # fmt: skip
    inputs = {
        ("interest_coverage", "FY", 2025, 0): [_input("ebit", "FY2025", 2909e6, oi),
                                               _input("interest_expense", "FY2025", 727e6, ie)],
        ("interest_coverage", "Q", 2025, 4): [
            _input("ebit", "2025Q4", -3646e6, oi, derivation="DERIVED"),
            _input("interest_expense", "2025Q4", 167e6, ie, derivation="DERIVED")],
    }  # fmt: skip
    filing = SourceRef("General Motors Co 10-K filed 2026-01-27", "https://sec/gm-10k.htm",
                       "2026-01-27", None, "10-K")  # fmt: skip
    base = dict(
        company={"ticker": "GM", "name": "General Motors", "fiscal_year_end": "1231"},
        group_code="AUTOMOTIVE", group_label="Automotive Manufacturing", creg=_creg(),
        labels=labels, names={"ebit": "EBIT", "interest_expense": "Interest Expense",
                              "interest_coverage": "Interest Coverage"},
        values=values, inputs=inputs,
        findings={"interest_coverage": [{"rule": "r", "severity": "Info", "period": "FY2025",
                                         "message": "m"}]},
        coverage={"interest_coverage": "Needs interest expense; see its note."},
        peers={"interest_coverage": {"fiscal_year": 2025, "median": 9.5, "peers": 4}},
        source_of=lambda m, r: filing, filing=lambda accn: filing,
    )  # fmt: skip
    return CompanyData(**(base | over))


def test_context_explains_each_value() -> None:
    d = _data()
    ctx = build_context(d.creg.metrics[2], d)
    assert ctx["version"] == CONTEXT_VERSION
    assert ctx["metric"]["definition"] == ["Formula: EBIT / Interest Expense."]
    assert ctx["metric"]["nm_when"] == "EBIT ≤ 0"
    fy, q4 = ctx["periods"][1], ctx["periods"][0]  # quarters first, newest first
    assert (q4["period"], q4["label"], fy["period"]) == ("2025Q4", "FY2025 Q4", "FY2025")
    assert fy["value_id"] == "dp:FY2025:interest_coverage"
    assert fy["accession"] == "0001-26-000013" and "source" not in fy
    ie = fy["inputs"][1]
    assert ie["id"] == "dp:FY2025:interest_expense"
    assert ie["tags"] == [{"concept": "interest_expense", "tag": GM_TAG, "dimensional": True,
                           "candidate": "1 of 1", "fallback": False,
                           "company_override": True}]  # fmt: skip
    assert ctx["company_overrides"] == {"interest_expense": "Automotive interest only (D-087)."}
    assert q4["nm_reason"] == "Not meaningful when EBIT ≤ 0 (EBIT = $-3.65B)."
    assert ctx["peer_median"]["id"] == "dp:FY2025:interest_coverage:peer_median"
    assert ctx["dq_findings"][0]["rule"] == "r"
    assert ctx["coverage_note"] == "Needs interest expense; see its note."
    assert ctx["derivations"].keys() == {"COMPUTED", "REPORTED", "DERIVED"}
    # The one filing is listed once; every id the context uses is a data point.
    assert [f["accession"] for f in ctx["filings"]] == ["0001-26-000013"]
    points = {pt["id"]: pt for pt in ctx["data_points"]}
    used = {fy["value_id"], q4["value_id"], ctx["peer_median"]["id"],
            *(i["id"] for per in ctx["periods"] for i in per["inputs"])}  # fmt: skip
    assert used == set(points)
    assert points["dp:FY2025:interest_coverage"]["value"] == 4.0013755158
    assert points["dp:2025Q4:ebit"] == {
        "id": "dp:2025Q4:ebit",
        "label": "EBIT FY2025 Q4",
        "value": -3646e6,
        "unit": "USD",
        "status": "OK",
    }
    # Same input, same JSON and hash.
    assert context_json(ctx) == context_json(build_context(d.creg.metrics[2], d))


def test_context_flags_fallback_and_proxy_tags() -> None:
    proxy = {"revenue": ["us-gaap:InterestPaidNet"]}
    d = _data(values={"interest_expense": [
        _row("interest_expense", "FY", 2025, 0, 5e6, derivation="REPORTED",
             formula="us-gaap:InterestPaidNet", tags=proxy, unit="USD")]})  # fmt: skip
    ctx = build_context(d.creg.metrics[1], d)
    tag = ctx["periods"][0]["tags"][0]
    assert tag["fallback"] is True and tag["candidate"] == "2 of 2"
    assert tag["preferred_tags"] == ["us-gaap:Revenues"] and tag["proxy"] == PAID
    assert "nm_reason" not in ctx["periods"][0] and "peer_median" not in ctx


def test_id_label() -> None:
    assert (id_label("FY2025"), id_label("2026Q3")) == ("FY2025", "FY2026 Q3")


# -- storage and publishing --------------------------------------------------------------------


def _metricdetail(engine) -> str:
    res = run_metric_detail_stage(
        [CompanyConfig(ticker="MAR", cik=1048286)], load_registry(CONFIG_DIR),
        load_industry_config(CONFIG_DIR), None, tm.load_insights_config(CONFIG_DIR),
        load_source_labels(CONFIG_DIR), engine, now=lambda: NOW,
    )  # fmt: skip
    assert res[0].ok, res
    return res[0].detail


def test_metricdetail_stores_contexts_once_and_publish_copies_them() -> None:
    engine, app, _ = tp._env()  # metricdetail already ran once (tm._run)
    with engine.connect() as c:
        stored = {r.metric_code: r for r in c.execute(select(metric_chat_context))}
    assert set(stored) == {"total_debt", "equity", "debt_to_equity"}
    de = json.loads(stored["debt_to_equity"].context_json)
    assert de["periods"][0]["nm_reason"].startswith("Not meaningful when Stockholders' Equity ≤ 0")
    assert "0 chat contexts written" in _metricdetail(engine)
    tp._publish(engine, app)
    with app.connect() as c:
        assert c.execute(select(func.count()).select_from(MetricChatContext)).scalar_one() == 3
        text = c.execute(select(MetricChatContext.c.ContextJson)).scalars().first()
    assert json.loads(text)["company"]["ticker"] == "MAR"
    # A metric no longer shown loses its context, here and in the app.
    with engine.begin() as c:
        c.execute(metric_value.delete().where(metric_value.c.metric_code == "equity"))
    _metricdetail(engine)
    tp._publish(engine, app)
    with engine.connect() as c:
        assert "equity" not in set(c.execute(select(metric_chat_context.c.metric_code)).scalars())
    with app.connect() as c:
        assert c.execute(select(func.count()).select_from(MetricChatContext)).scalar_one() == 2


def test_peer_median_needs_three_peers_of_the_group() -> None:
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    for cid in range(1, 6):
        ti._seed(engine, cid=cid, cik=f"{cid:010d}", ticker=f"T{cid}", name=f"CO{cid}")
    with engine.begin() as c:
        c.execute(metric_value.update().values(period_type="FY", fiscal_quarter=0,
                                               fiscal_year=2025))  # fmt: skip
        c.execute(company.update().where(company.c.company_id == 5).values(is_active=False))
        own = [dict(r) for r in c.execute(select(metric_value).where(
            metric_value.c.company_id == 1, metric_value.c.metric_code == "total_debt")
        ).mappings()]  # fmt: skip
    got = peer_medians(engine.connect(), 1, "HOTELS", {"total_debt": own})
    assert got == {"total_debt": {"fiscal_year": 2025, "median": 16.915e9, "peers": 3}}
    assert peer_medians(engine.connect(), 1, "BANKS", {"total_debt": own}) == {}


# -- DQ ----------------------------------------------------------------------------------------


def test_fallback_denominator_flags_proxy_tags_only() -> None:
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    ti._seed(engine)
    creg = CompanyRegistry(
        concepts={"ie": [TagRef(tag="us-gaap:InterestExpense"),
                         TagRef(tag="us-gaap:InterestExpenseDebt"),
                         TagRef(tag="us-gaap:InterestPaidNet", proxy=PAID)]},
        metrics=[MetricDef(code="cover", display_name="Interest Coverage", section="x",
                           unit="ratio", formula="ebit / ie")],
    )  # fmt: skip
    base = dict(company_id=1, metric_code="cover", period_type="FY", fiscal_quarter=0,
                period_end="2025-12-31", value=2.0, value_status="OK", unit="ratio",
                derivation="COMPUTED", formula="ebit / ie", source_tags="{}", source_accn="a",
                computed_at=NOW)  # fmt: skip

    def findings(tag: str, traits=frozenset()):
        with engine.begin() as c:
            c.execute(metric_value.delete().where(metric_value.c.metric_code == "cover"))
            c.execute(metric_input.delete())
            c.execute(insert(metric_value), [base | {"fiscal_year": 2024},
                                             base | {"fiscal_year": 2025}])  # fmt: skip
            c.execute(insert(metric_input).values(
                company_id=1, metric_code="cover", period_type="FY", fiscal_year=2025,
                fiscal_quarter=0, inputs_json=json.dumps([
                    _input("ebit", "FY2025", 2, {"oi": ["us-gaap:OperatingIncomeLoss"]}),
                    _input("ie", "FY2025", 1, {"ie": [tag]})])))  # fmt: skip
            values = [dict(r) for r in c.execute(select(metric_value)).mappings()]
            return fallback_denominator(c, 1, values, creg.metrics, creg, traits,
                                        FallbackDenominator(severity="Info"))  # fmt: skip

    assert findings("us-gaap:InterestExpense") == []
    assert findings("us-gaap:InterestExpenseDebt") == []  # a later candidate, same measure
    [info] = findings("us-gaap:InterestPaidNet")
    assert (info.severity, info.metric_code, info.period) == ("Info", "cover", "FY2025")
    assert PAID in info.message and "captive finance" not in info.message
    [warn] = findings("us-gaap:InterestPaidNet", frozenset({"captive_finance"}))
    assert warn.severity == "Warning" and "captive finance arm" in warn.message


# -- config ------------------------------------------------------------------------------------


def test_every_override_concept_says_why() -> None:
    """The chat quotes these notes (D-090); a new override concept needs one."""
    reg = load_registry(CONFIG_DIR)
    missing = [f"{t}.{c}" for t, o in reg.overrides.items() for c, d in o.concepts.items()
               if not d.note]  # fmt: skip
    assert missing == []
    proxies = [t.tag for c in reg.concepts.values() for t in c.tags if t.proxy]
    assert proxies == ["us-gaap:InterestPaidNet"]
