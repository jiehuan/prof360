"""Database access for the metric chat: the functions and tables of bff_metric_chat.sql.

`ChatStore` is what the endpoints need; `SqlChatStore` implements it on Azure SQL / SQL Server
with SQLAlchemy Core (the BFF may use its own data layer instead: the SQL is the point). Calls
are synchronous; the endpoints run them in a worker thread.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import Engine, text

from uw_metric_chat import ChatPromptRow, HistoryTurn

TASK = "metric_chat"


@dataclass(frozen=True)
class StoredContext:
    context: str  # the JSON text (udf_GetMetricChatContext's `context`)
    version: int
    updated_at: datetime | None


@dataclass(frozen=True)
class TurnOwner:
    user_id: int
    company_id: int
    metric_id: int


class ChatStore(Protocol):
    def context(self, company_id: int, metric_id: int) -> StoredContext | None: ...

    def prompt(self) -> ChatPromptRow | None: ...

    def history(
        self, conversation_id: uuid.UUID, user_id: int, company_id: int, metric_id: int,
        limit: int,
    ) -> list[HistoryTurn]: ...  # fmt: skip

    def add_turn(self, row: dict[str, Any]) -> int: ...

    def turn_owner(self, turn_id: int) -> TurnOwner | None: ...

    def add_report(self, row: dict[str, Any]) -> int: ...


class SqlChatStore:
    """bff_metric_chat.sql's objects in `schema` (dbo_professional360 by default)."""

    def __init__(self, engine: Engine, schema: str = "dbo_professional360") -> None:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
            raise ValueError(f"not a schema name: {schema!r}")
        self.engine, self.s = engine, schema

    def context(self, company_id: int, metric_id: int) -> StoredContext | None:
        with self.engine.connect() as c:
            row = c.execute(
                text(f"SELECT contextVersion, context, updatedAt "
                     f"FROM {self.s}.udf_GetMetricChatContext(:c, :m)"),
                {"c": company_id, "m": metric_id},
            ).first()  # fmt: skip
        if row is None:
            return None
        return StoredContext(context=row.context, version=int(row.contextVersion),
                             updated_at=row.updatedAt)  # fmt: skip

    def prompt(self) -> ChatPromptRow | None:
        with self.engine.connect() as c:
            row = c.execute(
                text(f"SELECT promptVersion, systemText, outputSchema, contextVersions, "
                     f"minPackageVersion FROM {self.s}.udf_GetActiveChatPrompt(:task)"),
                {"task": TASK},
            ).mappings().first()  # fmt: skip
        return ChatPromptRow.model_validate(dict(row)) if row is not None else None

    def history(
        self, conversation_id: uuid.UUID, user_id: int, company_id: int, metric_id: int,
        limit: int,
    ) -> list[HistoryTurn]:  # fmt: skip
        """The conversation's last turns (oldest first), only the user's own on this metric."""
        with self.engine.connect() as c:
            rows = c.execute(
                text(f"SELECT TOP (:n) Question, Answer FROM {self.s}.UserMetricChat "
                     "WHERE ConversationId = :conv AND UserId = :u AND CompanyId = :c "
                     "AND metric_id = :m ORDER BY TurnId DESC"),
                {"n": limit, "conv": str(conversation_id), "u": user_id, "c": company_id,
                 "m": metric_id},
            ).all()  # fmt: skip
        return [HistoryTurn(question=r.Question, answer=r.Answer or "") for r in reversed(rows)]

    def add_turn(self, row: dict[str, Any]) -> int:
        cols = ", ".join(row)
        values = ", ".join(f":{k}" for k in row)
        params = {k: (str(v) if isinstance(v, uuid.UUID) else v) for k, v in row.items()}
        with self.engine.begin() as c:
            turn_id = c.execute(
                text(f"INSERT INTO {self.s}.UserMetricChat ({cols}) OUTPUT INSERTED.TurnId "
                     f"VALUES ({values})"), params,
            ).scalar_one()  # fmt: skip
        return int(turn_id)

    def turn_owner(self, turn_id: int) -> TurnOwner | None:
        with self.engine.connect() as c:
            row = c.execute(
                text(f"SELECT UserId, CompanyId, metric_id FROM {self.s}.UserMetricChat "
                     "WHERE TurnId = :t"), {"t": turn_id},
            ).first()  # fmt: skip
        return TurnOwner(int(row[0]), int(row[1]), int(row[2])) if row is not None else None

    def add_report(self, row: dict[str, Any]) -> int:
        cols = ", ".join(row)
        values = ", ".join(f":{k}" for k in row)
        with self.engine.begin() as c:
            report_id = c.execute(
                text(f"INSERT INTO {self.s}.MetricIssueReport ({cols}) "
                     f"OUTPUT INSERTED.ReportId VALUES ({values})"), row,
            ).scalar_one()  # fmt: skip
        return int(report_id)
