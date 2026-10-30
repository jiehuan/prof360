from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, func, select

from tests.conftest import CONFIG_DIR
from tests.fake_llm import NOTHING, REFUSED, FakeLLM, rulings, signals
from uw_pipeline.enrich.grounding import is_grounded
from uw_pipeline.enrich.llm import load_llm_config
from uw_pipeline.enrich.schemas import SectionSignals, strict_json_schema
from uw_pipeline.schema import llm_call, metadata
from uw_pipeline.signals.detect import Detected, Doc, eight_k_signals
from uw_pipeline.signals.rules import load_signal_rules
from uw_pipeline.signals.vote import Answer, Voter, decide, merge

RULES = load_signal_rules(CONFIG_DIR)
CONFIG = load_llm_config(CONFIG_DIR)
SONNET, OPUS = CONFIG.extractor.model, CONFIG.tiebreaker.model
NOW = datetime(2026, 10, 13, tzinfo=UTC)

DEPARTURE = ("Dave Denton notified the Company that he will step down from his current "
             "position as Chief Financial Officer effective August 15, 2026.")  # fmt: skip
SEGMENT_CEO = ("Piepszak, 54, Co-Chief Executive Officer of the Commercial & Investment Bank, "
               "has been named as a Chief Operating Officer of the Firm.")  # fmt: skip


def run(text, llm, item="5.02", rule=None):
    """Vote on one 8-K. `rule` overrides the rule detections (to simulate a rule misfire)."""
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    doc = Doc(7, "8-K", "PRIMARY", "acc-7", "2026-06-18", None, f"{item},9.01")
    sections = {f"8K-{item}": f"Item {item}\n{text}"}
    if rule is None:
        rule = eight_k_signals([doc], {7: sections}, RULES, ("Acme",))
    with engine.begin() as conn:
        voter = Voter(llm, CONFIG, RULES, conn, lambda: NOW)
        result = voter.vote_document(doc, "ACME CORP", sections, rule)
        n_calls = conn.execute(select(func.count()).select_from(llm_call)).scalar_one()
    assert result is not None
    voted, rows = result
    return merge(rule, {7: voted}, RULES), {r.topic: r for r in rows}, voter, n_calls


def by(answer_map):
    return FakeLLM(lambda model, user: answer_map[model])


def test_agreement_needs_no_tiebreak() -> None:
    llm = by({SONNET: signals(leadership=[("CFO", "departure", "Dave Denton", DEPARTURE)])})
    out, rows, _, _ = run(DEPARTURE, llm)
    assert llm.calls == [SONNET]
    assert rows["leadership:CFO"].status == "UNANIMOUS"
    assert [(s.rule_id, s.review_status, s.method) for s in out] == [
        ("8k_5.02_cfo_departure", "UNANIMOUS", "VOTE")
    ]


def test_rule_match_voted_down_is_kept_as_overruled() -> None:
    # Real sentence the first rule version misread as a CFO appointment: the CFO is only named
    # as the person a new accounting VP reports to. Simulate that misfire as the rule vote.
    text = ("Prior to the effective date of his appointment, Mr. Crockett will serve as Vice "
            "President, Accounting of the Company reporting to Sherry House, the Company's "
            "Chief Financial Officer.")  # fmt: skip
    misfire = Detected(
        rule_id="8k_5.02_cfo_appointment", category="Governance & Leadership",
        subcategory="Leadership Change", severity="Info", title="CFO appointment",
        description="", event_date="2026-06-18", accession_no="acc-7", doc_id=7,
        snippet=text, key_extra="CFO",
    )  # fmt: skip
    llm = by({SONNET: NOTHING, OPUS: rulings(("leadership:CFO", "none", ""))})
    out, rows, voter, _ = run(text, llm, rule=[misfire])
    assert llm.calls == [SONNET, OPUS]
    vote = rows["leadership:CFO"]
    assert (vote.rule_value, vote.outcome, vote.status) == ("appointment", "none", "OVERRULED")
    # Not deleted: kept, clearly labelled, for review (the two LLMs could share a mistake).
    assert [(s.rule_id, s.review_status) for s in out] == [("8k_5.02_cfo_appointment", "OVERRULED")]
    assert "overruled" in out[0].title
    assert voter.stats["tiebreaks"] == 1 and voter.stats["overruled"] == 1


def test_llm_catches_rule_miss_confirmed_by_tiebreak() -> None:
    text = "Mr. Smith has informed the Board that he intends to retire from the Company at year end, ending his tenure as CFO."
    quote = "ending his tenure as CFO"
    llm = by({
        SONNET: signals(leadership=[("CFO", "departure", "Mr. Smith", quote)]),
        OPUS: rulings(("leadership:CFO", "departure", quote)),
    })  # fmt: skip
    out, rows, _, _ = run(text, llm)
    assert rows["leadership:CFO"].rule_value == "none"
    assert (rows["leadership:CFO"].outcome, rows["leadership:CFO"].status) == (
        "departure", "MAJORITY",
    )  # fmt: skip
    sig = next(s for s in out if s.rule_id == "8k_5.02_cfo_departure")
    assert sig.snippet == quote and "Mr. Smith" in sig.description
    assert sig.votes_json is not None and '"rules": "none"' in sig.votes_json


def test_three_way_split_is_disputed() -> None:
    llm = by({
        SONNET: signals(leadership=[("CFO", "appointment", None, "Chief Financial Officer effective August 15")]),
        OPUS: rulings(("leadership:CFO", "none", "")),
    })  # fmt: skip
    out, rows, _, _ = run(DEPARTURE, llm)  # rules: departure, Sonnet: appointment, Opus: none
    assert rows["leadership:CFO"].status == "DISPUTED"
    sig = next(s for s in out if s.rule_id.startswith("8k_5.02_cfo"))
    assert sig.review_status == "DISPUTED" and sig.title.startswith("Possible CFO departure")


def test_ungrounded_llm_claim_counts_as_none() -> None:
    llm = by({SONNET: signals(leadership=[("CEO", "departure", "X", "The CEO resigned today.")]),
              OPUS: NOTHING})  # fmt: skip
    _, rows, voter, _ = run(SEGMENT_CEO, llm)
    assert rows["leadership:CEO"].extractor_value == "none"
    assert voter.stats["ungrounded"] == 1
    assert llm.calls == [SONNET]  # rules and grounded LLM both say none: no tie-break


def test_listing_transfer_vote() -> None:
    text = "On September 25, 2026, the Company notified the NYSE of its intention to voluntarily de-list its common stock."
    llm = by(
        {
            SONNET: signals(
                findings=[("listing_voluntary_transfer", "voluntarily de-list its common stock")]
            )
        }
    )
    out, rows, _, _ = run(text, llm, item="3.01")
    assert rows["listing"].outcome == "voluntary_transfer"
    assert [(s.rule_id, s.severity, s.review_status) for s in out] == [
        ("8k_3.01", "Info", "UNANIMOUS")
    ]


def test_cached_answers_are_reused() -> None:
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    llm = by({SONNET: signals(leadership=[("CFO", "departure", None, DEPARTURE)])})
    doc = Doc(7, "8-K", "PRIMARY", "acc-7", "2026-06-18", None, "5.02")
    sections = {"8K-5.02": DEPARTURE}
    rule = eight_k_signals([doc], {7: sections}, RULES)
    for _ in range(2):
        with engine.begin() as conn:
            Voter(llm, CONFIG, RULES, conn, lambda: NOW).vote_document(doc, "ACME", sections, rule)
    assert llm.calls == [SONNET]


def test_unusable_llm_output_keeps_rule_result() -> None:
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    doc = Doc(7, "8-K", "PRIMARY", "acc-7", "2026-06-18", None, "5.02")
    with engine.begin() as conn:
        voter = Voter(FakeLLM(lambda m, u: REFUSED), CONFIG, RULES, conn, lambda: NOW)
        assert voter.vote_document(doc, "ACME", {"8K-5.02": DEPARTURE}, []) is None
        err = conn.execute(select(llm_call.c.error)).scalar_one()
    assert err == "declined"


@pytest.mark.parametrize(
    ("rule", "ext", "tb", "expected"),
    [
        ("present", "present", None, ("present", "UNANIMOUS")),
        ("present", "none", "none", ("none", "OVERRULED")),
        ("none", "present", "none", ("none", "MAJORITY")),
        ("none", "present", "present", ("present", "MAJORITY")),
        ("departure", "appointment", "none", ("disputed", "DISPUTED")),
    ],
)
def test_decide(rule, ext, tb, expected) -> None:
    outcome, status, _ = decide(Answer(rule), Answer(ext), Answer(tb) if tb else None)
    assert (outcome, status) == expected


def test_grounding_tolerates_formatting_only() -> None:
    src = "We have served as the Company\u2019s auditor\nsince 2002."
    assert is_grounded("served as the Company's auditor since 2002", src)
    assert is_grounded('"We have served as the company\'s auditor since 2002..."', src)
    assert not is_grounded("served as auditor since 2003", src)
    assert not is_grounded("since", src)  # too short to mean anything


def test_schema_keeps_fields_named_title() -> None:
    from uw_pipeline.enrich.schemas import BehavioralInsights, KeyInsights

    for model in (BehavioralInsights, KeyInsights):
        schema = strict_json_schema(model)
        item = next(d for n, d in schema["$defs"].items() if n != "Evidence")
        assert "title" in item["properties"] and "title" in item["required"]


def test_schema_is_strict() -> None:
    schema = strict_json_schema(SectionSignals)
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"leadership_changes", "findings", "summary"}
    lc = schema["$defs"]["LeadershipChange"]
    assert set(lc["required"]) == set(lc["properties"]) and lc["additionalProperties"] is False


def test_tiebreaker_sees_both_claims_without_sources() -> None:
    seen = {}

    def answer(model, user):
        if model == SONNET:
            return NOTHING
        seen["user"] = user
        return rulings(("leadership:CFO", "departure", "step down from his current position"))

    _, rows, _, _ = run(DEPARTURE, FakeLLM(answer))
    user = seen["user"]
    assert "Claim A: departure; quote:" in user and "Claim B: none; quote: (none)" in user
    assert "rules" not in user.lower() and SONNET not in user
    assert (rows["leadership:CFO"].outcome, rows["leadership:CFO"].status) == (
        "departure", "MAJORITY",
    )  # fmt: skip


def test_tiebreak_answer_outside_allowed_set_is_disputed() -> None:
    llm = by(
        {SONNET: NOTHING, OPUS: rulings(("leadership:CFO", "present", "Chief Financial Officer"))}
    )
    _, rows, _, _ = run(DEPARTURE, llm)
    assert rows["leadership:CFO"].status == "DISPUTED"


def test_llm_departure_plus_appointment_is_succession() -> None:
    text = ("John Doe will retire as Chief Financial Officer on March 1. "
            "The Board appointed Jane Roe as Chief Financial Officer.")  # fmt: skip
    llm = by({SONNET: signals(leadership=[
        ("CFO", "appointment", "Jane Roe", "appointed Jane Roe as Chief Financial Officer"),
        ("CFO", "departure", "John Doe", "John Doe will retire as Chief Financial Officer"),
    ])})  # fmt: skip
    out, rows, _, _ = run(text, llm)
    assert (rows["leadership:CFO"].rule_value, rows["leadership:CFO"].extractor_value) == (
        "succession", "succession",
    )  # fmt: skip
    sig = next(s for s in out if s.rule_id == "8k_5.02_cfo_succession")
    assert sig.severity == "Warning" and "Jane Roe" in sig.description


def test_grounding_accepts_elided_quotes_in_order() -> None:
    # Claude Haiku 5.5 joins a table header and the row it read with "..." (live MAR 10-Q).
    src = ("Comparable Systemwide Properties\nThree Months Ended September 30, 2025\n"
           "RevPAR Occupancy\nWorldwide (2) $ 131.43 0.5 % 71.5 % (0.3) % pts. $ 183.71 0.9 %")  # fmt: skip
    q = "Comparable Systemwide Properties ... Worldwide (2) $ 131.43 0.5 % 71.5 %"
    assert is_grounded(q, src)
    assert is_grounded("Comparable Systemwide Properties … Worldwide (2) $ 131.43", src)
    # pieces must appear in order, each long enough, and each verbatim
    assert not is_grounded("Worldwide (2) $ 131.43 ... Comparable Systemwide Properties", src)
    assert not is_grounded("Comparable Systemwide Properties ... $ 1 ... 71.5 %", src)
    assert not is_grounded("Comparable Systemwide Properties ... Worldwide (2) $ 999.99", src)
    # a leading or trailing ellipsis is still fine
    assert is_grounded("... Worldwide (2) $ 131.43 0.5 %", src)


def test_grounding_ignores_footnote_markers_left_out() -> None:
    # live JPM earnings release: "capital3", "ratio3"; MD&A tables: "estate(a)"
    src = "n Basel III common equity Tier 1 capital3 of $304 billion, Standardized ratio3 of 13.9%"
    assert is_grounded("Basel III common equity Tier 1 capital of $304 billion, Standardized "
                       "ratio of 13.9%", src)  # fmt: skip
    assert is_grounded("Residential real estate $ 300,664", "Residential real estate(a)\n$ 300,664")
    assert not is_grounded("Standardized ratio of 13.8%", src)


def test_footnote_rule_keeps_period_labels_apart() -> None:
    """Review 2026-10-14: stripping digits glued to letters made 'q3' and 'q4' the same."""
    src = "Q4 net revenue increased 12% on higher fees"
    assert not is_grounded("Q3 net revenue increased 12% on higher fees", src)
    assert not is_grounded("FY24 net revenue increased 12%", "FY25 net revenue increased 12%")
    assert is_grounded("Q4 net revenue increased 12%", src)


def test_a_restatement_finding_outside_the_voted_topics_is_ignored() -> None:
    """The model may report a restatement (the schema allows it) where only going concern is
    voted; it is not a voted topic and must not stop the stage (CGC, NFE: KeyError)."""
    from uw_pipeline.signals.vote import llm_answers

    source = ("We have restated our previously issued financial statements for 2024. "
              "There is no substantial doubt about our ability to continue as a going concern.")  # fmt: skip
    parsed = SectionSignals.model_validate({
        "leadership_changes": [],
        "findings": [{"topic": "restatement",
                      "evidence": "We have restated our previously issued financial statements"}],
        "summary": "Restatement.",
    })  # fmt: skip
    out, dropped = llm_answers(parsed, ["going_concern"], source)
    assert out == {"going_concern": Answer("none")} and dropped == 0
