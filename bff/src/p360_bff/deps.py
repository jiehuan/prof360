"""Per-request dependencies: a database connection (one transaction per request) and the user."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import Connection

from p360_bff import content
from p360_bff.user_tables import User, ensure_user


def get_conn(request: Request) -> Iterator[Connection]:
    with request.app.state.engine.begin() as conn:
        yield conn


Conn = Annotated[Connection, Depends(get_conn)]


def now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)  # stored as UTC, without time zone


def current_user(
    request: Request, conn: Conn, x_user_email: Annotated[str | None, Header()] = None
) -> User:
    """Development mode (D-048): the X-User-Email header, else the configured dev user. The
    corporate SSO token check replaces this once the app registration exists."""
    email = x_user_email or request.app.state.settings.bff_dev_user_email
    if "@" not in email:
        raise HTTPException(400, "X-User-Email must be an e-mail address")
    return ensure_user(conn, email, now())


CurrentUser = Annotated[User, Depends(current_user)]


def company_or_404(conn: Connection, company_id: int) -> object:
    row = content.company_row(conn, company_id)
    if row is None:
        raise HTTPException(404, f"company {company_id} not found")
    return row
