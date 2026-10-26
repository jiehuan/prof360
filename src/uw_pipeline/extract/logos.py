"""Find a company's logo on Wikimedia (D-067), for scripts/fetch_logos.py: the pipeline itself
never fetches logos (the corporate network does not reach Wikimedia).

Sources and order are described in config/logos.yaml. Wikidata is matched by the company's
pinned CIK and its predecessors' (never by ticker or name: both find wrong companies), or by the
item named in `overrides`.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import unquote, urlparse

from uw_pipeline.clients.logos import COMMONS_API, ENWIKI_API, LogoClient
from uw_pipeline.config import CompanyConfig, LogoConfig, LogoOverride
from uw_pipeline.logos import MAX_BYTES, image_size, image_type

log = logging.getLogger(__name__)

PREFERRED = "http://wikiba.se/ontology#PreferredRank"
DEPRECATED = "http://wikiba.se/ontology#DeprecatedRank"


@dataclass(frozen=True)
class Logo:
    image: bytes | None  # PNG, JPEG, GIF or WebP; None: nothing found
    source: str  # wikidata | wikipedia | favicon | none
    source_url: str | None  # file page, or the website whose icon it is
    wikidata_id: str | None


def find_logo(cfg: CompanyConfig, client: LogoClient, lcfg: LogoConfig, today: date) -> Logo:
    override = lcfg.overrides.get(cfg.ticker) or LogoOverride()
    ciks = [f"{c:010d}" for c in [cfg.cik, *cfg.predecessor_ciks] if c is not None]
    found = _choose(client.sparql(_query(ciks, override.wikidata)), ciks, today)
    qid = found.qid
    article = override.wikipedia or found.article
    if article and not override.logo_file:
        # 1. the logo in the English Wikipedia article's infobox: kept current (Wikidata's is
        # sometimes a former name's or another unit's), and non-free logos are only there
        infobox = infobox_logo(client.wikitext(article) or "")
        if infobox and (got := _thumbnail(cfg, client, lcfg, ENWIKI_API, infobox)):
            return Logo(got[0], "wikipedia", got[1], qid)
    files: list[str] = [override.logo_file] if override.logo_file else found.files
    for name in files:  # 2. Wikidata's logo, a Commons file
        if got := _thumbnail(cfg, client, lcfg, COMMONS_API, name):
            return Logo(got[0], "wikidata", got[1], qid)
    site = override.website or found.website
    host = urlparse(site).hostname if site else None
    if host:  # 3. the website's icon
        icon = client.favicon(host, lcfg.favicon_size)
        size = image_size(icon) if icon else None
        if icon and size and min(size) >= lcfg.min_favicon_px and len(icon) <= MAX_BYTES:
            return Logo(icon, "favicon", site, qid)
    return Logo(None, "none", None, qid)


def _thumbnail(cfg: CompanyConfig, client: LogoClient, lcfg: LogoConfig, api: str,
               name: str) -> tuple[bytes, str] | None:  # fmt: skip
    """(image, file page) of a wiki file `lcfg.width` pixels wide."""
    thumb = client.thumbnail(api, name, lcfg.width)
    if thumb is None:
        log.warning("%s: no file %r at %s", cfg.ticker, name, api)
        return None
    image = client.image(thumb[0])
    if not image or image_type(image) is None or len(image) > MAX_BYTES:
        return None
    return image, thumb[1]


def infobox_logo(wikitext: str) -> str | None:
    """The file named by the infobox's `logo` parameter: `X.svg`, `File:X.svg` or
    `[[File:X.svg|...]]`; None when empty or not an image file."""
    m = re.search(r"^\s*\|\s*logo\s*=[ \t]*(.*)$", wikitext, re.MULTILINE | re.IGNORECASE)
    if not m:
        return None
    value = re.sub(r"^\[\[\s*(?:File|Image)\s*:", "", m.group(1).strip(), flags=re.IGNORECASE)
    value = re.sub(r"^(?:File|Image)\s*:", "", value, flags=re.IGNORECASE)
    name = re.split(r"\||\]\]|<!--", value)[0].strip()
    return name if re.search(r"\.(svg|png|jpe?g|gif|webp)$", name, re.IGNORECASE) else None


def _query(ciks: list[str], qid: str | None) -> str:
    if qid:
        match = f"VALUES ?item {{ wd:{qid} }}"
    else:
        values = " ".join(f'"{c}"' for c in ciks)
        match = f"VALUES ?cik {{ {values} }} ?item wdt:P5531 ?cik ."
    return f"""SELECT ?item ?cik ?file ?rank ?start ?end ?icon ?website ?article WHERE {{
  {match}
  OPTIONAL {{ ?item p:P154 ?st . ?st ps:P154 ?file ; wikibase:rank ?rank .
             OPTIONAL {{ ?st pq:P580 ?start }} OPTIONAL {{ ?st pq:P582 ?end }} }}
  OPTIONAL {{ ?item wdt:P8972 ?icon }}
  OPTIONAL {{ ?item wdt:P856 ?website }}
  OPTIONAL {{ ?article schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/> }}
}}"""


@dataclass
class _Item:
    qid: str
    ciks: set[str] = field(default_factory=set)
    logos: set[tuple[int, str, str]] = field(default_factory=set)  # (rank, start, file)
    icons: set[str] = field(default_factory=set)
    sites: set[str] = field(default_factory=set)
    articles: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class Found:
    qid: str | None = None
    files: list[str] = field(default_factory=list)  # Commons files to try, in order
    website: str | None = None
    article: str | None = None  # English Wikipedia title


def _choose(rows: list[dict[str, str]], ciks: list[str], today: date) -> Found:
    """Items matched by the pinned CIK come before predecessors'; an item's current logos (not
    deprecated, end date not passed; preferred rank, then the latest start) before any item's
    small logo or icon."""
    items: dict[str, _Item] = {}
    for r in rows:
        qid = r["item"].rsplit("/", 1)[-1]
        it = items.setdefault(qid, _Item(qid))
        if "cik" in r:
            it.ciks.add(r["cik"])
        if "file" in r and r.get("rank") != DEPRECATED and not _ended(r.get("end"), today):
            it.logos.add((0 if r.get("rank") == PREFERRED else 1, r.get("start", ""),
                          _file(r["file"])))  # fmt: skip
        if "icon" in r:
            it.icons.add(_file(r["icon"]))
        if "website" in r:
            it.sites.add(r["website"])
        if "article" in r:
            it.articles.add(unquote(r["article"].rsplit("/wiki/", 1)[-1]).replace("_", " "))

    def order(it: _Item) -> tuple[int, int]:
        return min((ciks.index(c) for c in it.ciks if c in ciks), default=0), int(it.qid[1:])

    ranked = sorted(items.values(), key=order)
    files: list[str] = []
    for it in ranked:  # latest start first within a rank (stable sorts)
        by_start = sorted(it.logos, key=lambda x: x[1], reverse=True)
        files += [f for _, _, f in sorted(by_start, key=lambda x: x[0]) if f not in files]
    for it in ranked:
        files += [f for f in sorted(it.icons) if f not in files]
    with_logo = [it for it in ranked if it.logos or it.icons] or ranked
    sites = [s for it in ranked for s in sorted(it.sites)]
    articles = [a for it in ranked for a in sorted(it.articles)]
    return Found(with_logo[0].qid if with_logo else None, files,
                 sites[0] if sites else None, articles[0] if articles else None)  # fmt: skip


def _ended(end: str | None, today: date) -> bool:
    return end is not None and end[:10] <= today.isoformat()


def _file(value: str) -> str:
    """Commons file name of a Wikidata image value (a Special:FilePath URL)."""
    return unquote(value.rsplit("/", 1)[-1])
