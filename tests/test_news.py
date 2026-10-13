"""News parsing and de-duplication. The feed below is SYNTHETIC: it follows Alpha Vantage's
documented NEWS_SENTIMENT format, because the endpoint needs a premium key (decisions D-029)."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, insert, select

from tests.conftest import CONFIG_DIR
from tests.fake_llm import FakeLLM
from uw_pipeline.enrich.llm import load_llm_config
from uw_pipeline.jobs.run_news import dedup_and_store, load_news_config
from uw_pipeline.normalize.news import (
    Doc,
    near_duplicates,
    normalize_title,
    normalize_url,
    parse_feed,
)
from uw_pipeline.schema import activity, company, metadata, news_item

CFG = load_news_config(CONFIG_DIR)
NOW = datetime(2026, 10, 13, tzinfo=UTC)


def article(url, title, summary, when, source="Reuters", relevance="0.8", ticker="MAR"):
    return {
        "title": title, "url": url, "time_published": when, "authors": ["A. Writer"],
        "summary": summary, "source": source, "source_domain": "example.com",
        "overall_sentiment_score": 0.1, "overall_sentiment_label": "Neutral",
        "ticker_sentiment": [{"ticker": ticker, "relevance_score": relevance,
                              "ticker_sentiment_score": "0.2",
                              "ticker_sentiment_label": "Somewhat-Bullish"}],
    }  # fmt: skip


EARNINGS = "Marriott raised its full-year RevPAR outlook after second-quarter results beat estimates on strong international demand."
FEED = {
    "items": "6",
    "feed": [
        article("https://www.reuters.com/business/marriott-q2-2026/", "Marriott beats Q2 estimates, raises RevPAR outlook",
                EARNINGS, "20260804T113000"),
        # tier 1: same URL with tracking parameters / AMP
        article("https://reuters.com/business/marriott-q2-2026/amp?utm_source=x&utm_medium=y", "Marriott beats Q2 estimates, raises RevPAR outlook",
                EARNINGS, "20260804T113500"),
        # tier 2: syndicated copy with a source tag
        article("https://finance.yahoo.com/news/marriott-beats-q2-123.html", "Marriott beats Q2 estimates, raises RevPAR outlook - Reuters",
                EARNINGS, "20260804T120000", source="Yahoo Finance"),
        # tier 3: same event, different article
        article("https://www.cnbc.com/2026/08/04/marriott-earnings.html", "Hotel giant Marriott lifts forecast as travelers keep spending",
                "Marriott's chief executive said demand outside the US offset softer domestic leisure travel.", "20260804T150000", source="CNBC"),
        # different event
        article("https://www.reuters.com/legal/marriott-data-breach-ruling/", "Court revives Marriott data breach claims",
                "An appeals court revived consumer claims tied to the 2018 Starwood breach.", "20260806T090000"),
        # mentions MAR only in passing
        article("https://example.com/hotel-stocks", "Hotel stocks to watch this week", "A roundup of lodging names.",
                "20260805T090000", relevance="0.1"),
    ],
}  # fmt: skip


def test_normalize_url_drops_tracking_and_amp() -> None:
    strip = CFG.dedup.strip_params_prefixes
    a = normalize_url("https://www.reuters.com/business/marriott-q2-2026/", strip)
    b = normalize_url("http://reuters.com/business/marriott-q2-2026/amp?utm_source=x&id=7", strip)
    assert a == "reuters.com/business/marriott-q2-2026"
    assert b == "reuters.com/business/marriott-q2-2026?id=7"


def test_title_source_tag_removed() -> None:
    assert normalize_title("Marriott beats Q2 estimates - Reuters") == "marriott beats q2 estimates"
    assert normalize_title("Q2 beat | Benzinga") == "q2 beat"


def test_parse_feed_filters_low_relevance() -> None:
    items, dropped = parse_feed(FEED, "MAR", CFG)
    assert dropped == 1 and len(items) == 5
    assert items[0].published_at == datetime(2026, 8, 4, 11, 30)


def test_near_duplicates_respect_window() -> None:
    t0 = datetime(2026, 8, 4)
    docs = [
        Doc("a", "Marriott beats Q2 estimates, raises outlook", "", t0),
        Doc(
            "b",
            "Marriott beats Q2 estimates, raises outlook - Reuters",
            "",
            t0 + timedelta(hours=5),
        ),
        Doc("c", "Marriott beats Q2 estimates, raises outlook", "", t0 + timedelta(days=10)),
    ]
    assert near_duplicates(docs, CFG.dedup) == [("a", "b")]


@pytest.fixture
def conn():
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    with engine.begin() as c:
        c.execute(insert(company).values(
            company_id=1, cik="0001048286", ticker="MAR", name="Marriott", legal_name="MARRIOTT",
            industry_group="HOTELS", is_financial=False, is_active=True, source_url="u",
            source_hash="h", raw_path="p", last_refreshed_at=NOW))  # fmt: skip
    with engine.begin() as c:
        yield c


def grouping_llm(seen_ids=None):
    """Groups the two earnings articles by headline; also invents an id to check it's ignored."""

    def answer(model, user):
        lines = [ln for ln in user.splitlines() if " | " in ln and not ln.startswith("id |")]
        ids = {ln.split(" | ")[3]: ln.split(" | ")[0] for ln in lines}
        earnings = [i for t, i in ids.items() if "Marriott" in t and ("Q2" in t or "forecast" in t)]
        if seen_ids is not None:
            seen_ids.append(sorted(ids.values()))
        return (
            {"groups": [{"ids": [*earnings, "n999"], "event": "Q2 2026 results"}]}
            if len(earnings) > 1
            else {"groups": []}
        )

    return FakeLLM(answer)


def _llm(fake):
    return fake, load_llm_config(CONFIG_DIR)


def test_three_tiers_collapse_to_two_activities(conn) -> None:
    items, _ = parse_feed(FEED, "MAR", CFG)
    stats = dedup_and_store(conn, 1, "MARRIOTT", [(i, "raw") for i in items], CFG,
                            llm=_llm(grouping_llm()), now=NOW)  # fmt: skip
    acts = conn.execute(select(activity.c.title, activity.c.source_count)).all()
    assert sorted(acts) == [
        ("Court revives Marriott data breach claims", 1),
        ("Marriott beats Q2 estimates, raises RevPAR outlook", 3),
    ]
    methods = dict(conn.execute(select(news_item.c.source, news_item.c.dedup_method)).all())
    assert methods == {"Reuters": "FIRST", "Yahoo Finance": "NEAR_DUP", "CNBC": "LLM_EVENT"}
    assert (stats.url_duplicates, stats.near_duplicates, stats.event_duplicates) == (1, 1, 1)
    assert stats.llm_invented_ids == 1


def test_without_llm_only_tiers_one_and_two(conn) -> None:
    items, _ = parse_feed(FEED, "MAR", CFG)
    dedup_and_store(conn, 1, "MARRIOTT", [(i, "raw") for i in items], CFG, llm=None, now=NOW)
    assert conn.execute(select(func.count()).select_from(activity)).scalar_one() == 3


def test_next_run_attaches_and_never_duplicates(conn) -> None:
    items, _ = parse_feed(FEED, "MAR", CFG)
    llm = _llm(grouping_llm())
    dedup_and_store(conn, 1, "MARRIOTT", [(i, "raw") for i in items], CFG, llm=llm, now=NOW)
    later = {"feed": [
        *FEED["feed"][:3],  # overlap window re-fetches known articles
        article("https://www.marketwatch.com/story/marriott-q2", "Marriott beats Q2 estimates, raises RevPAR outlook",
                EARNINGS, "20260805T080000", source="MarketWatch"),
    ]}  # fmt: skip
    items2, _ = parse_feed(later, "MAR", CFG)
    stats = dedup_and_store(conn, 1, "MARRIOTT", [(i, "raw") for i in items2], CFG, llm=llm,
                            now=NOW + timedelta(days=1))  # fmt: skip
    assert stats.url_duplicates == 3 and stats.attached_to_existing == 1
    assert stats.new_activities == 0
    count = conn.execute(
        select(activity.c.source_count).where(activity.c.title.like("Marriott beats%"))
    ).scalar_one()
    assert count == 4
    # 4 stored on the first run (the tracking-URL copy was dropped) + MarketWatch.
    assert conn.execute(select(func.count()).select_from(news_item)).scalar_one() == 5


MENTION = load_news_config(CONFIG_DIR).mention
JPM = ["JPMorgan", "JP Morgan", "J.P. Morgan", "Chase", "Jamie Dimon"]


@pytest.mark.parametrize(
    ("title", "summary", "aliases", "keep"),
    [
        # live JPM feed, 2026-10: the company as analyst or fund holder is not news about it
        ("JPMorgan Downgrades VICI Properties to Neutral From Overweight, Adjusts PT to $30", "", JPM, False),
        ("Edison International (NYSE:EIX) Stock Price Target Lowered at JPMorgan Chase & Co.", "", JPM, False),
        ("US BROKER MOVES: CBOE upgraded at Morgan Stanley; DD downgraded at JPM", "", ["JPM"], False),
        ("JPMorgan initiates Marriott Vacations stock with overweight rating", "", JPM, False),
        ("REG - Trainline PLC JPMorgan Chase & Co - Holding(s) in Company", "", JPM, False),
        ("Bank of America Corp DE Makes New Investment in TPG RE Finance Trust, Inc. $TRTX", "", ["Bank of America"], False),
        ("2,479,813 Shares in Citigroup Inc. $C Bought by Nykredit A S", "", JPM, False),
        ("Crane Buys into Municipal Water with Trillium's US Pump Brands", "", JPM, False),
        ("Investors chase yield as rates fall", "", JPM, False),  # the verb, not the bank
        # about the company
        ("JPMorgan Chase Falls 3% Despite New $20B QIA Partnership", "", JPM, True),
        ("Bank stocks slide", "Jamie Dimon warned of cockroaches in private credit.", JPM, True),
        ("Layoffs at Ford's Louisville plant", "", ["Ford"], True),
        ("FDA approves new drug from Pfizer", "", ["Pfizer"], True),
        ("Ford recalls 500,000 SUVs", "", ["Ford"], True),
        ("Affordable housing push", "", ["Ford"], False),  # not a word match
    ],
)  # fmt: skip
def test_news_must_be_about_the_company(title, summary, aliases, keep) -> None:
    from uw_pipeline.normalize.news import about_company

    assert about_company(title, summary, aliases, MENTION) is keep


def test_llm_relevance_removes_non_subject_articles_once(conn) -> None:
    from uw_pipeline.jobs.run_news import judge_and_prune

    items, _ = parse_feed(FEED, "MAR", CFG)
    dedup_and_store(conn, 1, "MARRIOTT", [(i, "raw") for i in items], CFG, llm=None, now=NOW)

    def answer(model, user):
        rows = [ln.split(" | ") for ln in user.splitlines() if ln.startswith("N")]
        # the breach ruling is judged PASSING (to exercise removal); one id is left out
        return {"articles": [
            {"id": r[0], "verdict": "PASSING" if "breach" in r[1] else "SUBJECT"}
            for r in rows if "CNBC" not in r[2] and "forecast" not in r[1]
        ]}  # fmt: skip

    fake = FakeLLM(answer)
    judged, removed = judge_and_prune(conn, 1, "MARRIOTT", CFG, _llm(fake), NOW)
    assert (judged, removed) == (4, 1)
    titles = sorted(conn.execute(select(activity.c.title)).scalars())
    assert titles == ["Hotel giant Marriott lifts forecast as travelers keep spending",
                      "Marriott beats Q2 estimates, raises RevPAR outlook"]  # fmt: skip
    verdicts = dict(conn.execute(select(news_item.c.title, news_item.c.relevance_verdict)).all())
    assert (
        verdicts["Hotel giant Marriott lifts forecast as travelers keep spending"] == "UNCLASSIFIED"
    )
    calls = len(fake.calls)
    assert judge_and_prune(conn, 1, "MARRIOTT", CFG, _llm(fake), NOW) == (0, 0)
    assert len(fake.calls) == calls
