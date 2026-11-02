"""Fetch the four official Alpha Vantage demo examples and exercise production parsers.

    .venv/bin/python scripts/alphavantage_demo.py --refresh

Only the public demo key is used. No account, .env, pipeline DB or app DB is changed.
The examples deliberately use IBM and AAPL; demo is not a general-purpose API credential.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from uw_pipeline.clients.alphavantage import AlphaVantageClient
from uw_pipeline.clients.storage import LocalStorage
from uw_pipeline.jobs.run_market import earnings_rows, price_rows
from uw_pipeline.jobs.run_news import load_news_config
from uw_pipeline.jobs.run_transcripts import transcript_text
from uw_pipeline.normalize.news import parse_feed
from uw_pipeline.normalize.sections import chunk

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = {
    "earnings": {"function": "EARNINGS", "symbol": "IBM"},
    "transcript": {"function": "EARNINGS_CALL_TRANSCRIPT", "symbol": "IBM", "quarter": "2024Q1"},
    "daily": {"function": "TIME_SERIES_DAILY_ADJUSTED", "symbol": "IBM"},
    "news": {"function": "NEWS_SENTIMENT", "tickers": "AAPL"},
}


def inspect_payload(
    name: str, payload: dict[str, Any], key: str, now: datetime, config_dir: Path
) -> dict[str, Any]:
    """Check identity/content before reporting success; use the pipeline's actual parsers."""
    if name in ("earnings", "transcript") and payload.get("symbol") != "IBM":
        raise ValueError("response symbol is not IBM")
    if name == "earnings":
        rows = earnings_rows(payload, "IBM", key, now)
        if not rows or not any(r["reported_eps"] is not None for r in rows):
            raise ValueError("no usable quarterly EPS records")
        return {"symbol": "IBM", "quarterly_earnings": len(rows)}
    if name == "transcript":
        if payload.get("quarter") != "2024Q1":
            raise ValueError("response quarter is not 2024Q1")
        turns = payload.get("transcript") or []
        text = transcript_text(turns)
        if not turns or not text.strip() or not any(t.get("content", "").strip() for t in turns):
            raise ValueError("no usable transcript content")
        return {
            "symbol": "IBM",
            "quarter": "2024Q1",
            "turns": len(turns),
            "characters": len(text),
            "chunks": len(chunk(text)),
        }
    if name == "daily":
        if payload.get("Meta Data", {}).get("2. Symbol") != "IBM":
            raise ValueError("price response symbol is not IBM")
        rows = price_rows(payload, "IBM", "1900-01-01", key, now)
        if not rows or len(rows) != len(payload.get("Time Series (Daily)") or {}):
            raise ValueError("missing or unusable daily adjusted prices")
        dates = [r["trade_date"] for r in rows]
        return {
            "symbol": "IBM",
            "prices": len(rows),
            "first_date": min(dates),
            "last_date": max(dates),
        }
    cfg = load_news_config(config_dir)
    items, dropped = parse_feed(payload, "AAPL", cfg, ["Apple"])
    if not items:
        raise ValueError("no relevant AAPL articles after production news filters")
    dates = [i.published_at.isoformat() for i in items]
    return {
        "symbol": "AAPL",
        "articles": len(payload.get("feed") or []),
        "accepted": len(items),
        "filtered": dropped,
        "first_date": min(dates),
        "last_date": max(dates),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true", help="Fetch demo examples again")
    parser.add_argument("--raw-dir", type=Path, default=ROOT / "data/av-demo/raw")
    parser.add_argument("--report", type=Path, default=ROOT / "data/av-demo/demo-result.json")
    parser.add_argument("--config-dir", type=Path, default=ROOT / "config")
    args = parser.parse_args(argv)
    now = datetime.now(UTC)
    results: list[dict[str, Any]] = []
    with AlphaVantageClient("demo", LocalStorage(args.raw_dir)) as client:
        before = client.calls_today()
        for name, params in EXAMPLES.items():
            key = f"av/demo/{name}/{now:%Y-%m-%d}.json"
            try:
                payload = client.fetch(params, key, force=args.refresh)
                detail = inspect_payload(name, payload, key, now, args.config_dir)
                result = {"check": name, "ok": True, "raw_key": key, **detail}
            except Exception as exc:
                result = {
                    "check": name,
                    "ok": False,
                    "error_type": type(exc).__name__,
                    "detail": str(exc).replace("apikey=demo", "apikey=***")[:400],
                }
            results.append(result)
            print(json.dumps(result, ensure_ascii=False), flush=True)
        calls = client.calls_today() - before
    report = {
        "mode": "official_demo",
        "checked_at": now.isoformat(),
        "http_requests": calls,
        "checks": results,
        "scope": "Official examples and production parsers; no pipeline/app DB writes",
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    ok = all(r["ok"] for r in results)
    print(
        f"{'PASS' if ok else 'FAIL'}: {sum(r['ok'] for r in results)}/4 demo checks; "
        f"HTTP requests={calls}; report={args.report}"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
