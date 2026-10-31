"""Phase 17c (D-058): clinical-stage pipeline facts: grounding, the two models' agreement, the
stage and the insight input.

The filing text is a recorded excerpt of Cogent Biosciences' 10-K filed 2026-02-17 (business
section: bezuclastinib in NonAdvSM, AdvSM and GIST, an FGFR2/3 inhibitor); model answers come
from tests/fake_llm.py.
"""

from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine, insert, select

from tests.conftest import CONFIG_DIR
from tests.fake_llm import FakeLLM
from uw_pipeline.clients.storage import LocalStorage
from uw_pipeline.config import CompanyConfig
from uw_pipeline.enrich.clinical import (
    agreed,
    checked,
    clinical_text,
    current_programs,
    program_key,
)
from uw_pipeline.enrich.llm import load_llm_config
from uw_pipeline.enrich.schemas import ClinicalProgramRead
from uw_pipeline.jobs.run_clinical import run_clinical_stage
from uw_pipeline.schema import clinical_program, company, metadata, source_document

NOW = datetime(2026, 10, 21, tzinfo=UTC)
EXCERPT = (Path(__file__).parent / "fixtures" / "sec" / "clinical"
           / "cogt_10k_2026-02-17_pipeline.txt").read_text()  # fmt: skip
SOURCE = " ".join(EXCERPT.split())

NDA = ("We submitted the first New Drug Application (“NDA”) for bezuclastinib in "
       "patients with NonAdvSM in December 2025")  # fmt: skip
LAUNCH = ("we expect to launch bezuclastinib commercially in the United States in the second "
          "half of 2026 to treat patients with NonAdvSM")  # fmt: skip
APEX = ("APEX is our registration-directed global, open-label, multi-center, Phase 2 clinical "
        "trial in patients with AdvSM")  # fmt: skip
ADV_NDA = "expect to submit an NDA in the first half of 2026 for patients with AdvSM"
PEAK = ("PEAK is our randomized open-label, global Phase 3 clinical trial designed to evaluate "
        "the safety, tolerability, and efficacy of bezuclastinib in combination with "
        "sunitinib")  # fmt: skip
FGFR = "We also have an ongoing Phase 1 study of our novel internally developed FGFR2/3 inhibitor"


def read(name: str, indication: str, phase: str, quote: str, milestone: str | None = None,
         timing: str | None = None, mquote: str | None = None) -> dict:  # fmt: skip
    return {"name": name, "indication": indication, "phase": phase, "quote": quote,
            "next_milestone": milestone, "milestone_timing": timing,
            "milestone_quote": mquote}  # fmt: skip


ALL = [
    read("bezuclastinib", "NonAdvSM", "submitted", NDA, "U.S. commercial launch",
         "second half of 2026", LAUNCH),
    read("bezuclastinib", "AdvSM", "phase_2", APEX,
         "NDA submission", "first half of 2026", ADV_NDA),
    read("bezuclastinib", "GIST", "phase_3", PEAK),
    read("FGFR2/3 inhibitor", "solid tumors", "phase_1", FGFR),
]  # fmt: skip


def programs(*reads: dict):
    return checked([ClinicalProgramRead.model_validate(r) for r in reads], SOURCE)


def test_checked_keeps_what_the_quote_supports() -> None:
    kept = programs(*ALL)
    # APEX's quote names the trial, not the candidate: the candidate is named just before it.
    assert [(p.name, p.indication, p.phase) for p in kept] == [
        ("bezuclastinib", "NonAdvSM", "submitted"), ("bezuclastinib", "AdvSM", "phase_2"),
        ("bezuclastinib", "GIST", "phase_3"), ("FGFR2/3 inhibitor", "solid tumors", "phase_1"),
    ]  # fmt: skip
    # A candidate named nowhere near the quote is not kept.
    assert programs(read("CGT9999", "AdvSM", "phase_2", APEX)) == []
    assert (kept[0].next_milestone, kept[0].milestone_timing) == (
        "U.S. commercial launch", "second half of 2026")  # fmt: skip
    # A phase the quote does not state, a quote not in the filing, a timing not in the
    # milestone quote: the program, the program, the milestone are dropped.
    assert programs(read("bezuclastinib", "GIST", "approved", PEAK)) == []
    assert programs(read("bezuclastinib", "GIST", "phase_3",
                         "bezuclastinib entered a Phase 3 trial in 2019")) == []  # fmt: skip
    [p] = programs(read("bezuclastinib", "NonAdvSM", "submitted", NDA, "launch",
                        "first half of 2026", LAUNCH))  # fmt: skip
    assert p.next_milestone is None and p.milestone_quote is None


def test_both_models_must_give_the_same_phase_and_timing() -> None:
    first = programs(*ALL)
    other = [dict(r) for r in ALL]
    other[2]["phase"] = "phase_2"  # GIST read as Phase 2: not in its quote, so not paired
    other[0]["milestone_timing"] = "2H 2026"  # the same program, another timing: no milestone
    kept, unpaired = agreed(first, programs(*other))
    by = {(p.name, p.indication): p for p in kept}
    assert set(by) == {("bezuclastinib", "NonAdvSM"), ("bezuclastinib", "AdvSM"),
                       ("FGFR2/3 inhibitor", "solid tumors")}  # fmt: skip
    assert by["bezuclastinib", "NonAdvSM"].next_milestone is None
    assert unpaired == 1  # GIST from the first model only
    # Two readings of one program with different (each grounded) phases are not kept either.
    nda = programs(read("bezuclastinib", "NonAdvSM", "submitted", NDA))
    apex = APEX + (" evaluating the safety, efficacy, pharmacokinetic, and pharmacodynamic "
                   "profiles of bezuclastinib")  # fmt: skip
    as_phase_2 = programs(read("bezuclastinib", "NonAdvSM", "phase_2", apex))
    assert len(nda) == len(as_phase_2) == 1
    assert agreed(nda, as_phase_2) == ([], 0)  # paired, but the phases differ
    # Indications keep one candidate's programs apart.
    assert len({program_key(p) for p in first}) == 4


def _db():
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    with engine.begin() as c:
        c.execute(insert(company).values(
            company_id=1, cik="0001622229", ticker="COGT", name="Cogent Biosciences",
            legal_name="Cogent Biosciences, Inc.", industry_group="PHARMA", is_financial=False,
            is_active=True, fiscal_year_end="1231", source_url="u", source_hash="h", raw_path="p",
            last_refreshed_at=NOW))  # fmt: skip
    return engine


def _filing(engine, storage, doc_id: int, form: str, filed: str, text: str) -> None:
    name = f"cogt-{doc_id}.htm"
    body = "".join(f"<p>{line}</p>" for line in text.splitlines())
    # Non-ASCII characters (the curly quotes around "NDA") as entities, as filings carry them.
    storage.write_bytes(
        name, f"<html><body>{body}</body></html>".encode("ascii", "xmlcharrefreplace")
    )
    with engine.begin() as c:
        c.execute(insert(source_document).values(
            doc_id=doc_id, company_id=1, source_type="SEC_FILING", form_type=form,
            doc_role="PRIMARY", accession_no=f"0001622229-26-{doc_id:06d}", doc_name=name,
            filed_at=filed, url="u", content_hash="h", raw_path=name))  # fmt: skip


def test_stage_stores_current_programs_and_keeps_old_ones(tmp_path) -> None:
    engine, storage = _db(), LocalStorage(tmp_path)
    _filing(engine, storage, 1, "10-K", "2026-02-17", EXCERPT)
    llm_cfg = load_llm_config(CONFIG_DIR)
    found = list(ALL)
    llm = FakeLLM(lambda model, user: {"programs": found})
    cogent = CompanyConfig(ticker="COGT", cik=1622229, traits=["clinical_stage"])

    def run():
        res = run_clinical_stage([cogent, CompanyConfig(ticker="PFE", cik=78003)],
                                 (llm, llm_cfg), storage, engine, now=lambda: NOW)  # fmt: skip
        assert [r.ok for r in res] == [True, True], res
        assert res[1].detail == "not clinical-stage"
        with engine.connect() as c:
            return {r.indication: r for r in c.execute(select(clinical_program))}

    rows = run()
    assert len(llm.calls) == 2  # one filing, two models; PFE is not read
    assert {k: (r.phase, r.is_current) for k, r in rows.items()} == {
        "NonAdvSM": ("submitted", True), "AdvSM": ("phase_2", True), "GIST": ("phase_3", True),
        "solid tumors": ("phase_1", True),
    }  # fmt: skip
    # The insight package lists the current programs, most advanced first, with the quotes.
    with engine.connect() as c:
        current = current_programs(c, 1)
    text = clinical_text(current)
    assert text.splitlines()[0].startswith("bezuclastinib (NonAdvSM; submitted); next: U.S. "
                                           "commercial launch, second half of 2026.")  # fmt: skip
    assert NDA in text and "FGFR2/3 inhibitor (solid tumors; phase 1)" in text
    # A newer 10-K without a program: it is kept, no longer current (never deleted).
    found[:] = [r for r in ALL if r["indication"] != "solid tumors"]
    _filing(engine, storage, 2, "10-K", "2027-02-16", EXCERPT + "\nFiscal 2026.")
    rows = run()
    assert rows["solid tumors"].is_current is False and rows["GIST"].is_current is True
