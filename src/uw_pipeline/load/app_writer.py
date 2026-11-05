"""Writes rows into application tables that have identity keys (Phase 9, decisions D-037).

Each pipeline object has a stable key (e.g. "0000019617:nim:FY:2025:0"). `pipeline_publish_map`
remembers which app row it became, so reruns update that row instead of inserting a new one,
and rows whose content is unchanged (same hash) are not written at all. Plain INSERT/UPDATE
statements, so this works on SQLite (dev) and SQL Server alike.

A writer's company map (keys under its `scope`) is read once per table and kept current as rows
are written (D-084): on Azure SQL every statement is a network round trip, and a lookup per row
was about 2,400 of the 2,900 statements a company's publish made.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import Column, Connection, Table, and_, delete, insert, select, true, update

from uw_pipeline.schema_app import pipeline_publish_map as pmap


def _pk(table: Table) -> Column[Any]:
    return next(iter(table.primary_key.columns))


def _hash(values: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(values, sort_keys=True, default=str).encode()).hexdigest()


class AppWriter:
    def __init__(self, conn: Connection, now: datetime, *, scope: str = "") -> None:
        """`scope`: the key prefix of the objects this writer publishes (one company), so
        existence checks read only that company's rows, not whole tables."""
        self.conn, self.now, self.scope = conn, now, scope
        self.written = 0
        self.unchanged = 0
        self._existing: dict[tuple[str, bool], set[int]] = {}  # app ids present, loaded once
        # The map per (table, scoped): pipeline key -> (app id, row hash), read once. Scoped:
        # this company's keys. Not scoped: shared dimension rows (Company, metrics, categories;
        # each entity at most a few hundred rows), read whole.
        self._map: dict[tuple[str, bool], dict[str, tuple[int, str]]] = {}

    def _scoped(self, key: str) -> bool:
        return bool(self.scope) and key.startswith(self.scope)

    def _known(self, table: Table, key: str) -> tuple[int, str] | None:
        """The app id and row hash `key` was published with, if any."""
        cache = (table.name, self._scoped(key))
        if cache not in self._map:
            q = select(pmap.c.pipeline_key, pmap.c.app_id, pmap.c.row_hash).where(
                pmap.c.entity == table.name
            )
            if cache[1]:
                q = q.where(pmap.c.pipeline_key.startswith(self.scope, autoescape=True))
            self._map[cache] = {k: (int(a), h) for k, a, h in self.conn.execute(q)}
        return self._map[cache].get(key)

    def _remember(self, table: Table, key: str, entry: tuple[int, str] | None) -> None:
        known = self._map.get((table.name, self._scoped(key)))
        if known is None:
            return
        if entry is None:
            known.pop(key, None)
        else:
            known[key] = entry

    def _exists(self, table: Table, app_id: int, key: str) -> bool:
        scoped = self._scoped(key)
        cache = (table.name, scoped)
        if cache not in self._existing:
            ids = select(_pk(table))
            if scoped:  # this company's mapped rows only
                mine = select(pmap.c.app_id).where(
                    pmap.c.entity == table.name,
                    pmap.c.pipeline_key.startswith(self.scope, autoescape=True),
                )
                ids = ids.where(_pk(table).in_(mine))
            self._existing[cache] = set(self.conn.execute(ids).scalars())
        return app_id in self._existing[cache]

    def lookup(self, table: Table, key: str) -> int | None:
        known = self._known(table, key)
        return None if known is None else known[0]

    def put(self, table: Table, key: str, values: Mapping[str, Any]) -> int:
        """Insert or update the row for `key`; return its app id."""
        h = _hash(values)
        known = self._known(table, key)
        pk = _pk(table)
        if known is not None:
            known_id, known_hash = known
            if known_hash == h and self._exists(table, known_id, key):
                self.unchanged += 1
                return known_id
            res = self.conn.execute(update(table).where(pk == known_id).values(**values))
            if res.rowcount:
                self.conn.execute(
                    update(pmap)
                    .where(pmap.c.entity == table.name, pmap.c.pipeline_key == key)
                    .values(row_hash=h, published_at=self.now)
                )
                self._remember(table, key, (known_id, h))
                self.written += 1
                return known_id
            # The app row was removed outside the pipeline: insert it again.
            self.conn.execute(
                delete(pmap).where(pmap.c.entity == table.name, pmap.c.pipeline_key == key)
            )
            self._remember(table, key, None)
        new = self.conn.execute(insert(table).values(**values)).inserted_primary_key
        assert new is not None
        app_id = int(new[0])
        self._existing.setdefault((table.name, self._scoped(key)), set()).add(app_id)
        self.conn.execute(insert(pmap).values(entity=table.name, pipeline_key=key, app_id=app_id,
                                              row_hash=h, published_at=self.now))  # fmt: skip
        self._remember(table, key, (app_id, h))
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
        return self._delete(table, {k: a for k, a in self.keys(table, prefix).items()
                                    if k not in keep})  # fmt: skip

    def drop(self, table: Table, keys: Iterable[str]) -> int:
        """Delete the rows of exactly these keys (objects no longer published)."""
        wanted = list(keys)
        found: dict[str, int] = {}
        for i in range(0, len(wanted), 500):
            rows = self.conn.execute(
                select(pmap.c.pipeline_key, pmap.c.app_id).where(
                    pmap.c.entity == table.name, pmap.c.pipeline_key.in_(wanted[i : i + 500])
                )
            )
            found |= {k: int(a) for k, a in rows}
        return self._delete(table, found)

    def _delete(self, table: Table, stale: Mapping[str, int]) -> int:
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
        for k in keys:
            self._remember(table, k, None)
        return len(stale)

    def deactivate_except(self, table: Table, prefix: str, keep: Iterable[str]) -> int:
        """Set isActive = 0 on this prefix's rows not in `keep` (retired versions are kept).

        Rows retired earlier have an empty row_hash and are not looked at again, so the work
        (and the IN lists) stay small however many old versions accumulate."""
        keep = set(keep)
        active = self.conn.execute(
            select(pmap.c.pipeline_key, pmap.c.app_id).where(
                pmap.c.entity == table.name,
                pmap.c.pipeline_key.startswith(prefix, autoescape=True),
                pmap.c.row_hash != "",
            )
        ).all()
        stale = {k: int(a) for k, a in active if k not in keep}
        changed = 0
        ids, keys = list(stale.values()), list(stale)
        for i in range(0, len(ids), 500):
            res = self.conn.execute(
                update(table).where(_pk(table).in_(ids[i : i + 500]), table.c.isActive == true())
                .values(isActive=False)
            )  # fmt: skip
            changed += int(res.rowcount or 0)
            # Forget the content hash, so a later put of the same key rewrites the row.
            self.conn.execute(
                update(pmap)
                .where(pmap.c.entity == table.name, pmap.c.pipeline_key.in_(keys[i : i + 500]))
                .values(row_hash="")
            )  # fmt: skip
        for k, a in stale.items():
            self._remember(table, k, (a, ""))
        return changed
