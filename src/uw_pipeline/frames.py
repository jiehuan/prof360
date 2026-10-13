"""Small pandas helpers."""

from __future__ import annotations

from typing import Any, cast

import pandas as pd


def records(df: pd.DataFrame) -> list[dict[str, Any]]:
    """Rows as dicts with str keys (pandas types them as Hashable)."""
    return cast(list[dict[str, Any]], df.to_dict("records"))
