"""BFF API (D-048) over a seeded application database: page modules, active versions only,
related items from either end, user state kept apart per user."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from p360_bff.app import create_app
from p360_bff.settings import REPO, Settings

API = "/api/v1"
OTHER = {"X-User-Email": "ben@example.com"}


def get(client: TestClient, path: str, **kw: object) -> object:
    r = client.get(API + path, **kw)  # type: ignore[arg-type]
    assert r.status_code == 200, r.text
    return r.json()


def test_company_list_counts_and_search(client: TestClient) -> None:
    rows = get(client, "/companies")
    assert [(r["ticker"], r["riskFlags"], r["followUps"]) for r in rows] == [
        ("ACH", 1, 1),
        ("BBK", 0, 0),
    ]  # active Warning/Negative key insights; active questions
    assert rows[0]["sic"] == {"sicId": 1, "code": "7011", "description": "Hotels & Motels"}
    assert [r["ticker"] for r in get(client, "/companies", params={"search": "bbk"})] == ["BBK"]


def test_modules_follow_the_data(client: TestClient) -> None:
    hotel = get(client, "/companies/1")["modules"]
    bank = get(client, "/companies/2")["modules"]
    assert hotel["operational"] and not hotel["performanceTrends"] and not hotel["loanPortfolio"]
    assert bank["performanceTrends"] and bank["loanPortfolio"] and not bank["operational"]
    assert client.get(API + "/companies/99").status_code == 404


def test_metrics_quarterly_and_annual(client: TestClient) -> None:
    q = get(client, "/companies/1/metrics")
    assert q["view"] == "quarterly" and not q["customized"]
    cats = {c["category"]: c["metrics"] for c in q["categories"]}
    assert set(cats) == {"Financial", "Operational Metrics"}  # SIC default: revenue, RevPAR
    rev = cats["Financial"][0]
    assert len(rev["series"]) == 8 and rev["series"][0]["period"] == "FY2024 Q3"
    assert rev["latest"]["period"] == "FY2026 Q2"
    assert rev["latest"]["sourceLabel"] == "Preliminary (earnings release)"
    assert cats["Operational Metrics"][0]["latest"]["sourceLabel"] == "Extracted from filing text"
    a = get(client, "/companies/1/metrics", params={"view": "annual"})
    assert [p["period"] for p in a["categories"][0]["metrics"][0]["series"]] == ["FY2025"]


def test_user_metric_selection_applies_to_the_industry(client: TestClient) -> None:
    r = client.put(API + "/me/preferences/metrics/1", json={"ids": [3]})
    assert r.json()["metricSelections"] == {"1": [3]}
    q = get(client, "/companies/1/metrics")
    assert q["customized"]
    point = q["categories"][0]["metrics"][0]["latest"]
    assert (point["status"], point["value"]) == ("NM", None)  # D/E with negative equity: N/M
    assert not get(client, "/companies/1/metrics", headers=OTHER)["customized"]
    client.put(API + "/me/preferences/metrics/1", json={"ids": []})  # back to the default
    assert not get(client, "/companies/1/metrics")["customized"]
    avail = get(client, "/me/preferences/metrics/1/available")
    assert [m["metricId"] for m in avail][:2] == [1, 2] and 9 not in [m["metricId"] for m in avail]


def test_metric_modal(client: TestClient) -> None:
    d = get(client, "/companies/1/metrics/1")
    assert d["methodology"] == ["Taken directly from XBRL.", "Tag: us-gaap:Revenues"]
    assert d["dataSources"][0]["url"] == "https://www.sec.gov/x.htm"
    assert [k["id"] for k in d["related"]["keyInsights"]] == [10]
    assert client.get(API + "/companies/2/metrics/2").status_code == 404  # no data


def test_key_insights_only_active_with_related_items(client: TestClient) -> None:
    items = get(client, "/companies/1/key-insights")
    assert [i["id"] for i in items] == [10, 12]
    assert items[0]["sources"][0]["title"].startswith("ACME HOTELS 10-Q")
    rel = get(client, "/companies/1/key-insights/10")["related"]
    assert [r["id"] for r in rel["risks"]] == [20]
    assert [m["metricId"] for m in rel["metrics"]] == [1]
    assert [f["id"] for f in rel["followUps"]] == [30]  # 31 is a replaced version
    assert client.get(API + "/companies/1/key-insights/11").status_code == 404


def test_risks_grouped_and_detail(client: TestClient) -> None:
    groups = get(client, "/companies/1/risks")
    assert [(g["categoryLabel"], [r["id"] for r in g["risks"]]) for g in groups] == [
        ("Risk & Legal", [20]),
        ("Behavioral Insights", [21]),
    ]
    assert groups[1]["risks"][0]["sources"] == [
        {"title": "ACME HOTELS earnings call 2026Q2", "url": None, "date": None}
    ]
    rel = get(client, "/companies/1/risks/20")["related"]
    assert [k["id"] for k in rel["keyInsights"]] == [10]  # linked from the key insight's side


def test_follow_up_flags_are_per_user(client: TestClient) -> None:
    assert get(client, "/companies/1/follow-ups")[0]["isFlagged"] is False
    r = client.put(API + "/companies/1/follow-ups/30/flag", json={"flagged": True})
    assert r.status_code == 200 and r.json()["isFlagged"] is True
    assert get(client, "/companies/1/follow-ups/30")["isFlagged"] is True
    assert get(client, "/companies/1/follow-ups/30", headers=OTHER)["isFlagged"] is False
    rel = get(client, "/companies/1/follow-ups/30")["related"]
    assert [k["id"] for k in rel["keyInsights"]] == [10] and rel["risks"] == []  # 22 replaced
    assert (
        client.put(API + "/companies/1/follow-ups/31/flag", json={"flagged": True}).status_code
        == 404
    )


def test_activities_with_latest_risk_score(client: TestClient) -> None:
    items = get(client, "/companies/1/activities")
    assert [a["id"] for a in items] == [40]  # 90 days old: outside the default 30
    assert items[0]["riskScore"]["rationale"] == "latest run"
    assert len(get(client, "/companies/1/activities", params={"days": 120})) == 2


def test_fi_modules(client: TestClient) -> None:
    trends = get(client, "/companies/2/trends")
    assert [(t["name"], [p["year"] for p in t["points"]]) for t in trends] == [
        ("CET1 Ratio", [2024, 2025])
    ]
    lp = get(client, "/companies/2/loan-portfolio")
    assert (lp["currentPeriod"], lp["priorPeriod"]) == ("2026-06-30", "2025-12-31")
    assert {s["segment"] for s in lp["segments"]} == {"Credit card", "Loans retained"}
    assert lp["methodology"] == ["Retained loans by class."]
    assert get(client, "/companies/1/trends") == []
    assert get(client, "/companies/1/loan-portfolio")["segments"] == []


def test_transcripts_hide_the_model(client: TestClient) -> None:
    items = get(client, "/companies/1/transcripts")
    assert items[0]["quarter"] == "2026Q2" and items[0]["flags"][0]["category"] == "guidance"
    assert "model" not in str(items).lower()


def test_favorites_pinned_and_recent(client: TestClient) -> None:
    assert client.put(API + "/me/favorites/2").status_code == 204
    assert client.put(API + "/me/pinned/1").status_code == 204
    assert [c["ticker"] for c in get(client, "/me/favorites")] == ["BBK"]
    assert [c["isPinned"] for c in get(client, "/companies")] == [True, False]
    assert get(client, "/me/favorites", headers=OTHER) == []
    assert client.delete(API + "/me/favorites/2").status_code == 204
    assert get(client, "/me/favorites") == []
    assert client.put(API + "/me/favorites/99").status_code == 404
    get(client, "/companies/1")
    get(client, "/companies/2")
    get(client, "/companies/1")
    assert [r["company"]["ticker"] for r in get(client, "/me/recent")] == ["ACH", "BBK"]
    assert get(client, "/me/recent", headers=OTHER) == []


def test_preferences_and_me(client: TestClient) -> None:
    assert get(client, "/me")["email"] == "ana@example.com"
    r = client.put(API + "/me/preferences/industries", json={"ids": [2, 1, 2]})
    assert r.json()["followedSicIds"] == [1, 2]
    assert client.get(API + "/me", headers={"X-User-Email": "nobody"}).status_code == 400


def test_health(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok", "database": "ok"}


@pytest.mark.skipif(not (REPO / "data" / "app.db").exists(), reason="no local data/app.db")
def test_every_endpoint_on_the_published_app_database(tmp_path: Path) -> None:
    """Smoke test on a copy of the database the pipeline's publish stage wrote."""
    db = tmp_path / "app.db"
    shutil.copy(REPO / "data" / "app.db", db)
    client = TestClient(create_app(Settings(bff_database_url=f"sqlite:///{db}")))
    for c in get(client, "/companies"):
        cid = c["companyId"]
        for path in (
            "",
            "/metrics",
            "/metrics?view=annual",
            "/key-insights",
            "/risks",
            "/follow-ups",
            "/activities",
            "/trends",
            "/loan-portfolio",
            "/transcripts",
        ):
            get(client, f"/companies/{cid}{path}")
        for k in get(client, f"/companies/{cid}/key-insights")[:2]:
            get(client, f"/companies/{cid}/key-insights/{k['id']}")
