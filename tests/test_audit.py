"""audit-insights (Phases 5 and 8): evidence is re-checked against what the model saw. Proxy facts
and litigation matters reach the model as rendered text, and loan rows read by table cell keep
"row label | value" evidence (Phase 16)."""

import json
from datetime import UTC, datetime

from sqlalchemy import create_engine, insert

from uw_pipeline.enrich.audit import audit
from uw_pipeline.schema import (
    board_composition,
    company,
    filing_section,
    insight,
    insight_evidence,
    legal_matter,
    loan_portfolio_segment,
    metadata,
    source_document,
)

NOW = datetime(2026, 10, 20, tzinfo=UTC)
LOANS = "Consumer credit portfolio Residential real estate(a) 300,664 303,531 Auto 66,464"


def _db():
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    with engine.begin() as c:
        c.execute(insert(company).values(
            company_id=1, cik="0000019617", ticker="JPM", name="JPMorgan", legal_name="JPM",
            industry_group="BANKS", is_financial=True, is_active=True, source_url="u",
            source_hash="h", raw_path="p", last_refreshed_at=NOW))  # fmt: skip
        doc = dict(company_id=1, source_type="SEC_FILING", doc_role="PRIMARY", doc_name="d",
                   url="u")  # fmt: skip
        c.execute(insert(source_document), [
            doc | {"doc_id": 1, "form_type": "DEF 14A", "accession_no": "a1",
                   "filed_at": "2026-04-01"},
            doc | {"doc_id": 2, "form_type": "10-Q", "accession_no": "a2",
                   "filed_at": "2026-08-01"},
        ])  # fmt: skip
        c.execute(insert(filing_section).values(
            doc_id=2, section_code="10Q-I-2", start_offset=0, char_count=len(LOANS), text=LOANS,
            text_hash="h"))  # fmt: skip
        c.execute(insert(board_composition).values(
            company_id=1, doc_id=1, board_size=12, committees_json="[]",
            evidence_json=json.dumps({"board_size": "The Board has 12 directors."}),
            model="m", prompt_version="board_v2", input_hash="h", updated_at=NOW))  # fmt: skip
        c.execute(insert(legal_matter).values(
            company_id=1, matter_key="k", matter_type="securities_class_action",
            name="In re Example Securities Litigation", status="open", wells_notice=False,
            quote="A putative class action was filed.", first_doc_id=2, last_doc_id=2,
            first_disclosed="2026-08-01", last_seen="2026-08-01", review_status="BASELINE",
            model="m", prompt_version="legal_matters_v1", updated_at=NOW))  # fmt: skip
        c.execute(insert(loan_portfolio_segment).values(
            company_id=1, period_end="2026-06-30", segment="Residential real estate",
            outstanding=300_664e6, evidence="Residential real estate(a) | 300,664",
            source_doc_id=2, updated_at=NOW))  # fmt: skip
        c.execute(insert(insight).values(
            insight_id=1, company_id=1, kind="KEY", ordinal=1, title="t", summary="s",
            is_current=True, model="m", prompt_version="v", input_hash="h",
            created_at=NOW))  # fmt: skip
        ev = dict(insight_id=1, source_kind="DOCUMENT", source_title="title")
        c.execute(insert(insight_evidence), [
            ev | {"doc_id": 1, "snippet": "Directors: 12. \"The Board has 12 directors.\""},
            ev | {"doc_id": 2, "snippet": "In re Example Securities Litigation (securities "
                                          "class action; open"},
        ])  # fmt: skip
    return engine


def test_rendered_proxy_and_litigation_text_and_table_cells_are_found() -> None:
    engine = _db()
    with engine.connect() as c:
        rep = audit(c, sample=10)
    assert (rep.snippets, rep.snippets_found) == (2, 2)
    assert (rep.loan_rows, rep.loan_rows_found) == (1, 1)
    assert not rep.failed, rep.failures


def test_a_quote_the_model_did_not_see_is_reported() -> None:
    engine = _db()
    with engine.begin() as c:
        c.execute(insert(insight_evidence).values(
            insight_id=1, source_kind="DOCUMENT", doc_id=1, source_title="title",
            snippet="The Board has 15 directors."))  # fmt: skip
    with engine.connect() as c:
        rep = audit(c, sample=10)
    assert rep.failed and rep.snippets_found == 2 and len(rep.failures) == 1
