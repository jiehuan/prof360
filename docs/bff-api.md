# Professional 360 BFF API

The BFF (`bff/`, decisions D-048) serves the company pages from the application database that the
pipeline publishes. All endpoints are under `/api/v1` and return JSON with camelCase field names.
The full machine-readable spec is `docs/bff-openapi.json`; a running BFF also serves it at
`/openapi.json`, with interactive docs at `/docs`.

## Conventions

| Topic | Rule |
|---|---|
| User | Development mode: the `X-User-Email` header names the user (default `dev.user@example.com`). Corporate SSO replaces this later; endpoints do not change. |
| Versions | Key insights, risks and follow-up questions are versioned by the pipeline: only current versions are returned. An id from an older version answers 404 ("not found (or replaced)"); reload the list. |
| Sources | Every `sources` / `dataSources` item is `{title, url, date}`. Earnings-call transcripts have no public link: `url` and `date` are `null`, show the title only. |
| Periods | Fiscal periods follow the company's own fiscal year: `period` is `"FY2026 Q2"` or `"FY2025"`. |
| Units | `USD` is dollars (not millions); `fraction` is a share (0.049 = 4.9%); `ratio` is a multiple (0.53x). |
| N/M | `status: "NM"` with `value: null`: not meaningful (e.g. debt/equity with negative equity); show "N/M". |
| Source labels | `sourceLabel` on metric points: `"Preliminary (earnings release)"` must be shown (replaced once the 10-Q is filed); `"Extracted from filing text"`, `"Computed"`, `"Derived"` can be tooltips; `null` for values reported in XBRL. |
| Modules | `GET /companies/{id}` returns `modules`: hide every page module whose flag is `false` (e.g. no Performance Trends or Loan Portfolio for non-banks). |

## Endpoints

| Method and path | Page element | Notes |
|---|---|---|
| `GET /companies?search=` | Company list, dashboard cards | `riskFlags` = current key insights of type Warning or Negative; `followUps` = current questions |
| `GET /companies/{id}` | Company header | Also records the view for Recently Viewed |
| `GET /companies/{id}/metrics?view=quarterly\|annual` | Financial, Liquidity, Leverage, Cash Flow, Operational, Performance Trends cards | quarterly: last 8 quarters (QoQ and YoY views); annual: last 5 fiscal years (5Ys). Uses the user's metric choice for the industry, else the industry default |
| `GET /companies/{id}/metrics/{metricId}` | Metric modal | Methodology (lines), data sources, data points, UW relevance, related items |
| `GET /companies/{id}/key-insights` | Key Insights Summary | |
| `GET /companies/{id}/key-insights/{insightId}` | Key insight modal | `related`: risks, metrics, follow-up questions |
| `GET /companies/{id}/risks` | Strategic & Risk Insights | Groups in page order: Governance & Leadership, Risk & Legal, Event Signals, Behavioral Insights |
| `GET /companies/{id}/risks/{riskId}` | Risk modal | `related`: key insights, metrics, follow-up questions |
| `GET /companies/{id}/follow-ups` | Suggested Follow-Up Questions | `isFlagged` for the current user |
| `GET /companies/{id}/follow-ups/{questionId}` | Question modal | `related`: key insights, risks, metrics |
| `PUT /companies/{id}/follow-ups/{questionId}/flag` | Flag button | Body `{"flagged": true}`; returns the question |
| `GET /companies/{id}/activities?days=30` | Recent Activities | Newest first, with the latest news risk score |
| `GET /companies/{id}/trends` | Performance Trends chart (banks) | Annual points per trend; empty for other companies |
| `GET /companies/{id}/loan-portfolio` | Loan Portfolio Composition and modal (banks) | Current and prior period-end, segments, methodology, sources |
| `GET /companies/{id}/transcripts` | Earnings call tone | Latest quarter first, with risk flags |
| `GET /me` | User menu | |
| `GET /me/recent?limit=10` | Recently Viewed | |
| `GET /me/favorites`, `PUT` / `DELETE /me/favorites/{companyId}` | Favorites | 204 on change |
| `GET /me/pinned`, `PUT` / `DELETE /me/pinned/{companyId}` | Pinned companies | 204 on change |
| `GET /me/preferences` | Save Preferences | Followed industries and metric choices per industry |
| `PUT /me/preferences/industries` | Followed industries | Body `{"ids": [sicId, ...]}` replaces the list |
| `PUT /me/preferences/metrics/{sicId}` | Metric choice for an industry | Body `{"ids": [metricId, ...]}`; `[]` restores the default |
| `GET /me/preferences/metrics/{sicId}/available` | Metric picker | All metrics, the industry default first |
| `GET /health` | Probes | `{"status": "ok", "database": "ok"}` |

## Example: metric card

```json
{
  "metricId": 1, "name": "Total Revenue", "subtitle": "USD", "unit": "USD",
  "latest": {"period": "FY2026 Q2", "fiscalYear": 2026, "fiscalPeriod": "Q2",
             "periodEnd": "2026-06-30", "value": 7071000000.0, "status": "OK",
             "sourceClass": "REPORTED", "sourceLabel": null, "notes": null},
  "series": ["... oldest first ..."]
}
```

## Open items

- SSO: the token check (Entra ID) replaces the development header once the app registration
  exists.
- User tables: columns inferred from the data model diagram's table names (D-048), to confirm
  with the team; `vw_Notification` is not served yet.
- Risk flag count: defined here as current Warning/Negative key insights; to confirm with
  product (schema-contract open item 4).
