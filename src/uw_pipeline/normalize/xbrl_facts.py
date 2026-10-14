"""XBRL facts (companyfacts JSON or a filing's XBRL instance) -> one long table.

Both sources produce the same columns (FACT_COLUMNS); `dims` is '' for non-dimensional facts.
Only facts for the requested TagRefs are kept. The raw files stay in the landing zone for audit.
"""

from __future__ import annotations

import io
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable
from typing import Any

import pandas as pd

from uw_pipeline.compute.registry import TagRef, dims_key

FACT_COLUMNS = [
    "taxonomy", "tag", "dims", "unit", "val", "start", "end",
    "fy", "fp", "form", "accn", "filed", "frame", "source",
]  # fmt: skip

# Only periodic reports feed metrics; 8-K/S-1 etc. are ignored.
PERIODIC_FORMS = frozenset({"10-K", "10-K/A", "10-Q", "10-Q/A", "10-KT", "10-KT/A"})

_XBRLI = "{http://www.xbrl.org/2003/instance}"
_XBRLDI = "{http://xbrl.org/2006/xbrldi}"
_XSI_NIL = "{http://www.w3.org/2001/XMLSchema-instance}nil"


def _empty() -> pd.DataFrame:
    return pd.DataFrame(columns=FACT_COLUMNS)


def facts_from_companyfacts(payload: dict[str, Any], refs: Iterable[TagRef]) -> pd.DataFrame:
    wanted = {(r.taxonomy, r.name) for r in refs if not r.dims}
    rows: list[dict[str, Any]] = []
    for taxonomy, tags in (payload.get("facts") or {}).items():
        for name, body in tags.items():
            if (taxonomy, name) not in wanted:
                continue
            for unit, facts in body.get("units", {}).items():
                for f in facts:
                    if f.get("form") not in PERIODIC_FORMS:
                        continue
                    rows.append(
                        {
                            "taxonomy": taxonomy,
                            "tag": name,
                            "dims": "",
                            "unit": unit,
                            "val": float(f["val"]),
                            "start": f.get("start"),
                            "end": f["end"],
                            "fy": f.get("fy"),
                            "fp": f.get("fp"),
                            "form": f["form"],
                            "accn": f["accn"],
                            "filed": f["filed"],
                            "frame": f.get("frame"),
                            "source": "companyfacts",
                        }
                    )
    return pd.DataFrame(rows, columns=FACT_COLUMNS) if rows else _empty()


def facts_from_instance(
    xml: bytes,
    refs: Iterable[TagRef],
    *,
    accn: str,
    form: str,
    filed: str,
) -> pd.DataFrame:
    """Parse an XBRL instance document, keeping facts that match `refs` exactly (incl. dims)."""
    wanted = {(r.tag, r.dims_key) for r in refs}
    return _instance_facts(xml, lambda tag, dims: (tag, dims) in wanted,
                           accn=accn, form=form, filed=filed)  # fmt: skip


# Pay-versus-performance (DEF 14A, `ecd` taxonomy): amounts for the principal executive officer
# are tagged either without dimensions or per person on this axis (Marriott does the latter).
PEO_AXIS = "ecd:IndividualAxis"


def pay_versus_performance(
    xml: bytes, refs: Iterable[TagRef], *, accn: str, form: str, filed: str
) -> pd.DataFrame:
    """The proxy statement's pay-versus-performance facts for `refs` (non-dimensional tags).

    A PEO amount tagged only per person counts when one person held the role that year; with
    two or more (a mid-year change) the year has no value, since adding them up is wrong.
    A person shown with 0 for a year did not hold the role then (Southwest lists both CEOs every
    year)."""
    wanted = {r.tag for r in refs if not r.dims}
    peo_dims = f"{PEO_AXIS}="
    raw = _instance_facts(
        xml,
        lambda tag, dims: tag in wanted and (dims == "" or (dims.startswith(peo_dims)
                                                            and "|" not in dims)),
        accn=accn, form=form, filed=filed,
    )  # fmt: skip
    if raw.empty:
        return raw
    keep = []
    for _, group in raw.groupby(["taxonomy", "tag", "start", "end", "unit"], dropna=False):
        plain = group[group["dims"] == ""]
        people = group[(group["dims"] != "") & (group["val"] != 0)]
        if not plain.empty:
            keep.append(plain.iloc[[0]])
        elif people["dims"].nunique() == 1:
            keep.append(people.iloc[[0]].assign(dims=""))
    return pd.concat(keep, ignore_index=True) if keep else _empty()


def _instance_facts(
    xml: bytes,
    keep: Callable[[str, str], bool],
    *,
    accn: str,
    form: str,
    filed: str,
) -> pd.DataFrame:
    ns_prefix: dict[str, str] = {}
    root: ET.Element | None = None
    for event, item in ET.iterparse(io.BytesIO(xml), events=("start-ns", "start")):
        if event == "start-ns":
            prefix, uri = item
            ns_prefix.setdefault(uri, prefix)
        elif root is None and isinstance(item, ET.Element):
            root = item
    if root is None:
        return _empty()

    contexts: dict[str, tuple[str | None, str, str]] = {}
    for ctx_el in root.iter(f"{_XBRLI}context"):
        period = ctx_el.find(f"{_XBRLI}period")
        if period is None:
            continue
        start = period.findtext(f"{_XBRLI}startDate")
        end = period.findtext(f"{_XBRLI}endDate") or period.findtext(f"{_XBRLI}instant")
        if end is None:
            continue
        if ctx_el.find(f".//{_XBRLDI}typedMember") is not None:
            continue  # typed dimensions are never selected by config
        members = [
            ((m.get("dimension") or "").strip(), (m.text or "").strip())
            for m in ctx_el.iter(f"{_XBRLDI}explicitMember")
        ]
        contexts[ctx_el.get("id", "")] = (start, end, dims_key(members))

    units: dict[str, str] = {}
    for u in root.iter(f"{_XBRLI}unit"):
        measures = [(m.text or "").split(":")[-1] for m in u.iter(f"{_XBRLI}measure")]
        units[u.get("id", "")] = "/".join(measures)

    rows: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for el in root:
        ctx_ref = el.get("contextRef")
        if ctx_ref is None or el.get(_XSI_NIL) == "true" or not el.tag.startswith("{"):
            continue
        uri, local = el.tag[1:].split("}", 1)
        tag = f"{ns_prefix.get(uri, uri)}:{local}"
        ctx = contexts.get(ctx_ref)
        if ctx is None or not keep(tag, ctx[2]):
            continue
        try:
            val = float((el.text or "").strip())
        except ValueError:
            continue
        unit = units.get(el.get("unitRef", ""), "")
        key = (tag, ctx, unit)
        if key in seen:
            continue  # the same fact can appear more than once in an instance
        seen.add(key)
        taxonomy, name = tag.split(":", 1)
        rows.append(
            {
                "taxonomy": taxonomy,
                "tag": name,
                "dims": ctx[2],
                "unit": unit,
                "val": val,
                "start": ctx[0],
                "end": ctx[1],
                "fy": None,
                "fp": None,
                "form": form,
                "accn": accn,
                "filed": filed,
                "frame": None,
                "source": "instance",
            }
        )
    return pd.DataFrame(rows, columns=FACT_COLUMNS) if rows else _empty()


def text_facts_from_instance(xml: bytes, tags: Iterable[str]) -> dict[str, str]:
    """Non-dimensional text facts (e.g. dei:AuditorName) from an XBRL instance."""
    wanted = set(tags)
    ns_prefix: dict[str, str] = {}
    root: ET.Element | None = None
    for event, item in ET.iterparse(io.BytesIO(xml), events=("start-ns", "start")):
        if event == "start-ns":
            prefix, uri = item
            ns_prefix.setdefault(uri, prefix)
        elif root is None and isinstance(item, ET.Element):
            root = item
    if root is None:
        return {}
    dimensional = {
        ctx.get("id", "")
        for ctx in root.iter(f"{_XBRLI}context")
        if ctx.find(f".//{_XBRLDI}explicitMember") is not None
        or ctx.find(f".//{_XBRLDI}typedMember") is not None
    }
    out: dict[str, str] = {}
    for el in root:
        if not el.tag.startswith("{") or el.get("contextRef") in dimensional:
            continue
        uri, local = el.tag[1:].split("}", 1)
        tag = f"{ns_prefix.get(uri, uri)}:{local}"
        if tag in wanted and (el.text or "").strip():
            out.setdefault(tag, " ".join((el.text or "").split()))
    return out
