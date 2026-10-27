"""Fill config/logos/ with company logos from Wikimedia (D-067).

    python scripts/fetch_logos.py                 # active companies without a logo file
    python scripts/fetch_logos.py --ticker MAR    # fetch again, replacing the file
    python scripts/fetch_logos.py --all           # every active company again, except logos
                                                  # added by hand (source: manual)

The pipeline never fetches logos: the corporate network does not reach Wikimedia. Run this on
one that does, look at the new images (data/reports/logos.html shows them all), then commit
them. A logo can also be added by hand: a PNG, JPEG, GIF or WebP file named by ticker
(`MAR.png`), at most 200 KB, about 250 px wide.
Where to look, and corrections: config/logos.yaml. What each file came from:
config/logos/SOURCES.yaml. `uw-pipeline config check` validates the files.
"""

from __future__ import annotations

import argparse
import html
from datetime import UTC, datetime
from pathlib import Path

import yaml

from uw_pipeline.clients.logos import LogoClient
from uw_pipeline.config import CompanyConfig, load_companies, load_logo_config
from uw_pipeline.extract.logos import find_logo
from uw_pipeline.logos import EXTENSIONS, LOGO_DIR, image_type, load_logos, logo_files
from uw_pipeline.settings import Settings

SOURCES = "SOURCES.yaml"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--ticker", nargs="*", default=[], help="fetch these again")
    ap.add_argument("--all", action="store_true", help="fetch every active company again")
    args = ap.parse_args()
    settings = Settings()
    cfg_dir = settings.config_dir
    folder = cfg_dir / LOGO_DIR
    folder.mkdir(exist_ok=True)
    lcfg = load_logo_config(cfg_dir)
    companies = [c for c in load_companies(cfg_dir) if c.active]
    lcfg.check_companies(companies)
    have = logo_files(cfg_dir)
    wanted = {t.upper() for t in args.ticker}
    sources_path = folder / SOURCES
    sources = (yaml.safe_load(sources_path.read_text(encoding="utf-8")) or {}
               if sources_path.exists() else {})  # fmt: skip
    manual = {t for t, v in sources.items() if v.get("source") == "manual"}
    todo = [c for c in companies if c.ticker in wanted or (not wanted and (
        c.ticker not in have or (args.all and c.ticker not in manual)))]  # fmt: skip
    client = LogoClient(settings.sec_user_agent, timeout=60.0)
    today = datetime.now(UTC).date()
    missing = []
    try:
        for c in todo:
            try:
                logo = find_logo(c, client, lcfg, today)
            except Exception as exc:  # one company's failure does not stop the others
                print(f"{c.ticker:6} error: {exc}")
                missing.append(c.ticker)
                continue
            if logo.image is None:
                print(f"{c.ticker:6} none found (wikidata item {logo.wikidata_id})")
                missing.append(c.ticker)
                continue
            kind = image_type(logo.image)
            ext = next(e for e, k in EXTENSIONS.items() if k == kind)
            for old in folder.glob(f"{c.ticker}.*"):
                if old.suffix.lower() in EXTENSIONS:
                    old.unlink()
            (folder / f"{c.ticker}{ext}").write_bytes(logo.image)
            sources[c.ticker] = {
                "source": logo.source, "url": logo.source_url,
                "wikidata": logo.wikidata_id, "fetched": today.isoformat(),
            }  # fmt: skip
            print(f"{c.ticker:6} {logo.source:9} {len(logo.image) // 1024:3} KB  {logo.source_url}")
    finally:
        client.close()
        header = (
            "# Where each logo in this folder came from (scripts/fetch_logos.py, D-067).\n"
            "# Add an entry when adding a logo by hand: source: manual, url: where from.\n"
        )
        body = yaml.safe_dump(dict(sorted(sources.items())), sort_keys=False, allow_unicode=True)
        sources_path.write_text(header + body, encoding="utf-8")
    print(f"{len(todo) - len(missing)} of {len(todo)} fetched; none for: {' '.join(missing)}")
    sheet = settings.data_dir / "reports" / "logos.html"
    sheet.parent.mkdir(parents=True, exist_ok=True)
    sheet.write_text(contact_sheet(companies, cfg_dir, sources), encoding="utf-8")
    print(f"review: {sheet}")


def contact_sheet(companies: list[CompanyConfig], cfg_dir: Path,
                  sources: dict[str, dict[str, str]]) -> str:  # fmt: skip
    """Every company's logo on a light and a dark tile, with its source."""
    logos = load_logos(cfg_dir)
    tiles = []
    for c in companies:
        uri = logos.get(c.ticker)
        src = sources.get(c.ticker, {})
        img = f'<img src="{uri}" alt="">' if uri else "<i>none</i>"
        label = (
            f"<b>{html.escape(c.ticker)}</b> {html.escape(c.display_name or '')}<br>"
            + html.escape(str(src.get("source", "")))
        )
        tiles.append(
            f'<figure><div>{img}</div><div class="dark">{img}</div>'
            f"<figcaption>{label}</figcaption></figure>"
        )
    style = ("body{font:13px system-ui;margin:16px;background:#f4f4f4}main{display:grid;"
             "grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:12px}"
             "figure{margin:0;background:#fff;padding:8px;border:1px solid #ddd}"
             "figure div{height:56px;display:flex;align-items:center;justify-content:center}"
             "figure div.dark{background:#222}img{max-height:48px;max-width:160px}")  # fmt: skip
    return (
        f"<!doctype html><meta charset=utf-8><title>Company logos</title><style>{style}"
        f"</style><p>{len(logos)} of {len(companies)} companies have a logo.</p>"
        f"<main>{''.join(tiles)}</main>"
    )


if __name__ == "__main__":
    main()
