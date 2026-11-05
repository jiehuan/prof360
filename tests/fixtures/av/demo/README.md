# Official Alpha Vantage demo response excerpts

Recorded on 2026-10-26 (Asia/Shanghai) using the public demo examples in the
[official documentation](https://www.alphavantage.co/documentation/):

- `earnings.json`: EARNINGS, IBM; first annual row and first two quarterly rows.
- `transcript.json`: EARNINGS_CALL_TRANSCRIPT, IBM, 2024Q1; first two speaker turns.
- `daily.json`: TIME_SERIES_DAILY_ADJUSTED, IBM, default compact output; first two bars.
- `news.json`: NEWS_SENTIMENT, AAPL; one article accepted and one filtered by the existing
  pipeline rules; `items` adjusted to the excerpt length of two.

Retained rows and values are unmodified. These excerpts exercise the demo smoke script without
network access; full locally fetched responses remain in the ignored `data/av-demo/raw/`.
The demo supports specific sample requests and is not an account credential or production key.
