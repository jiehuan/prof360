"""Writes rows into application tables that have identity keys (Phase 9, decisions D-037).

Each pipeline object has a stable key (e.g. "0000019617:nim:FY:2025:0"). `pipeline_publish_map`
remembers which app row it became, so reruns update that row instead of inserting a new one,
and rows whose content is unchanged (same hash) are not written at all. Plain INSERT/UPDATE
statements, so this works on SQLite (dev) and SQL Server alike.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import Column, Connection, Table, and_, delete, insert, select, update

from uw_pipeline.schema_app import pipeline_publish_map as pmap


def _pk(table: Table) -> Column[Any]:
    return next(iter(table.primary_key.columns))


def _hash(values: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(values, sort_keys=True, default=str).encode()).hexdigest()


class AppWriter:
    def __init__(self, conn: Connection, now: datetime) -> None:
        self.conn, self.now = conn, now
        self.written = 0
        self.unchanged = 0
        self._existing: dict[str, set[int]] = {}  # app ids present, loaded once per table

    def _exists(self, table: Table, app_id: int) -> bool:
        if table.name not in self._existing:
            self._existing[table.name] = set(self.conn.execute(select(_pk(table))).scalars())
        return app_id in self._existing[table.name]

    def lookup(self, table: Table, key: str) -> int | None:
        return self.conn.execute(
            select(pmap.c.app_id).where(pmap.c.entity == table.name, pmap.c.pipeline_key == key)
        ).scalar()

    def put(self, table: Table, key: str, values: Mapping[str, Any]) -> int:
        """Insert or update the row for `key`; return its app id."""
        h = _hash(values)
        known = self.conn.execute(
            select(pmap.c.app_id, pmap.c.row_hash)
            .where(pmap.c.entity == table.name, pmap.c.pipeline_key == key)
        ).first()  # fmt: skip
        pk = _pk(table)
        if known is not None:
            if known.row_hash == h and self._exists(table, int(known.app_id)):
                self.unchanged += 1
                return int(known.app_id)
            res = self.conn.execute(update(table).where(pk == known.app_id).values(**values))
            if res.rowcount:
                self.conn.execute(
                    update(pmap)
                    .where(pmap.c.entity == table.name, pmap.c.pipeline_key == key)
                    .values(row_hash=h, published_at=self.now)
                )
                self.written += 1
                return int(known.app_id)
            # The app row was removed outside the pipeline: insert it again.
            self.conn.execute(
                delete(pmap).where(pmap.c.entity == table.name, pmap.c.pipeline_key == key)
            )
        new = self.conn.execute(insert(table).values(**values)).inserted_primary_key
        assert new is not None
        app_id = int(new[0])
        self._existing.setdefault(table.name, set()).add(app_id)
        self.conn.execute(insert(pmap).values(entity=table.name, pipeline_key=key, app_id=app_id,
                                              row_hash=h, published_at=self.now))  # fmt: skip
        self.written += 1
        return app_id

    def keys(self, table: Table, prefix: str) -> dict[str, int]:
        rows = self.conn.execute(
            select(pmap.c.pipeline_key, pmap.c.app_id).where(
                pmap.c.entity == table.name, pmap.c.pipeline_key.startswith(prefix, autoescape=True)
            )
        )
        return {k: int(a) for k, a in rows}

    def drop_except(self, table: Table, prefix: str, keep: Iterable[str]) -> int:
        """Delete this prefix's rows whose key is not in `keep` (stale objects)."""
        keep = set(keep)
        stale = {k: a for k, a in self.keys(table, prefix).items() if k not in keep}
        if not stale:
            return 0
        ids = list(stale.values())
        for i in range(0, len(ids), 500):
            self.conn.execute(delete(table).where(_pk(table).in_(ids[i : i + 500])))
        keys = list(stale)
        for i in range(0, len(keys), 500):
            self.conn.execute(
                delete(pmap).where(
                    and_(pmap.c.entity == table.name, pmap.c.pipeline_key.in_(keys[i : i + 500]))
                )
            )
        return len(stale)

    def deactivate_except(self, table: Table, prefix: str, keep: Iterable[str]) -> int:
        """Set isActive = 0 on this prefix's rows not in `keep` (retired versions are kept)."""
        keep = set(keep)
        stale = {k: a for k, a in self.keys(table, prefix).items() if k not in keep}
        if not stale:
            return 0
        res = self.conn.execute(
            update(table).where(_pk(table).in_(list(stale.values())), table.c.isActive.is_(True))
            .values(isActive=False)
        )  # fmt: skip
        # Forget the content hash, so a later put of the same key rewrites the row.
        self.conn.execute(
            update(pmap)
            .where(pmap.c.entity == table.name, pmap.c.pipeline_key.in_(list(stale)))
            .values(row_hash="")
        )  # fmt: skip
        return int(res.rowcount or 0)
