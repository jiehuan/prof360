"""Quarterly insider metrics from Form 4 transactions (Phase 15b, D-050).

- insider_net_sales: officers' and directors' open-market sales (code S) minus purchases
  (code P), in USD, by fiscal quarter of the trade date.
- officer_sell_pct: shares officers sold on the open market in the quarter / their holdings
  before those sales (shares held after their last sale in the quarter + shares sold).

A quarter that lies wholly within the Form 4s read (`covered_from` on) and has no such trades
is 0 for both: every Form 4 since then was read, so none means none. Earlier quarters have no
value.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from typing import Any

PeriodKey = tuple[str, int, int]


def quarter_windows(periods: Mapping[PeriodKey, str]) -> list[tuple[PeriodKey, str, str]]:
    """(key, start exclusive, end inclusive) for each fiscal quarter, oldest first."""
    quarters = sorted((end, key) for key, end in periods.items() if key[0] == "Q")
    out = []
    for i, (end, key) in enumerate(quarters):
        start = (
            quarters[i - 1][0] if i else (date.fromisoformat(end) - timedelta(days=91)).isoformat()
        )
        out.append((key, start, end))
    return out


def insider_metrics(
    rows: Sequence[Mapping[str, Any]], periods: Mapping[PeriodKey, str],
    covered_from: str | None = None,
) -> dict[str, dict[PeriodKey, tuple[float, str]]]:  # fmt: skip
    """metric code -> fiscal quarter -> (value, period end)."""
    out: dict[str, dict[PeriodKey, tuple[float, str]]] = defaultdict(dict)
    for key, start, end in quarter_windows(periods):
        trades = [r for r in rows if r["trade_date"] and start < r["trade_date"] <= end
                  and (r["is_officer"] or r["is_director"]) and r["code"] in ("S", "P")
                  and r["shares"]]  # fmt: skip
        covered = covered_from is not None and start >= covered_from
        if not trades:
            if covered:
                out["insider_net_sales"][key] = (0.0, end)
                out["officer_sell_pct"][key] = (0.0, end)
            continue
        net = sum((1 if r["code"] == "S" else -1) * r["shares"] * (r["price"] or 0) for r in trades)
        out["insider_net_sales"][key] = (net, end)
        sold: dict[str, float] = defaultdict(float)
        after: dict[str, tuple[str, float]] = {}
        for r in sorted(trades, key=lambda r: (r["trade_date"], r["accession_no"], r["line"])):
            if r["code"] == "S" and r["is_officer"]:
                owner = r["owner_cik"] or r["owner_name"] or ""
                sold[owner] += r["shares"]
                after[owner] = (r["trade_date"], r["shares_after"] or 0.0)
        before = sum(sold[o] + after[o][1] for o in sold)
        if before > 0:
            out["officer_sell_pct"][key] = (sum(sold.values()) / before, end)
        elif covered:
            out["officer_sell_pct"][key] = (0.0, end)  # trades, but no officer sold
    return out
