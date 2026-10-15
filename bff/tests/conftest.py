"""A small application database: a hotel group (non-FI) and a bank (FI), with every table the
API reads, in an in-memory SQLite shared by the test client's threads."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, insert
from sqlalchemy.pool import StaticPool

from p360_bff.app import create_app
from p360_bff.db import create_dev_tables
from p360_bff.settings import Settings
from uw_pipeline.schema_app import app_metadata

T = app_metadata.tables
NOW = datetime.now()
SRC = json.dumps(
    [
        {
            "title": "ACME HOTELS 10-Q filed 2026-08-03 · MD&A",
            "url": "https://www.sec.gov/x.htm",
            "date": "2026-08-03",
        }
    ]
)
CALL = json.dumps([{"title": "ACME HOTELS earnings call 2026Q2", "url": None, "date": None}])


def seed(engine: Engine) -> None:
    rows: dict[str, list[dict]] = {
        "SIC_Code": [
            {"SIC_Id": 1, "SIC_Code": "7011", "Description": "Hotels & Motels"},
            {"SIC_Id": 2, "SIC_Code": "6021", "Description": "National Commercial Banks"},
        ],
        "Company": [
            {
                "CompanyId": 1,
                "SIC_Id": 1,
                "CompanyName": "Acme Hotels",
                "NameAbbreviation": "ACH",
                "Location": "Bethesda, MD",
                "UpdatedAt": date(2026, 10, 14),
                "CIK": "0000000001",
            },
            {
                "CompanyId": 2,
                "SIC_Id": 2,
                "CompanyName": "Big Bank",
                "NameAbbreviation": "BBK",
                "Location": "New York, NY",
                "UpdatedAt": date(2026, 10, 14),
                "CIK": "0000000002",
            },
        ],
        "metric_category": [
            {"metric_category_Id": 1, "categoryName": "Financial"},
            {"metric_category_Id": 5, "categoryName": "Operational Metrics"},
            {"metric_category_Id": 7, "categoryName": "Loan Portfolio"},
        ],
        "metrics": [
            {
                "metric_id": 1,
                "metric_name": "Total Revenue",
                "metric_category_Id": 1,
                "subtitle": "USD",
                "Formula": "Taken directly from XBRL.",
            },
            {"metric_id": 2, "metric_name": "RevPAR", "metric_category_Id": 5, "subtitle": "USD"},
            {
                "metric_id": 3,
                "metric_name": "Debt / Equity",
                "metric_category_Id": 1,
                "subtitle": "x",
            },
            {"metric_id": 9, "metric_name": "Loan Portfolio Composition", "metric_category_Id": 7},
        ],
        "SIC_Preferences": [{"metric_id": m, "SIC_Id": 1} for m in (1, 2)]
        + [{"metric_id": m, "SIC_Id": 2} for m in (1, 9)],
        "KeyInsightsTypes": [
            {"KeyInsightsType_Id": i, "TypeName": n}
            for i, n in enumerate(["Warning", "Negative", "Positive", "Info"], 1)
        ],
        "RiskTypes": [
            {"RiskType_Id": 1, "RiskTypeName": "Litigation"},
            {"RiskType_Id": 2, "RiskTypeName": "Tone Shift"},
        ],
        "TrendTypes": [{"TrendTypeId": 1, "TrendName": "CET1 Ratio", "Methodology": "XBRL."}],
        "LoanTypes": [
            {"LoanTypeId": 1, "LoanTypeName": "Credit card", "ParentSegment": "Consumer"},
            {"LoanTypeId": 2, "LoanTypeName": "Loans retained", "ParentSegment": "Wholesale"},
        ],
    }
    matrix = []
    for i, (fy, fp, end) in enumerate(
        [
            (2024, "Q1", "2024-03-31"),
            (2024, "Q2", "2024-06-30"),
            (2024, "Q3", "2024-09-30"),
            (2024, "Q4", "2024-12-31"),
            (2025, "Q1", "2025-03-31"),
            (2025, "Q2", "2025-06-30"),
            (2025, "Q3", "2025-09-30"),
            (2025, "Q4", "2025-12-31"),
            (2026, "Q1", "2026-03-31"),
            (2026, "Q2", "2026-06-30"),
        ]
    ):
        matrix.append(
            {
                "CompanyId": 1,
                "metric_id": 1,
                "cik": "0000000001",
                "value": 1e9 + i,
                "unit": "USD",
                "status": "OK",
                "source_class": "REPORTED",
                "period_end": date.fromisoformat(end),
                "fiscal_year": fy,
                "fiscal_period": fp,
            }
        )
    matrix[-1] |= {"source_class": "PRELIMINARY"}
    matrix += [
        {
            "CompanyId": 1,
            "metric_id": 1,
            "cik": "1",
            "value": 4e9,
            "unit": "USD",
            "status": "OK",
            "source_class": "REPORTED",
            "period_end": date(2025, 12, 31),
            "fiscal_year": 2025,
            "fiscal_period": "FY",
        },
        {
            "CompanyId": 1,
            "metric_id": 2,
            "cik": "1",
            "value": 138.74,
            "unit": "USD",
            "status": "OK",
            "source_class": "LLM_EXTRACTED",
            "period_end": date(2026, 6, 30),
            "fiscal_year": 2026,
            "fiscal_period": "Q2",
            "notes": "Comparable systemwide",
        },
        {
            "CompanyId": 1,
            "metric_id": 3,
            "cik": "1",
            "value": None,
            "unit": "ratio",
            "status": "NM",
            "source_class": "COMPUTED",
            "period_end": date(2026, 6, 30),
            "fiscal_year": 2026,
            "fiscal_period": "Q2",
        },
        {
            "CompanyId": 2,
            "metric_id": 1,
            "cik": "2",
            "value": 4.5e10,
            "unit": "USD",
            "status": "OK",
            "source_class": "REPORTED",
            "period_end": date(2026, 6, 30),
            "fiscal_year": 2026,
            "fiscal_period": "Q2",
        },
    ]
    rows |= {
        "MatrixData": matrix,
        "CompanyMetricDetail": [
            {
                "CompanyMetricDetailId": 1,
                "CompanyId": 1,
                "metric_id": 1,
                "Methodology": "Taken directly from XBRL.\nTag: us-gaap:Revenues",
                "DataSources": SRC,
                "DataPoints": json.dumps([{"label": "FY2026 Q2", "text": "$1.0B"}]),
                "UnderwritingRelevance": "Revenue scale.",
                "UpdatedAt": NOW,
            },
            {
                "CompanyMetricDetailId": 2,
                "CompanyId": 2,
                "metric_id": 9,
                "Methodology": "Retained loans by class.",
                "DataSources": SRC,
                "DataPoints": "[]",
                "UpdatedAt": NOW,
            },
        ],
        "KeyInsights": [
            {
                "UniqueID": 10,
                "CompanyId": 1,
                "title": "Buybacks outpace earnings",
                "summary": "s",
                "KeyInsightsType_Id": 1,
                "iconType": "warning",
                "source": SRC,
                "updatedDate": date(2026, 8, 3),
                "confidence": 0.8,
                "isActive": True,
            },
            {
                "UniqueID": 11,
                "CompanyId": 1,
                "title": "Old version",
                "KeyInsightsType_Id": 2,
                "isActive": False,
            },
            {
                "UniqueID": 12,
                "CompanyId": 1,
                "title": "Demand holds",
                "KeyInsightsType_Id": 3,
                "isActive": True,
            },
        ],
        "StrategicRisk": [
            {
                "StrategicRiskId": 20,
                "CompanyId": 1,
                "Category": "risk",
                "RiskType_Id": 1,
                "Title": "Litigation accrual",
                "Summary": "s",
                "sources": SRC,
                "isActive": True,
            },
            {
                "StrategicRiskId": 21,
                "CompanyId": 1,
                "Category": "behavior",
                "RiskType_Id": 2,
                "Title": "Tone turns cautious",
                "sources": CALL,
                "isActive": True,
            },
            {
                "StrategicRiskId": 22,
                "CompanyId": 1,
                "Category": "risk",
                "Title": "Replaced",
                "isActive": False,
            },
        ],
        "FollowUpQuestions": [
            {
                "FollowUpQuestionId": 30,
                "CompanyId": 1,
                "category": "Financial",
                "title": "How does the board weigh buybacks?",
                "Priority": "high",
                "sources": SRC,
                "isActive": True,
            },
            {
                "FollowUpQuestionId": 31,
                "CompanyId": 1,
                "category": "Governance",
                "title": "Old",
                "isActive": False,
            },
        ],
        "ItemLink": [
            {
                "CompanyId": 1,
                "FromType": "KEY_INSIGHT",
                "FromId": 10,
                "ToType": "STRATEGIC_RISK",
                "ToId": 20,
                "LinkSource": "llm",
            },
            {
                "CompanyId": 1,
                "FromType": "KEY_INSIGHT",
                "FromId": 10,
                "ToType": "METRIC",
                "ToId": 1,
                "LinkSource": "llm",
            },
            {
                "CompanyId": 1,
                "FromType": "FOLLOWUP",
                "FromId": 30,
                "ToType": "KEY_INSIGHT",
                "ToId": 10,
                "LinkSource": "llm",
            },
            {
                "CompanyId": 1,
                "FromType": "FOLLOWUP",
                "FromId": 31,
                "ToType": "KEY_INSIGHT",
                "ToId": 10,
                "LinkSource": "llm",
            },
            {
                "CompanyId": 1,
                "FromType": "FOLLOWUP",
                "FromId": 30,
                "ToType": "STRATEGIC_RISK",
                "ToId": 22,
                "LinkSource": "llm",
            },
        ],
        "Activities": [
            {
                "ActivityId": 40,
                "CompanyId": 1,
                "title": "Acme raises outlook",
                "updateDate": NOW - timedelta(days=2),
                "sentiment": "Positive",
                "sources": SRC,
                "confidence": 0.6,
            },
            {
                "ActivityId": 41,
                "CompanyId": 1,
                "title": "Old news",
                "updateDate": NOW - timedelta(days=90),
            },
        ],
        "NewsRiskScore": [
            {
                "NewsId": 1,
                "runId": 1,
                "CompanyId": 1,
                "ActivityId": 40,
                "overallRiskScore": 0.1,
                "litigationEO": 0,
                "regulatory": 0,
                "financialDistress": 0,
                "governance": 0,
                "operational": 0.1,
                "rationale": "old run",
                "createdAt": NOW - timedelta(days=1),
            },
            {
                "NewsId": 1,
                "runId": 2,
                "CompanyId": 1,
                "ActivityId": 40,
                "overallRiskScore": 0.2,
                "litigationEO": 0,
                "regulatory": 0,
                "financialDistress": 0,
                "governance": 0,
                "operational": 0.2,
                "rationale": "latest run",
                "createdAt": NOW,
            },
        ],
        "TranscriptToneAnalysis": [
            {
                "OneAnalysisId": 1,
                "TranscriptsId": 50,
                "runId": 1,
                "CompanyId": 1,
                "Quarter": "2026Q2",
                "overallTone": 0.4,
                "nChunks": 10,
                "NChunksAttempted": 10,
                "modelVersion": "model-x/transcript_tone_v1",
                "createdAt": NOW,
            },
        ],
        "TranscriptRiskFlag": [
            {
                "TranscriptsId": 50,
                "runId": 1,
                "FlagIndex": 1,
                "FlagText": "Guidance is unchanged.",
                "Category": "guidance",
            }
        ],
        "Trend": [
            {"TrendTypeId": 1, "CompanyId": 2, "Year": y, "Value": v, "DataSources": SRC}
            for y, v in ((2024, 0.15), (2025, 0.152))
        ],
        "LoanPortfolio": [
            {
                "LoanTypeId": 1,
                "CompanyId": 2,
                "LoanValue": 249.9e9,
                "PeriodEnd": date(2026, 6, 30),
                "PctOfBook": 0.171,
            },
            {
                "LoanTypeId": 2,
                "CompanyId": 2,
                "LoanValue": 846.8e9,
                "PeriodEnd": date(2026, 6, 30),
                "PctOfBook": 0.578,
                "Nonaccrual": 4.19e9,
                "NonaccrualRatio": 0.0049,
            },
            {
                "LoanTypeId": 2,
                "CompanyId": 2,
                "LoanValue": 792.4e9,
                "PeriodEnd": date(2025, 12, 31),
                "PctOfBook": 0.56,
            },
        ],
    }
    with engine.begin() as conn:
        for table in app_metadata.sorted_tables:
            for row in rows.get(table.name, []):  # one by one: rows leave out different columns
                conn.execute(insert(table).values(**row))


@pytest.fixture
def engine() -> Engine:
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    create_dev_tables(eng)
    seed(eng)
    return eng


@pytest.fixture
def client(engine: Engine) -> TestClient:
    return TestClient(create_app(Settings(bff_dev_user_email="ana@example.com"), engine))
