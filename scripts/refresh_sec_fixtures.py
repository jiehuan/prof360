"""Rebuild tests/fixtures/sec from the latest raw files under data/raw/sec.

Run `uw-pipeline run --all --stage company` first, then this script. Fixtures are trimmed
(ticker list to the tickers tests need, filing history to a few rows) to keep the repo small.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw" / "sec"
OUT = ROOT / "tests" / "fixtures" / "sec"
KEEP_TICKERS = {"MAR", "LUV", "F", "PFE", "JPM", "M", "LUMN", "SKYD", "PSKY", "PARA", "ENPH"}
KEEP_FILINGS = 5


def latest(directory: Path) -> Path:
    files = sorted(directory.glob("*.json"))
    if not files:
        sys.exit(f"no raw files in {directory}; run the company stage first")
    return files[-1]


def main() -> None:
    tickers = json.loads(latest(RAW / "company_tickers").read_text())
    kept = [row for row in tickers.values() if row["ticker"] in KEEP_TICKERS]
    out = {str(i): row for i, row in enumerate(kept)}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "company_tickers.json").write_text(json.dumps(out, indent=1) + "\n")

    (OUT / "submissions").mkdir(exist_ok=True)
    for cik_dir in sorted((RAW / "submissions").iterdir()):
        payload = json.loads(latest(cik_dir).read_text())
        recent = payload["filings"]["recent"]
        payload["filings"]["recent"] = {k: v[:KEEP_FILINGS] for k, v in recent.items()}
        payload["filings"]["files"] = payload["filings"]["files"][:1]
        target = OUT / "submissions" / f"{cik_dir.name}.json"
        target.write_text(json.dumps(payload, indent=1) + "\n")
        print(f"wrote {target.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
