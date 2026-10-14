"""Phase 13 (decisions D-042): capital ratios from XBRL, pay versus performance from the proxy
statement, identities between metrics. Fixtures are excerpts of the filings themselves."""

from pathlib import Path

import pandas as pd

from tests.conftest import CONFIG_DIR
from uw_pipeline.compute.metrics import concept_values
from uw_pipeline.compute.registry import TagRef, load_registry
from uw_pipeline.config import load_industry_config
from uw_pipeline.jobs.run_text_metrics import Stats, _identity_gate
from uw_pipeline.normalize.periods import learn_calendar, standardize
from uw_pipeline.normalize.xbrl_facts import facts_from_instance, pay_versus_performance
from uw_pipeline.quality.dq import load_dq_config
from uw_pipeline.signals.detect import pay_signals
from uw_pipeline.signals.rules import load_signal_rules

SEC = Path(__file__).parent / "fixtures" / "sec"
IDENT = load_dq_config(CONFIG_DIR).metric_identities


def _jpm_registry():
    reg, ind = load_registry(CONFIG_DIR), load_industry_config(CONFIG_DIR)
    return reg.for_company("JPM", ind.groups["BANKS"])


def test_capital_ratios_come_from_the_10k_not_the_regulatory_floors() -> None:
    """The 10-K tags the actual ratios and the minimums with the same unit, period and
    dimensions; only the actual firm-wide Standardized ratios may be selected."""
    creg = _jpm_registry()
    xml = (SEC / "jpm-20251231_capital_excerpt.xml").read_bytes()
    refs = {r for c in ("cet1_ratio", "tier1_ratio", "total_capital_ratio")
            for r in creg.concepts[c]}  # fmt: skip
    facts = facts_from_instance(xml, refs, accn="0001628280-26-008131", form="10-K",
                                filed="2026-02-13")  # fmt: skip
    assert set(facts["unit"]) == {"pure"}
    periods = standardize(facts, learn_calendar(facts, "1231"))
    values = concept_values(periods, creg)
    # Golden values from JPMorgan's FY2025 10-K (capital table, Standardized, firm).
    assert values["cet1_ratio"][("FY", 2025, 0)].value == 0.146
    assert values["tier1_ratio"][("FY", 2025, 0)].value == 0.155
    assert values["total_capital_ratio"][("FY", 2025, 0)].value == 0.174
    # The excerpt also holds the minimums (e.g. CET1 11.4% required): none was picked.
    all_tags = facts_from_instance(
        xml, [TagRef(tag="us-gaap:BankingRegulationCommonEquityTierOneRiskBasedCapitalRatio"
                         "CapitalAdequacyMinimum")],
        accn="x", form="10-K", filed="x",
    )  # fmt: skip
    assert all_tags.empty  # needs its own dims: never matches the ratio candidates


def test_pay_versus_performance_plain_and_per_person() -> None:
    refs = [TagRef(tag=t) for t in ("ecd:PeoTotalCompAmt", "ecd:TotalShareholderRtnAmt")]

    def pvp(name: str) -> dict[tuple[str, str], float]:
        df = pay_versus_performance((SEC / name).read_bytes(), refs, accn="a", form="DEF 14A",
                                    filed="2026-04-06")  # fmt: skip
        return {(r.tag, r.end): r.val for r in df.itertuples()}

    jpm = pvp("jpm-20260402_def14a_pvp_excerpt.xml")  # amounts without dimensions
    assert jpm[("PeoTotalCompAmt", "2025-12-31")] == 40632724
    assert jpm[("TotalShareholderRtnAmt", "2025-12-31")] == 289.18
    mar = pvp("mar-2026_def14a_pvp_excerpt.xml")  # PEO amounts tagged per person
    assert mar[("PeoTotalCompAmt", "2025-12-31")] == 22970926


def _two_ceos(smith: int, jones: int) -> bytes:
    return b"""<xbrl xmlns="http://www.xbrl.org/2003/instance" xmlns:ecd="http://xbrl.sec.gov/ecd/2025"
      xmlns:xbrldi="http://xbrl.org/2006/xbrldi" xmlns:c="http://example.com/c">
      <context id="a"><entity><identifier scheme="x">1</identifier><segment>
        <xbrldi:explicitMember dimension="ecd:IndividualAxis">c:SmithMember</xbrldi:explicitMember>
        </segment></entity><period><startDate>2025-01-01</startDate><endDate>2025-12-31</endDate></period></context>
      <context id="b"><entity><identifier scheme="x">1</identifier><segment>
        <xbrldi:explicitMember dimension="ecd:IndividualAxis">c:JonesMember</xbrldi:explicitMember>
        </segment></entity><period><startDate>2025-01-01</startDate><endDate>2025-12-31</endDate></period></context>
      <unit id="usd"><measure>iso4217:USD</measure></unit>
      <ecd:PeoTotalCompAmt contextRef="a" unitRef="usd" decimals="0">%d</ecd:PeoTotalCompAmt>
      <ecd:PeoTotalCompAmt contextRef="b" unitRef="usd" decimals="0">%d</ecd:PeoTotalCompAmt>
    </xbrl>""" % (smith, jones)


def test_two_ceos_in_one_year_are_not_added_up() -> None:
    ref = [TagRef(tag="ecd:PeoTotalCompAmt")]
    xml = _two_ceos(10, 20)
    df = pay_versus_performance(xml, ref, accn="a", form="DEF 14A", filed="x")
    assert df.empty
    # A former CEO listed with 0 that year (Southwest's layout) is not a second CEO.
    df = pay_versus_performance(_two_ceos(0, 20), ref, accn="a", form="DEF 14A", filed="x")
    assert list(df["val"]) == [20]


def _row(code: str, value: float, key=("Q", 2026, 2)) -> dict:
    return {"metric_code": code, "period_type": key[0], "fiscal_year": key[1],
            "fiscal_quarter": key[2], "value": value}  # fmt: skip


def test_text_values_breaking_an_identity_are_dropped() -> None:
    # A growth rate read as RevPAR (the SEC_metrics round-1 incident: "RevPAR increased 2.0%").
    bad = [_row("revpar", 2.0), _row("adr", 193.66), _row("occupancy", 0.716)]
    stats = Stats()
    assert _identity_gate(bad, {}, IDENT, stats, "MAR") == []
    assert (stats.identity_checked, stats.identity_dropped) == (1, 1)
    good = [_row("revpar", 138.74), _row("adr", 193.66), _row("occupancy", 0.716)]
    assert len(_identity_gate(good, {}, IDENT, Stats(), "MAR")) == 3
    # Mixed with XBRL: a text CET1 above the XBRL Tier 1 ratio is dropped, XBRL is untouched.
    xbrl = {("tier1_ratio", "Q", 2026, 2): 0.150, ("total_capital_ratio", "Q", 2026, 2): 0.17}
    assert _identity_gate([_row("cet1_ratio", 0.16)], xbrl, IDENT, Stats(), "JPM") == []
    assert _identity_gate([_row("cet1_ratio", 0.14)], xbrl, IDENT, Stats(), "JPM") != []


def test_pay_signal_when_pay_rises_and_tsr_falls() -> None:
    rules = load_signal_rules(CONFIG_DIR)

    def fy(code: str, year: int, value: float) -> dict:
        return {"metric_code": code, "period_type": "FY", "fiscal_year": year,
                "fiscal_quarter": 0, "value": value, "period_end": f"{year}-12-31",
                "source_accn": "0000000001-26-000001"}  # fmt: skip

    rows = [fy("ceo_total_comp", 2024, 20e6), fy("ceo_total_comp", 2025, 26e6),
            fy("tsr_value_100", 2024, 150.0), fy("tsr_value_100", 2025, 120.0)]  # fmt: skip
    (s,) = pay_signals(rows, rules)
    assert s.rule_id == "pay_vs_performance" and "30%" in s.description
    rows[-1] = fy("tsr_value_100", 2025, 160.0)  # shareholders gained: no signal
    assert pay_signals(rows, rules) == []
    rows[1] = fy("ceo_total_comp", 2025, 21e6)  # +5% only
    assert pay_signals(rows, rules) == []


def test_dq_reports_stored_values_breaking_an_identity() -> None:
    from uw_pipeline.quality.dq import metric_identities

    values = [{**_row("cet1_ratio", 0.16), "metric_code": "cet1_ratio"},
              _row("tier1_ratio", 0.15), _row("total_capital_ratio", 0.17)]  # fmt: skip

    class _Conn:  # no textmetrics drops recorded
        def execute(self, *_):
            return type("R", (), {"scalar": lambda self: None})()

    found = metric_identities(_Conn(), 1, values, IDENT, pd.Timestamp("2026-10-14"), 30)  # type: ignore[arg-type]
    assert [f.rule_id for f in found] == ["metric_identity"]
    assert "cet1_ratio 0.16 > tier1_ratio 0.15" in found[0].message
