from uw_pipeline.compute.registry import TagRef
from uw_pipeline.normalize.xbrl_facts import facts_from_companyfacts, facts_from_instance

INSTANCE = b"""<?xml version="1.0" encoding="utf-8"?>
<xbrl xmlns="http://www.xbrl.org/2003/instance"
      xmlns:xbrldi="http://xbrl.org/2006/xbrldi"
      xmlns:iso4217="http://www.xbrl.org/2003/iso4217"
      xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
      xmlns:us-gaap="http://fasb.org/us-gaap/2025"
      xmlns:mar="http://www.marriott.com/20260630">
  <context id="q"><entity><identifier scheme="x">1</identifier></entity>
    <period><startDate>2026-04-01</startDate><endDate>2026-06-30</endDate></period></context>
  <context id="q_fee"><entity><identifier scheme="x">1</identifier>
      <segment><xbrldi:explicitMember dimension="srt:ProductOrServiceAxis">mar:FeeServiceMember</xbrldi:explicitMember></segment>
    </entity>
    <period><startDate>2026-04-01</startDate><endDate>2026-06-30</endDate></period></context>
  <context id="i"><entity><identifier scheme="x">1</identifier></entity>
    <period><instant>2026-06-30</instant></period></context>
  <unit id="usd"><measure>iso4217:USD</measure></unit>
  <us-gaap:Revenues contextRef="q" unitRef="usd" decimals="-6">7071000000</us-gaap:Revenues>
  <us-gaap:Revenues contextRef="q_fee" unitRef="usd" decimals="-6">1547000000</us-gaap:Revenues>
  <us-gaap:Revenues contextRef="q_fee" unitRef="usd" decimals="-6">1547000000</us-gaap:Revenues>
  <mar:DepreciationAmortizationAndOther contextRef="q" unitRef="usd">222000000</mar:DepreciationAmortizationAndOther>
  <us-gaap:Cash contextRef="i" unitRef="usd" xsi:nil="true"/>
</xbrl>
"""


def _parse(refs):
    return facts_from_instance(INSTANCE, refs, accn="acc-1", form="10-Q", filed="2026-08-03")


def test_instance_dimension_match_is_exact() -> None:
    fee = TagRef(tag="us-gaap:Revenues", dims={"srt:ProductOrServiceAxis": "mar:FeeServiceMember"})
    df = _parse([fee])
    assert len(df) == 1  # duplicate fact collapsed; undimensioned Revenues not matched
    row = df.iloc[0]
    assert (row["val"], row["dims"], row["unit"]) == (
        1547000000.0,
        "srt:ProductOrServiceAxis=mar:FeeServiceMember",
        "USD",
    )
    assert (row["start"], row["end"], row["accn"], row["source"]) == (
        "2026-04-01",
        "2026-06-30",
        "acc-1",
        "instance",
    )


def test_instance_extension_tag_and_nil() -> None:
    df = _parse([TagRef(tag="mar:DepreciationAmortizationAndOther"), TagRef(tag="us-gaap:Cash")])
    assert df[["taxonomy", "tag", "val", "dims"]].values.tolist() == [
        ["mar", "DepreciationAmortizationAndOther", 222000000.0, ""]
    ]


def test_companyfacts_keeps_periodic_forms_only() -> None:
    payload = {"facts": {"us-gaap": {"Revenues": {"units": {"USD": [
        {"start": "2025-01-01", "end": "2025-12-31", "val": 1, "accn": "a", "fy": 2025,
         "fp": "FY", "form": "10-K", "filed": "2026-02-01"},
        {"start": "2025-01-01", "end": "2025-12-31", "val": 1, "accn": "b", "fy": 2025,
         "fp": "FY", "form": "8-K", "filed": "2026-02-01"},
    ]}}, "Other": {"units": {"USD": []}}}}}  # fmt: skip
    df = facts_from_companyfacts(payload, [TagRef(tag="us-gaap:Revenues")])
    assert df["accn"].tolist() == ["a"]
