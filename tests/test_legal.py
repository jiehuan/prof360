"""Phase 15e (D-050): litigation and regulatory matters: grounding, the two models' agreement,
matching across filings, the review queue, signals and the insight input.

The filing text is a recorded excerpt of Enphase's 10-Q filed 2026-04-28 (Item 1, Legal
Proceedings); model answers come from tests/fake_llm.py.
"""

from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine, insert, select

from tests.conftest import CONFIG_DIR
from tests.fake_llm import FakeLLM
from uw_pipeline.clients.storage import LocalStorage
from uw_pipeline.config import CompanyConfig
from uw_pipeline.enrich.legal import (
    Matter,
    agreed,
    checked,
    legal_matters,
    match,
    matter_key,
    same_subject,
    similarity,
)
from uw_pipeline.enrich.llm import load_llm_config
from uw_pipeline.enrich.package import legal_text
from uw_pipeline.enrich.schemas import LegalMatterRead
from uw_pipeline.jobs.run_legal import run_legal_stage
from uw_pipeline.jobs.run_review import decide, pending
from uw_pipeline.schema import company, legal_matter, metadata, source_document
from uw_pipeline.signals.detect import legal_signals
from uw_pipeline.signals.rules import load_signal_rules

NOW = datetime(2026, 10, 17, tzinfo=UTC)
EXCERPT = (Path(__file__).parent / "fixtures" / "sec" / "legal"
           / "enph_10q_2026-04-28_legal.txt").read_text()  # fmt: skip
SOURCE = " ".join(EXCERPT.split())
RULES = load_signal_rules(CONFIG_DIR)

HAYES = ("On July 15, 2024, a putative class action complaint was filed against us, our chief "
         "executive officer and our chief financial officer")  # fmt: skip
PENSION = ("On December 13, 2024, another putative class action complaint was filed naming us, "
           "our chief executive officer and our chief products officer")  # fmt: skip
TRIPATHI = ("On February 17, 2026, a putative class action complaint was filed naming us, our "
            "chief executive officer, and our chief financial officer")  # fmt: skip
IBARRA = ("On July 16, 2024, a shareholder derivative lawsuit was filed purportedly on our behalf "
          "against the Initial Defendants")  # fmt: skip


def read(name: str, quote: str, **over) -> dict:
    base = {"name": name, "matter_type": "securities_class_action", "agency": None,
            "status": "open", "started": None, "wells_notice": False, "accrual_usd": None,
            "quote": quote}  # fmt: skip
    return base | over


def matters(*reads: dict) -> list[Matter]:
    return checked([LegalMatterRead.model_validate(r) for r in reads], SOURCE)


def test_checked_keeps_what_the_quote_supports() -> None:
    [m] = matters(read("Tripathi v. Enphase Energy, Inc.", TRIPATHI, started="February 17, 2026"))
    assert m.started == "February 17, 2026"
    # A date or amount not in the quote is dropped (the matter stays); so is a Wells notice
    # the quote does not mention, and a quote not in the filing drops the matter.
    caption = "captioned Tripathi v. Enphase Energy, Inc., et al., Case No. 4:26-cv-01380"
    [m] = matters(read("Tripathi", caption, started="February 17, 2026", accrual_usd=5e6,
                       wells_notice=True))  # fmt: skip
    assert (m.started, m.accrual, m.wells_notice) == (None, None, False)
    assert matters(read("Made up", "a class action was filed against us in 2019")) == []
    # JSON debris after the quote (a model answer seen in testing) is ignored.
    sentence = (
        "The Court has not yet appointed a lead plaintiff or lead counsel in the Tripathi Action."
    )
    [m] = matters(read("Tripathi", sentence + "','"))
    assert m.quote == sentence


def test_models_must_agree_on_the_type() -> None:
    first = matters(
        read("Hayes v. Enphase Energy, Inc.", HAYES, started="July 15, 2024"),
        read("Tripathi Action", TRIPATHI, started="February 17, 2026", status="open"),
    )
    second = matters(
        read("Hayes v. Enphase Energy (Securities Class Action)", HAYES, matter_type="derivative"),
        read("Tripathi v. Enphase Energy, Inc., et al.", TRIPATHI, started="February 17, 2026",
             status="unknown"),
    )  # fmt: skip
    kept, unpaired = agreed(first, second)
    assert [(m.name, m.status, m.started) for m in kept] == [
        ("Tripathi Action", "unknown", "February 17, 2026"),  # paired by their quotes
    ]
    assert unpaired == 2  # Hayes: securities class action for one model, derivative for the other


def _m(name: str, kind: str = "product_liability", **over) -> Matter:
    base = {"agency": None, "status": "open", "started": None, "wells_notice": False,
            "accrual": None, "quote": "q"}  # fmt: skip
    return Matter(name=name, matter_type=kind, **(base | over))


def _row(m: Matter) -> dict:
    return {"matter_key": matter_key(m), "matter_type": m.matter_type, "name": m.name,
            "started": m.started, "agency": m.agency}  # fmt: skip


def test_matching_names_across_filings() -> None:
    pfizer, enphase = frozenset({"pfizer"}), frozenset({"enphase", "energy"})
    assert (
        similarity("Asbestos personal injury lawsuits", "Asbestos product liability lawsuits") == 1
    )
    assert similarity("Docetaxel product liability MDL",
                      "Docetaxel hair loss and eye injury litigation") == 1  # fmt: skip
    assert (
        similarity(
            "In re Southwest Airlines Securities Litigation (S.D. Tex.)",
            "In re Southwest Airlines Securities Litigation - technology outage",
        )
        == 1
    )
    # Two derivative suits of the same company, a year apart, stay two matters (the stage
    # ignores the company's own name: it names every suit).
    assert similarity("In re Enphase Energy, Inc. Stockholder Derivative Litigation",
                      "In re Enphase Energy, Inc. 2025 Shareholder Derivative Litigation",
                      enphase) < 0.6  # fmt: skip
    # The company's own name is not a subject.
    assert similarity("Pfizer securities class action", "Pfizer Inc. derivative litigation",
                      pfizer) < 0.6  # fmt: skip

    stored = [_row(_m("Zantac product litigation (MDL and state court cases)")),
              _row(_m("Amrapali investigation (India Enforcement Directorate)",
                      "regulatory_investigation"))]  # fmt: skip
    # Another type matches only with nearly the same name: an investigation that became an
    # enforcement action, but not Zantac suits brought by states.
    hit = match(_m("Amrapali investigation (India Enforcement Directorate)", "enforcement"), stored)
    assert hit is not None and hit["matter_type"] == "regulatory_investigation"
    assert (
        match(_m("Zantac civil actions by the State of New Mexico", "enforcement"), stored) is None
    )
    # The same full start date, or a government matter's agency and subject, also match.
    luv = _row(_m("In re Southwest Airlines Securities Litigation", "securities_class_action",
                  started="January 10, 2023"))  # fmt: skip
    assert match(_m("Technology outage class action", "securities_class_action",
                    started="January 10, 2023"), [luv]) is not None  # fmt: skip
    fcc = "Federal Communications Commission"
    assert same_subject(
        "Huawei Network Deployment Investigations - FCC Letter of Inquiry",
        fcc,
        "FCC Enforcement Bureau Letter of Inquiry regarding Huawei",
        fcc,
    )
    assert not same_subject("Huawei equipment inquiry", fcc, "Huawei equipment CID",
                            "U.S. Department of Justice")  # fmt: skip


def _db():
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    with engine.begin() as c:
        c.execute(insert(company).values(
            company_id=1, cik="0001463101", ticker="ENPH", name="Enphase Energy",
            legal_name="ENPHASE ENERGY, INC.", industry_group="ENERGY_EQUIPMENT",
            is_financial=False, is_active=True, fiscal_year_end="1231", source_url="u",
            source_hash="h", raw_path="p", last_refreshed_at=NOW))  # fmt: skip
    return engine


def _filing(engine, storage, doc_id: int, form: str, filed: str, text: str) -> None:
    name = f"enph-{doc_id}.htm"
    storage.write_bytes(
        name,
        (
            "<html><body>"
            + "".join(f"<p>{line}</p>" for line in text.splitlines())
            + "</body></html>"
        ).encode(),
    )
    with engine.begin() as c:
        c.execute(insert(source_document).values(
            doc_id=doc_id, company_id=1, source_type="SEC_FILING", form_type=form,
            doc_role="PRIMARY", accession_no=f"0001463101-26-{doc_id:06d}", doc_name=name,
            filed_at=filed, url="u", content_hash="h", raw_path=name))  # fmt: skip


def test_stage_baseline_review_signals_and_insight_input(tmp_path) -> None:
    engine, storage = _db(), LocalStorage(tmp_path)
    ten_k = EXCERPT.split("On February 17, 2026")[0]  # the 10-K predates the Tripathi suit
    _filing(engine, storage, 1, "10-K", "2026-02-17", ten_k)
    _filing(engine, storage, 2, "10-Q", "2026-04-28", EXCERPT)
    cfg = load_llm_config(CONFIG_DIR)
    later: list[dict] = []  # matters a later filing adds

    def answer(model, user):
        found = [
            read("Hayes v. Enphase Energy, Inc.", HAYES, started="July 15, 2024"),
            read("Pension Fund Action", PENSION, started="December 13, 2024"),
        ]
        if "10-Q filed" in user:
            found.append(read("Tripathi v. Enphase Energy, Inc.", TRIPATHI,
                              started="February 17, 2026"))  # fmt: skip
        if "10-Q filed 2026-07-28" in user:
            found += later
        return {"matters": found}

    llm = FakeLLM(answer)

    def run():
        res = run_legal_stage([CompanyConfig(ticker="ENPH", cik=1463101)], (llm, cfg), storage,
                              engine, now=lambda: NOW)  # fmt: skip
        assert res[0].ok, res
        with engine.connect() as c:
            return {r.name: r for r in c.execute(select(legal_matter))}

    rows = run()
    assert len(llm.calls) == 4  # two filings, two models each
    # Everything in the company's first run is its baseline: no review items.
    assert {n: (r.review_status, r.first_disclosed, r.last_seen) for n, r in rows.items()} == {
        "Hayes v. Enphase Energy, Inc.": ("BASELINE", "2026-02-17", "2026-04-28"),
        "Pension Fund Action": ("BASELINE", "2026-02-17", "2026-04-28"),
        "Tripathi v. Enphase Energy, Inc.": ("BASELINE", "2026-04-28", "2026-04-28"),
    }
    assert run() == rows and len(llm.calls) == 4  # rerun: cached calls, nothing changes

    # A later filing adds a derivative suit: NEW, in the review queue until a reviewer decides.
    later.append(read("Ibarra v. Kothandaraman", IBARRA, matter_type="derivative",
                      started="July 16, 2024"))  # fmt: skip
    _filing(engine, storage, 3, "10-Q", "2026-07-28", EXCERPT)
    rows = run()
    assert rows["Ibarra v. Kothandaraman"].review_status == "NEW"
    with engine.begin() as c:
        [item] = pending(c)
        assert (item.item_type, item.title) == (
            "MATTER",
            "2026-07-28 derivative: Ibarra v. Kothandaraman",
        )
        current = legal_matters(c, 1)
        assert len(current) == 4
        signals = legal_signals(current, RULES, "2026-10-17")
        # Hayes began more than lookback_days ago; the derivative suit is too old as well.
        assert sorted((s.rule_id, s.severity, s.event_date) for s in signals) == [
            ("legal_securities_class_action", "Critical", "2024-12-13"),
            ("legal_securities_class_action", "Critical", "2026-02-17"),
        ]
        assert signals[0].snippet and signals[0].accession_no == "0001463101-26-000003"
        decide(c, item.short, "REJECTED", "Pat Lee", "duplicate", NOW)
        assert pending(c) == []
        assert {m["name"] for m in legal_matters(c, 1)} == {
            "Hayes v. Enphase Energy, Inc.", "Pension Fund Action",
            "Tripathi v. Enphase Energy, Inc."}  # fmt: skip
        text = legal_text(legal_matters(c, 1))
    assert text.splitlines()[0].startswith("Hayes v. Enphase Energy, Inc. (securities class "
                                           "action; open; began July 15, 2024; last disclosed "
                                           "2026-07-28).")  # fmt: skip


def _matter(**over) -> dict:
    base = {"matter_key": "k", "matter_type": "regulatory_investigation", "name": "SEC inquiry",
            "agency": "SEC", "status": "open", "started": None, "wells_notice": False,
            "quote": "q", "first_disclosed": "2026-02-01", "last_seen": "2026-08-01",
            "last_doc_id": 1, "accession_no": "a"}  # fmt: skip
    return base | over


def test_legal_signal_rules() -> None:
    def ids(*ms: dict) -> list[str]:
        return [s.rule_id for s in legal_signals(ms, RULES, "2026-10-17")]

    assert ids(_matter()) == ["legal_regulatory_investigation"]  # open, no date given
    assert ids(_matter(wells_notice=True)) == ["legal_wells_notice"]
    assert ids(_matter(status="settled")) == []  # no date and not known to be open
    assert ids(_matter(status="settled", started="March 2025")) == [
        "legal_regulatory_investigation"]  # fmt: skip
    assert ids(_matter(status="dismissed", started="March 2026")) == []
    assert ids(_matter(last_seen="2025-06-30")) == []  # no longer disclosed
    assert ids(_matter(matter_type="product_liability")) == []  # insight input only
