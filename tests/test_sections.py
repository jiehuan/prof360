from uw_pipeline.normalize.sections import (
    apply_rules,
    audit_report,
    auditor_since,
    chunk,
    split_8k_items,
    split_items,
)

BODY = "x " * 400  # filler so real sections are longer than TOC entries

TEN_K = "\n".join(
    [
        "Table of Contents",
        "Item 1A. Risk Factors 10",
        "Item 3. Legal Proceedings 30",
        "Item 7. Management's Discussion and Analysis 42",
        "Item 7A. Quantitative and Qualitative Disclosures 60",
        "PART I",
        "ITEM 1A. RISK FACTORS",
        BODY,
        "Item 1A. Risk Factors (Continued)",  # page header repeated on every page
        BODY,
        "ITEM 3. LEGAL PROCEEDINGS",
        "Refer to Note 30.",
        "ITEM 7. MANAGEMENT'S DISCUSSION AND ANALYSIS",
        BODY,
        "see Item 1A for risks.",
        "Item 7. Management's Discussion and Analysis (Continued)",
        BODY,
        "ITEM 7A. QUANTITATIVE AND QUALITATIVE DISCLOSURES",
        "Market risk.",
        "Report of Independent Registered Public Accounting Firm",
        "Opinion on the Financial Statements",
        "We have served as the Company\u2019s auditor since at least 1942.",
        "/s/ KPMG LLP",
        "New York, New York",
        "February 26, 2026",
        "Other text.",
    ]
)


def test_10k_items_skip_toc_and_merge_page_headers() -> None:
    secs = {s.code: s for s in split_items(TEN_K, "10-K")}
    assert set(secs) == {"10K-1A", "10K-3", "10K-7", "10K-7A"}
    assert secs["10K-1A"].text.startswith("ITEM 1A. RISK FACTORS")
    assert "(Continued)" in secs["10K-1A"].text  # whole section, not one page
    assert secs["10K-3"].text == "ITEM 3. LEGAL PROCEEDINGS\nRefer to Note 30."
    assert secs["10K-7"].text.count(BODY.strip()) == 2
    assert TEN_K[secs["10K-3"].start :].startswith("ITEM 3. LEGAL")


def test_audit_report_and_tenure() -> None:
    audit = audit_report(TEN_K)
    assert audit is not None and audit.code == "10K-AUDIT"
    assert audit.text.endswith("February 26, 2026")
    assert auditor_since(audit.text) == 1942
    assert auditor_since("We have served as the Corporation's auditors since 1965.") == 1965


def test_10q_items_are_part_qualified() -> None:
    text = "\n".join(
        ["PART I", "Item 2. MD&A", BODY, "PART II", "Item 1. Legal Proceedings", "None.",
         "Item 1A. Risk Factors", "No changes."]
    )  # fmt: skip
    assert [s.code for s in split_items(text, "10-Q")] == ["10Q-I-2", "10Q-II-1", "10Q-II-1A"]


def test_heading_rules_override_item_split() -> None:
    text = "\n".join(
        ["Item 7. Management's Discussion", "See pages 50-160.", "Item 8. Statements",
         "Management's discussion and analysis", BODY,
         "Management's report on internal control over financial reporting", "..."]
    )  # fmt: skip
    rules = {
        "10K-7": (
            "^Management.s discussion and analysis$",
            "^Management.s report on internal control",
        )
    }
    secs = {s.code: s for s in apply_rules(text, split_items(text, "10-K"), rules)}
    assert secs["10K-7"].text.startswith("Management's discussion and analysis\nx x")


def test_8k_items_stop_at_exhibits() -> None:
    text = "\n".join(
        ["Item 5.02 Departure of Directors", "Jane Doe resigned.", "Item 7.01 Regulation FD",
         "Press release.", "Item 9.01 Financial Statements and Exhibits", "99.1 Release",
         "SIGNATURES"]
    )  # fmt: skip
    secs = {s.code: s.text for s in split_8k_items(text)}
    assert secs == {
        "8K-5.02": "Item 5.02 Departure of Directors\nJane Doe resigned.",
        "8K-7.01": "Item 7.01 Regulation FD\nPress release.",
    }


def test_chunks_cover_text_on_line_boundaries() -> None:
    text = "\n".join(f"paragraph {i} " + "y" * 50 for i in range(200))
    chunks = chunk(text, max_chars=1000)
    assert all(len(c.text) <= 1000 for c in chunks)
    assert "\n".join(c.text for c in chunks) == text
    assert all(text[c.start : c.end] == c.text for c in chunks)
