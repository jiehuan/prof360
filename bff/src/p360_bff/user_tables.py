"""User-state tables of the team's data model diagram, owned by the application (the pipeline
never writes them), and every query on them (D-048).

The diagram names these tables (Users, UserHistory, UserFavority, UserSIC, UserMetricsSelection,
UserPinnedCompanies, UserFlaggedFollowupQuestions); their columns were not available, so the
columns below are inferred from the names. TO CONFIRM against the team's DDL: when it arrives,
only this module changes. vw_Notification (a view) is not built yet.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import (
    Column,
    Connection,
    DateTime,
    Index,
    Integer,
    MetaData,
    Table,
    Unicode,
    delete,
    func,
    insert,
    select,
    update,
)

user_metadata = MetaData()

Users = Table(
    "Users",
    user_metadata,
    Column("UserId", Integer, primary_key=True, autoincrement=True),
    Column("Email", Unicode(320), nullable=False, unique=True),
    Column("DisplayName", Unicode(200)),
    Column("CreatedAt", DateTime, nullable=False),
    Column("LastSeenAt", DateTime, nullable=False),
)

UserHistory = Table(
    "UserHistory",
    user_metadata,
    Column("UserHistoryId", Integer, primary_key=True, autoincrement=True),
    Column("UserId", Integer, nullable=False),
    Column("CompanyId", Integer, nullable=False),
    Column("ViewedAt", DateTime, nullable=False),
    Index("ix_UserHistory_user_viewed", "UserId", "ViewedAt"),
)

# Spelled as in the diagram.
UserFavority = Table(
    "UserFavority",
    user_metadata,
    Column("UserId", Integer, primary_key=True),
    Column("CompanyId", Integer, primary_key=True),
    Column("CreatedAt", DateTime, nullable=False),
)

UserPinnedCompanies = Table(
    "UserPinnedCompanies",
    user_metadata,
    Column("UserId", Integer, primary_key=True),
    Column("CompanyId", Integer, primary_key=True),
    Column("PinnedAt", DateTime, nullable=False),
)

UserFlaggedFollowupQuestions = Table(
    "UserFlaggedFollowupQuestions",
    user_metadata,
    Column("UserId", Integer, primary_key=True),
    Column("FollowUpQuestionId", Integer, primary_key=True),
    Column("FlaggedAt", DateTime, nullable=False),
)

# Industries the user follows (Save Preferences).
UserSIC = Table(
    "UserSIC",
    user_metadata,
    Column("UserId", Integer, primary_key=True),
    Column("SIC_Id", Integer, primary_key=True),
)

# Metrics the user chose to see for an industry; none = the SIC_Preferences default.
UserMetricsSelection = Table(
    "UserMetricsSelection",
    user_metadata,
    Column("UserId", Integer, primary_key=True),
    Column("SIC_Id", Integer, primary_key=True),
    Column("metric_id", Integer, primary_key=True),
)

# Company sets: favorites and pinned companies share the same queries.
COMPANY_SETS = {
    "favorites": (UserFavority, "CreatedAt"),
    "pinned": (UserPinnedCompanies, "PinnedAt"),
}


@dataclass(frozen=True)
class User:
    user_id: int
    email: str
    display_name: str | None


def ensure_user(conn: Connection, email: str, now: datetime) -> User:
    """The user with this e-mail, created on first sight; LastSeenAt updated."""
    email = email.strip().lower()
    row = conn.execute(select(Users).where(Users.c.Email == email)).mappings().first()
    if row is None:
        conn.execute(insert(Users).values(Email=email, CreatedAt=now, LastSeenAt=now))
        row = conn.execute(select(Users).where(Users.c.Email == email)).mappings().one()
    else:
        conn.execute(update(Users).where(Users.c.UserId == row["UserId"]).values(LastSeenAt=now))
    return User(row["UserId"], row["Email"], row["DisplayName"])


def record_view(conn: Connection, user_id: int, company_id: int, now: datetime) -> None:
    conn.execute(insert(UserHistory).values(UserId=user_id, CompanyId=company_id, ViewedAt=now))


def recent(conn: Connection, user_id: int, limit: int) -> list[tuple[int, datetime]]:
    """Companies by the user's latest view, newest first."""
    last = func.max(UserHistory.c.ViewedAt)
    rows = conn.execute(
        select(UserHistory.c.CompanyId, last.label("viewed"))
        .where(UserHistory.c.UserId == user_id)
        .group_by(UserHistory.c.CompanyId)
        .order_by(last.desc())
        .limit(limit)
    ).all()
    return [(r[0], r[1]) for r in rows]


def company_set(conn: Connection, kind: str, user_id: int) -> list[int]:
    table, at = COMPANY_SETS[kind]
    rows = conn.execute(
        select(table.c.CompanyId).where(table.c.UserId == user_id).order_by(table.c[at])
    ).all()
    return [r[0] for r in rows]


def set_company(
    conn: Connection, kind: str, user_id: int, company_id: int, on: bool, now: datetime
) -> None:
    table, at = COMPANY_SETS[kind]
    key = (table.c.UserId == user_id) & (table.c.CompanyId == company_id)
    exists = conn.execute(select(table.c.UserId).where(key)).first() is not None
    if on and not exists:
        conn.execute(insert(table).values(UserId=user_id, CompanyId=company_id, **{at: now}))
    elif not on and exists:
        conn.execute(delete(table).where(key))


def flagged_questions(conn: Connection, user_id: int) -> set[int]:
    t = UserFlaggedFollowupQuestions
    return {r[0] for r in conn.execute(select(t.c.FollowUpQuestionId).where(t.c.UserId == user_id))}


def set_flag(conn: Connection, user_id: int, question_id: int, on: bool, now: datetime) -> None:
    t = UserFlaggedFollowupQuestions
    key = (t.c.UserId == user_id) & (t.c.FollowUpQuestionId == question_id)
    exists = conn.execute(select(t.c.UserId).where(key)).first() is not None
    if on and not exists:
        conn.execute(
            insert(t).values(UserId=user_id, FollowUpQuestionId=question_id, FlaggedAt=now)
        )
    elif not on and exists:
        conn.execute(delete(t).where(key))


def followed_sics(conn: Connection, user_id: int) -> list[int]:
    rows = conn.execute(
        select(UserSIC.c.SIC_Id).where(UserSIC.c.UserId == user_id).order_by(UserSIC.c.SIC_Id)
    ).all()
    return [r[0] for r in rows]


def set_followed_sics(conn: Connection, user_id: int, sic_ids: list[int]) -> None:
    conn.execute(delete(UserSIC).where(UserSIC.c.UserId == user_id))
    if sic_ids:
        conn.execute(insert(UserSIC), [{"UserId": user_id, "SIC_Id": s} for s in set(sic_ids)])


def metric_selection(
    conn: Connection, user_id: int, sic_id: int | None = None
) -> dict[int, list[int]]:
    """Chosen metric ids per industry (only industries with a choice)."""
    t = UserMetricsSelection
    q = select(t.c.SIC_Id, t.c.metric_id).where(t.c.UserId == user_id)
    if sic_id is not None:
        q = q.where(t.c.SIC_Id == sic_id)
    out: dict[int, list[int]] = {}
    for sic, metric in conn.execute(q.order_by(t.c.SIC_Id, t.c.metric_id)).all():
        out.setdefault(sic, []).append(metric)
    return out


def set_metric_selection(
    conn: Connection, user_id: int, sic_id: int, metric_ids: list[int]
) -> None:
    """Replace the user's choice for an industry; an empty list goes back to the default."""
    t = UserMetricsSelection
    conn.execute(delete(t).where((t.c.UserId == user_id) & (t.c.SIC_Id == sic_id)))
    if metric_ids:
        conn.execute(
            insert(t),
            [{"UserId": user_id, "SIC_Id": sic_id, "metric_id": m} for m in set(metric_ids)],
        )
