"""Build the company universe (PLAN 17a, D-055) and, with --sample N, the development sample:
the N largest companies of each industry group, as a review list.

    python scripts/build_universe.py                  # data/reports/universe-<date>.{csv,md}
    python scripts/build_universe.py --sample 5       # ... and sample-<date>.{md,yaml}
    python scripts/build_universe.py --clinical 20    # ... and clinical-<date>.{md,yaml} (D-058)

Reads SEC's exchange ticker list, every listed registrant's submissions (about 6,000 requests
at the SEC rate limit, about 45 minutes; a rerun the same day resumes) and the revenue frames. Lands
the ticker list and frames as they are and one summary line per registrant (D-055). Nothing is
written to config/: chosen companies are added to config/companies.yaml by hand after review.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from collections import Counter
from datetime import UTC, date, datetime
from pathlib import Path

import httpx

from uw_pipeline import universe as uv
from uw_pipeline.clients.ratelimit import TokenBucket
from uw_pipeline.clients.sec import SecClient
from uw_pipeline.clients.storage import Storage, make_storage
from uw_pipeline.config import load_companies, load_industry_config
from uw_pipeline.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
SAVE_EVERY = 250


def summaries(sec: SecClient, storage: Storage, ciks: list[int], key: str) -> dict[int, dict]:
    """Submissions summaries of `ciks`, resuming from today's landed file."""
    done: dict[int, dict] = {}
    if storage.exists(key):
        for line in storage.read_bytes(key).decode().splitlines():
            row = json.loads(line)
            done[row["cik"]] = row
    todo = [c for c in ciks if c not in done]
    print(f"submissions: {len(done)} landed today, {len(todo)} to read", flush=True)

    def save() -> None:
        storage.write_bytes(key, "".join(json.dumps(r) + "\n" for r in done.values()).encode())

    for i, cik in enumerate(todo, 1):
        try:
            done[cik] = uv.summarize(cik, sec.submissions(cik, land=False).payload)
        except httpx.HTTPStatusError as exc:
            done[cik] = {"cik": cik, "error": exc.response.status_code}
        if i % SAVE_EVERY == 0:
            save()
            print(f"  {i} of {len(todo)}", flush=True)
    save()
    return {c: r for c, r in done.items() if "error" not in r}


def frames(sec: SecClient, periods: tuple[str, ...]) -> dict[tuple[str, str], dict]:
    out = {}
    for tag in uv.FRAME_TAGS:
        for period in periods:
            try:
                out[tag, period] = sec.frame(tag, "USD", period).payload
            except httpx.HTTPStatusError as exc:
                print(f"frame {tag} {period}: HTTP {exc.response.status_code}")
    return out


def money(r: uv.Revenue | None) -> str:
    if r is None:
        return "—"
    notes = [x for x in ("interest income" if r.interest_income else "", r.note) if x]
    return f"${r.value / 1e9:,.2f}B {r.period}" + (f" ({'; '.join(notes)})" if notes else "")


def universe_md(entries: list[uv.Entry], industry, companies, since: date, today: date) -> str:
    scope = Counter(e.scope for e in entries)
    inside = [e for e in entries if e.scope == uv.IN_SCOPE]
    grouped = [e for e in inside if e.group != industry.default_group]
    out = [f"# Company universe {today}", "",
           f"Rule (D-055): CIKs with a ticker on {', '.join(uv.EXCHANGES)} whose latest annual "
           f"report since {since} is a 10-K. Listed registrants read: {len(entries)}.", "",
           "| Scope | Companies |", "|---|---|"]  # fmt: skip
    out += [f"| {uv.SCOPE_REASONS[k]} | {n:,} |" for k, n in scope.most_common()]
    share = len(grouped) / len(inside) if inside else 0
    out += ["", f"**In scope: {len(inside):,}; with an industry group: {len(grouped):,} "
            f"({share:.1%}).**", "", "| Group | Label | Companies | Share |",
            "|---|---|---|---|"]  # fmt: skip
    for g, n in Counter(e.group for e in inside).most_common():
        out.append(f"| {g} | {industry.groups[g].label} | {n:,} | {n / len(inside):.1%} |")
    rest = Counter(f"{e.sic or '—'} {e.sic_desc}".strip()
                   for e in inside if e.group == industry.default_group)  # fmt: skip
    out += ["", f"Largest SIC codes without a group ({industry.default_group}):", ""]
    out += [f"- {k}: {n}" for k, n in rest.most_common(15)]
    by_cik = {e.cik: e for e in entries}
    notes = []
    for c in companies:
        e = by_cik.get(c.cik)
        files = (f"; files under predecessor CIK {', '.join(map(str, c.predecessor_ciks))}"
                 if c.predecessor_ciks else "")  # fmt: skip
        if e is None:
            notes.append(f"- {c.ticker} ({c.cik}): no ticker on {', '.join(uv.EXCHANGES)}{files}")
        elif e.scope != uv.IN_SCOPE:
            notes.append(f"- {c.ticker} ({c.cik}): {uv.SCOPE_REASONS[e.scope]}{files}")
    out += ["", "## companies.yaml outside the universe", ""] + (notes or ["None."])
    return "\n".join(out) + "\n"


def sample_md(chosen: dict[str, list[uv.Entry]], industry, n: int, today: date) -> str:
    rows = [e for es in chosen.values() for e in es]
    new = [e for e in rows if not e.configured]
    out = [f"# Development sample {today}", "",
           f"The {n} largest in-scope companies of each industry group by revenue (PLAN 17a', "
           f"D-055): {len(rows)} companies in {len(chosen)} groups, {len(rows) - len(new)} "
           f"already in companies.yaml, {len(new)} new. To review: a company whose SIC puts "
           "it in the wrong group gets an `industry` entry in config/universe.yaml (then rerun: "
           "the group fills up again); `sample_skip` leaves one out.", ""]  # fmt: skip
    for g, es in chosen.items():
        short = f" (only {len(es)})" if len(es) < n else ""
        out += [f"## {g} — {industry.groups[g].label}{short}", "",
                "| # | Ticker | Name | SIC | Revenue | Note |",
                "|---|---|---|---|---|---|"]  # fmt: skip
        for i, e in enumerate(es, 1):
            how = {"company": "group from its `industry`", "universe": "group from universe.yaml"}
            note = ", ".join(x for x in ("in companies.yaml" if e.configured else "",
                                         how.get(e.how, "")) if x)  # fmt: skip
            out.append(f"| {i} | {e.ticker} | {e.name} | {e.sic} {e.sic_desc} | "
                       f"{money(e.revenue)} | {note} |")  # fmt: skip
        out.append("")
    return "\n".join(out)


def sample_yaml(chosen: dict[str, list[uv.Entry]], today: date) -> str:
    out = [f"# Development sample {today} (PLAN 17a', D-055): new companies, to review and copy",
           "# into config/companies.yaml. CIKs from SEC; names are proposals."]  # fmt: skip
    for g, es in chosen.items():
        new = [e for e in es if not e.configured]
        if not new:
            continue
        out.append(f"  # {g}")
        for e in new:
            name = json.dumps(uv.display_name(e.name), ensure_ascii=False)
            out += [f"  - ticker: {e.ticker}", f"    cik: {e.cik}", f"    display_name: {name}",
                    f"    news_aliases: [{name}]"]  # fmt: skip
            if e.how == "universe":
                reason = json.dumps(e.reason, ensure_ascii=False)
                out += [f"    industry: {e.group}", f"    industry_reason: {reason}"]
    return "\n".join(out) + "\n"


def clinical_md(cands: list[uv.Entry], rnd: dict[int, float], n: int, today: date) -> str:
    ranked = sorted(cands, key=lambda e: -rnd.get(e.cik, 0.0))
    out = [f"# Clinical-stage candidates {today}", "",
           f"Life-sciences SIC codes, revenue under ${uv.CLINICAL_REVENUE / 1e6:.0f}M or none "
           f"tagged (D-058): {len(cands)} companies; the {n} with the largest R&D expense are "
           "the development set. Confirm each (pre-revenue, not a revenue tag the frames miss) "
           "before adding `traits: [clinical_stage]`.", "",
           "| # | Ticker | Name | SIC | Group | Revenue | R&D | In companies.yaml |",
           "|---|---|---|---|---|---|---|---|"]  # fmt: skip
    for i, e in enumerate(ranked, 1):
        r = rnd.get(e.cik)
        shown = f"${r / 1e6:,.0f}M" if r else "—"
        out.append(f"| {i} | {e.ticker} | {e.name} | {e.sic} | {e.group} | {money(e.revenue)} | "
                   f"{shown} | {'yes' if e.configured else ''} |")  # fmt: skip
    return "\n".join(out) + "\n"


def clinical_yaml(cands: list[uv.Entry], rnd: dict[int, float], n: int, today: date) -> str:
    out = [f"# Clinical-stage development set {today} (D-058): to review and copy into",
           "# config/companies.yaml."]  # fmt: skip
    for e in sorted(cands, key=lambda e: -rnd.get(e.cik, 0.0))[:n]:
        if e.configured:
            continue
        name = json.dumps(uv.display_name(e.name), ensure_ascii=False)
        out += [f"  - ticker: {e.ticker}", f"    cik: {e.cik}", f"    display_name: {name}",
                f"    news_aliases: [{name}]", "    traits: [clinical_stage]"]  # fmt: skip
    return "\n".join(out) + "\n"


def entries_csv(entries: list[uv.Entry]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["cik", "ticker", "exchange", "name", "sic", "sic_desc", "scope", "annual_form",
                "group", "how", "revenue", "revenue_period", "revenue_tag",
                "configured"])  # fmt: skip
    for e in entries:
        r = e.revenue
        w.writerow([e.cik, e.ticker, e.exchange, e.name, e.sic or "", e.sic_desc, e.scope,
                    e.annual_form or "", e.group, e.how, r.value if r else "",
                    r.period if r else "", r.tag if r else "", e.configured])  # fmt: skip
    return buf.getvalue()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sample", type=int, default=0, help="also list the N largest per group")
    ap.add_argument("--clinical", type=int, default=0,
                    help="also list clinical-stage candidates, most R&D first")  # fmt: skip
    ap.add_argument("--since", type=date.fromisoformat, default=date(2024, 7, 1),
                    help="annual report on or after this date (default 2024-07-01)")  # fmt: skip
    args = ap.parse_args()
    settings = Settings()
    config_dir = settings.config_dir
    industry = load_industry_config(config_dir)
    companies = load_companies(config_dir)
    industry.check_companies(companies)
    universe = uv.load_universe_config(config_dir)
    universe.check(industry)
    today = datetime.now(UTC).date()
    periods = (f"CY{today.year - 1}", f"CY{today.year - 2}")
    storage = make_storage(settings)
    with SecClient(settings.sec_user_agent, storage,
                   rate_limiter=TokenBucket(rate=settings.sec_rate_limit_per_sec),
                   timeout=settings.http_timeout_sec,
                   max_attempts=settings.sec_max_attempts) as sec:  # fmt: skip
        listing = uv.listed(sec.company_tickers_exchange())
        print(f"listed on {', '.join(uv.EXCHANGES)}: {len(listing)} registrants", flush=True)
        summ = summaries(sec, storage, sorted(listing), f"sec/universe/{today}.jsonl")
        revenue = uv.revenue_by_cik(frames(sec, periods), periods)
        rnd_frames: dict[str, dict] = {}
        if args.clinical:
            for period in periods:
                try:
                    rnd_frames[period] = sec.frame(uv.RND_TAG, "USD", period).payload
                except httpx.HTTPStatusError as exc:
                    print(f"frame {uv.RND_TAG} {period}: HTTP {exc.response.status_code}")
    entries = uv.build(listing, summ, revenue, industry, companies, args.since, universe)
    reports = ROOT / "data" / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / f"universe-{today}.csv").write_text(entries_csv(entries), encoding="utf-8")
    md = universe_md(entries, industry, companies, args.since, today)
    (reports / f"universe-{today}.md").write_text(md, encoding="utf-8")
    print(f"wrote data/reports/universe-{today}.md and .csv")
    if args.sample:
        skip = [s.cik for s in universe.sample_skip]
        chosen = uv.sample(entries, args.sample, industry.default_group, skip)
        (reports / f"sample-{today}.md").write_text(
            sample_md(chosen, industry, args.sample, today), encoding="utf-8")  # fmt: skip
        (reports / f"sample-{today}.yaml").write_text(sample_yaml(chosen, today), encoding="utf-8")
        print(f"wrote data/reports/sample-{today}.md and .yaml")
    if args.clinical:
        cands = uv.clinical_candidates(entries)
        rnd = uv.latest_by_cik(rnd_frames, periods)
        (reports / f"clinical-{today}.md").write_text(
            clinical_md(cands, rnd, args.clinical, today), encoding="utf-8")  # fmt: skip
        (reports / f"clinical-{today}.yaml").write_text(
            clinical_yaml(cands, rnd, args.clinical, today), encoding="utf-8")  # fmt: skip
        print(f"wrote data/reports/clinical-{today}.md and .yaml ({len(cands)} candidates)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
