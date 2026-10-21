"""Rebuild tests/fixtures/sec from the latest raw files under data/raw/sec.

Run `uw-pipeline run --all` first, then this script. `--only T1 T2 ...` writes the fixtures of
those tickers only and keeps every other fixture as it is (a full rebuild re-trims the existing
ones to the past 13 months from today). Fixtures are trimmed to keep the repo small:
- ticker list: only the tickers tests need
- submissions (the MVP and golden companies): the latest few filings plus the 10-K/10-Qs of
  the past ~13 months
- companyfacts (golden companies only): only the tags the registry uses, for periods ending in
  the last seven years
- XBRL instances (golden companies only): only facts the registry reads from instances,
  plus the contexts and units they reference
"""

from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from pathlib import Path

from uw_pipeline.compute.registry import load_registry
from uw_pipeline.config import load_companies, load_industry_config
from uw_pipeline.extract.filings import instance_name
from uw_pipeline.normalize.company import parse_sic
from uw_pipeline.normalize.xbrl_facts import PERIODIC_FORMS

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw" / "sec"
OUT = ROOT / "tests" / "fixtures" / "sec"
KEEP_TICKERS = {"MAR", "LUV", "F", "PFE", "JPM", "M", "LUMN", "SKYD", "PSKY", "PARA", "ENPH"}
KEEP_FILINGS = 5
PERIODIC_LOOKBACK_DAYS = 400
# Phase 16: two companies per new or changed industry module (golden values, PLAN 16e).
GOLDEN = {
    "MAR", "PFE", "MSFT", "CRM", "CSCO", "AAPL", "NVDA", "AMD", "MDT", "ISRG", "KO", "PG",
    "DUK", "NEE", "LMT", "BA", "UNP", "UPS", "V", "MA", "BAC", "COF", "GS", "MS", "COST", "WMT",
    # Phase 17 (D-056): two companies per new industry module.
    "DOW", "LYB", "NEM", "FCX", "AA", "CLF", "DHI", "TOL", "HCA", "UHS", "MGM", "LVS", "MCD",
    "SBUX",
}  # fmt: skip
FACT_YEARS = 7

_XBRLI = "{http://www.xbrl.org/2003/instance}"


def latest(directory: Path) -> Path:
    files = sorted(directory.glob("*.json"))
    if not files:
        sys.exit(f"no raw files in {directory}; run the pipeline first")
    return files[-1]


def trim_submissions(payload: dict, today: date) -> dict:
    recent = payload["filings"]["recent"]
    since = (today - timedelta(days=PERIODIC_LOOKBACK_DAYS)).isoformat()
    keep = [
        i
        for i in range(len(recent["form"]))
        if i < KEEP_FILINGS
        or (recent["form"][i] in PERIODIC_FORMS and recent["filingDate"][i] >= since)
    ]
    payload["filings"]["recent"] = {k: [v[i] for i in keep] for k, v in recent.items()}
    payload["filings"]["files"] = []  # tests never page through older filings
    return payload


def trim_companyfacts(payload: dict, tags: set[tuple[str, str]], since: str) -> dict:
    def recent(body: dict) -> dict:
        units = {u: [v for v in vals if v["end"] >= since] for u, vals in body["units"].items()}
        return body | {"units": {u: vals for u, vals in units.items() if vals}}

    payload["facts"] = {
        tax: {name: recent(body) for name, body in names.items() if (tax, name) in tags}
        for tax, names in payload["facts"].items()
    }
    return payload


def trim_instance(xml: bytes, wanted_tags: set[str]) -> bytes:
    """Keep facts whose prefixed tag is wanted, plus the contexts and units they use."""
    prefixes: dict[str, str] = {}
    for _, (prefix, uri) in ET.iterparse(_bytes_io(xml), events=("start-ns",)):
        prefixes.setdefault(uri, prefix)
        ET.register_namespace(prefix, uri)
    root = ET.fromstring(xml)
    facts, ctx_ids, unit_ids = [], set(), set()
    for el in list(root):
        if el.get("contextRef") is None or not el.tag.startswith("{"):
            continue
        uri, local = el.tag[1:].split("}", 1)
        if f"{prefixes.get(uri, uri)}:{local}" in wanted_tags:
            facts.append(el)
            ctx_ids.add(el.get("contextRef"))
            if el.get("unitRef"):
                unit_ids.add(el.get("unitRef"))
    for el in list(root):
        keep = (
            el in facts
            or (el.tag == f"{_XBRLI}context" and el.get("id") in ctx_ids)
            or (el.tag == f"{_XBRLI}unit" and el.get("id") in unit_ids)
        )
        if not keep:
            root.remove(el)
    return ET.tostring(root, xml_declaration=True, encoding="utf-8")


def _bytes_io(data: bytes):  # type: ignore[no-untyped-def]
    import io

    return io.BytesIO(data)


def main() -> None:
    today = date.today()
    only = set(sys.argv[sys.argv.index("--only") + 1 :]) if "--only" in sys.argv else None
    unknown = (only or set()) - (KEEP_TICKERS | GOLDEN)
    if unknown:
        sys.exit(f"add {sorted(unknown)} to KEEP_TICKERS or GOLDEN first")
    tickers = json.loads(latest(RAW / "company_tickers").read_text())
    kept = [row for row in tickers.values() if row["ticker"] in KEEP_TICKERS | GOLDEN]
    OUT.mkdir(parents=True, exist_ok=True)
    if only is not None:  # existing rows stay as they are; the new tickers are added
        old = list(json.loads((OUT / "company_tickers.json").read_text()).values())
        have = {row["ticker"] for row in old}
        kept = old + [row for row in kept if row["ticker"] in only and row["ticker"] not in have]
    out = {str(i): row for i, row in enumerate(kept)}
    (OUT / "company_tickers.json").write_text(json.dumps(out, indent=1) + "\n")

    config = ROOT / "config"
    registry = load_registry(config)
    industry = load_industry_config(config)
    by_cik = {f"CIK{c.cik:010d}": c for c in load_companies(config) if c.cik}

    (OUT / "submissions").mkdir(exist_ok=True)
    wanted = only if only is not None else KEEP_TICKERS | GOLDEN
    for cik_dir in sorted(d for d in (RAW / "submissions").iterdir() if d.is_dir()):
        cfg = by_cik.get(cik_dir.name)
        if cfg is None or cfg.ticker not in wanted:
            continue  # predecessor registrants and companies the tests do not use
        payload = json.loads(latest(cik_dir).read_text())
        subs = trim_submissions(payload, today)
        target = OUT / "submissions" / f"{cik_dir.name}.json"
        target.write_text(json.dumps(subs, indent=1) + "\n")
        print(f"wrote {target.relative_to(ROOT)}")

        if cfg.ticker not in GOLDEN:
            continue
        # The company's own industry (D-049), else its SIC code's.
        group = (industry.groups[cfg.industry] if cfg.industry
                 else industry.classify(parse_sic(subs))[0])  # fmt: skip
        refs = registry.for_company(cfg.ticker, group).tag_refs()
        cf = json.loads(latest(RAW / "companyfacts" / cik_dir.name).read_text())
        std = {(r.taxonomy, r.name) for r in refs if not r.needs_instance}
        target = OUT / "companyfacts" / f"{cik_dir.name}.json"
        target.parent.mkdir(exist_ok=True)
        since = (today - timedelta(days=365 * FACT_YEARS)).isoformat()
        target.write_text(json.dumps(trim_companyfacts(cf, std, since)) + "\n")
        print(f"wrote {target.relative_to(ROOT)}")

        inst_tags = {r.tag for r in refs if r.needs_instance}
        recent = subs["filings"]["recent"]
        for i, form in enumerate(recent["form"]):
            if form not in PERIODIC_FORMS:
                continue
            accn = recent["accessionNumber"][i]
            folder = RAW / "filings" / cik_dir.name / accn
            if not (folder / "index.json").exists():
                continue  # the facts stage never needed this filing's instance
            names = json.loads((folder / "index.json").read_text())["directory"]["item"]
            name = instance_name([n["name"] for n in names], recent["primaryDocument"][i])
            if name is None:
                continue
            dest = OUT / "filings" / cik_dir.name / accn
            dest.mkdir(parents=True, exist_ok=True)
            index = {"directory": {"item": [{"name": name}]}}
            (dest / "index.json").write_text(json.dumps(index) + "\n")
            (dest / name).write_bytes(trim_instance((folder / name).read_bytes(), inst_tags))
            print(f"wrote {(dest / name).relative_to(ROOT)}")


if __name__ == "__main__":
    main()
