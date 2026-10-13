from uw_pipeline.extract.filings import instance_name


def test_instance_name_inline_xbrl() -> None:
    names = ["mar-20260630.htm", "mar-20260630_htm.xml", "mar-20260630_cal.xml", "R1.htm"]
    assert instance_name(names, "mar-20260630.htm") == "mar-20260630_htm.xml"


def test_instance_name_legacy_xbrl() -> None:
    names = ["f-20180630.xml", "f-20180630_lab.xml", "f-20180630_pre.xml", "FilingSummary.xml"]
    assert instance_name(names, "f0630201810-q.htm") == "f-20180630.xml"


def test_instance_name_missing() -> None:
    assert instance_name(["a.htm", "FilingSummary.xml"], "a.htm") is None
