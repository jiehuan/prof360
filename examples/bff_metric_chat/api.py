"""The metric chat endpoints (FastAPI router).

    GET  api/companies/{companyId}/metrics/{metricId}/chat/suggestions   three starter questions
    POST api/companies/{companyId}/metrics/{metricId}/chat               ask a question
    POST api/companies/{companyId}/metrics/{metricId}/chat/{turnId}/report
                                                                         "This looks wrong"

The BFF includes `router` in its app and supplies three things through `app.state` (see
main.py): `chat_store` (a ChatStore), `chat_client` (AsyncSecureGPT) and `chat_settings`; and
replaces the `current_user` dependency with its own authentication.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from collections import defaultdict, deque
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from uw_metric_chat import CANNOT_CONFIRM, ChatReply, IncompatibleError, answer_question
from uw_metric_chat.securegpt import DEFAULT_MODEL

from .store import ChatStore

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/companies/{company_id}/metrics/{metric_id}/chat",
                   tags=["metric chat"])  # fmt: skip
PERIOD = re.compile(r"^(FY\d{4}|\d{4}Q[1-4])$")


# -- settings, user, rate limit ------------------------------------------------------------------


class ChatSettings(BaseModel):
    model: str = DEFAULT_MODEL  # or "gpt-6-sol-2026-09-22"
    history_turns: int = 6
    questions_per_hour: int = 30  # per user, per process (use a shared store with several pods)


class User(BaseModel):
    id: int


def current_user() -> User:
    """Replace with the BFF's authentication: app.dependency_overrides[current_user] = ..."""
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "authentication is not wired")


class RateLimiter:
    """At most `limit` questions per user in any hour (in memory, per process)."""

    def __init__(self, limit: int, window_sec: float = 3600.0, clock: Any = time.monotonic):
        self.limit, self.window, self.clock = limit, window_sec, clock
        self._seen: dict[int, deque[float]] = defaultdict(deque)

    def retry_after(self, user_id: int) -> int | None:
        """None when the user may ask now (and counts the question), else seconds to wait."""
        now, seen = self.clock(), self._seen[user_id]
        while seen and now - seen[0] >= self.window:
            seen.popleft()
        if len(seen) >= self.limit:
            return int(self.window - (now - seen[0])) + 1
        seen.append(now)
        return None


def _deps(request: Request) -> tuple[ChatStore, Any, ChatSettings, RateLimiter]:
    s = request.app.state
    return s.chat_store, s.chat_client, s.chat_settings, s.chat_limiter


# -- request and response bodies (camelCase, as the front end's API list) ------------------------


class _Camel(BaseModel):
    model_config = ConfigDict(populate_by_name=True)


class ChatRequest(_Camel):
    conversation_id: uuid.UUID | None = Field(default=None, alias="conversationId")
    message: str = Field(min_length=1, max_length=1000)


class CitationOut(_Camel):
    id: str
    label: str
    value: float | None
    unit: str
    status: str
    period: str  # FY2025 | 2026Q3: the period in the popup to highlight


class ChatResponse(_Camel):
    conversation_id: uuid.UUID = Field(alias="conversationId")
    turn_id: int = Field(alias="turnId")
    # answered | out_of_scope | cannot_confirm (the answer failed the number check) |
    # unavailable (SecureGPT failed; try again later)
    status: Literal["answered", "out_of_scope", "cannot_confirm", "unavailable"]
    answer: str
    citations: list[CitationOut]
    possible_issue: bool = Field(alias="possibleIssue")
    issue_summary: str | None = Field(alias="issueSummary")
    period: str | None


class ReportRequest(_Camel):
    comment: str | None = Field(default=None, max_length=2000)
    period: str | None = Field(default=None, pattern=PERIOD.pattern)


class ReportResponse(_Camel):
    report_id: int = Field(alias="reportId")


class Suggestions(_Camel):
    questions: list[str]


def reply_status(reply: ChatReply) -> str:
    if reply.grounding == "ok":
        return "answered" if reply.in_scope else "out_of_scope"
    return "cannot_confirm" if reply.answer == CANNOT_CONFIRM else "unavailable"


def suggested_questions(ctx: dict[str, Any]) -> list[str]:
    """Three starter questions; the second names an N/M period when the popup shows one."""
    name = (ctx.get("metric") or {}).get("name") or "this metric"
    nm = next((p for p in ctx.get("periods") or [] if p.get("status") == "NM"), None)
    second = (f"Why is {name} N/M for {nm.get('label') or nm.get('period')}?" if nm
              else "Why does the latest period differ from earlier ones?")  # fmt: skip
    return [f"How is {name} calculated?", second, "Where does this number come from?"]


# -- endpoints -----------------------------------------------------------------------------------

CompanyId = Annotated[int, Path(ge=1)]
MetricId = Annotated[int, Path(ge=1)]


@router.get("/suggestions", response_model=Suggestions, response_model_by_alias=True)
async def suggestions(
    company_id: CompanyId, metric_id: MetricId, request: Request,
    user: Annotated[User, Depends(current_user)],
) -> Suggestions:  # fmt: skip
    store, _, _, _ = _deps(request)
    stored = await run_in_threadpool(store.context, company_id, metric_id)
    if stored is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No chat for this metric")
    return Suggestions(questions=suggested_questions(json.loads(stored.context)))


@router.post("", response_model=ChatResponse, response_model_by_alias=True)
async def ask(
    company_id: CompanyId, metric_id: MetricId, body: ChatRequest, request: Request,
    user: Annotated[User, Depends(current_user)],
) -> ChatResponse:  # fmt: skip
    store, client, settings, limiter = _deps(request)
    wait = limiter.retry_after(user.id)
    if wait is not None:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many questions; try later",
                            headers={"Retry-After": str(wait)})  # fmt: skip
    stored = await run_in_threadpool(store.context, company_id, metric_id)
    if stored is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No chat for this metric")
    prompt = await run_in_threadpool(store.prompt)
    if prompt is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Chat is not set up yet")
    conversation = body.conversation_id or uuid.uuid4()
    history = await run_in_threadpool(store.history, conversation, user.id, company_id,
                                      metric_id, settings.history_turns)  # fmt: skip
    try:
        reply = await answer_question(client, prompt, stored.context, history, body.message,
                                      model=settings.model)  # fmt: skip
    except IncompatibleError as exc:
        # The database holds a prompt or context this package cannot read: the pipeline team
        # has published a newer version; copy in the new uw_metric_chat folder.
        log.error("metric chat: %s", exc)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Chat is being updated") from exc
    turn_id = await run_in_threadpool(store.add_turn, {
        "ConversationId": conversation, "UserId": user.id, "CompanyId": company_id,
        "metric_id": metric_id, "Question": body.message[:2000],
        "ContextUpdatedAt": stored.updated_at, **reply.log_row(),
    })  # fmt: skip
    if reply.possible_issue and reply.grounding == "ok":
        await run_in_threadpool(store.add_report, {
            "UserId": user.id, "CompanyId": company_id, "metric_id": metric_id,
            "TurnId": turn_id, "Source": "MODEL", "Period": reply.period,
            "IssueSummary": reply.issue_summary,
        })  # fmt: skip
    log.info("metric chat turn %s: %s, %s ms", turn_id, reply.grounding, reply.latency_ms)
    return ChatResponse(
        conversation_id=conversation, turn_id=turn_id, status=reply_status(reply),
        answer=reply.answer,
        citations=[CitationOut(**c.model_dump()) for c in reply.citations],
        possible_issue=reply.possible_issue, issue_summary=reply.issue_summary,
        period=reply.period,
    )  # fmt: skip


@router.post("/{turn_id}/report", response_model=ReportResponse, response_model_by_alias=True,
             status_code=status.HTTP_201_CREATED)  # fmt: skip
async def report(
    company_id: CompanyId, metric_id: MetricId, turn_id: Annotated[int, Path(ge=1)],
    body: ReportRequest, request: Request, user: Annotated[User, Depends(current_user)],
) -> ReportResponse:  # fmt: skip
    store, _, _, _ = _deps(request)
    owner = await run_in_threadpool(store.turn_owner, turn_id)
    if owner is None or (owner.user_id, owner.company_id, owner.metric_id) != (
            user.id, company_id, metric_id):  # fmt: skip
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such answer")
    report_id = await run_in_threadpool(store.add_report, {
        "UserId": user.id, "CompanyId": company_id, "metric_id": metric_id, "TurnId": turn_id,
        "Source": "USER", "Period": body.period, "Comment": body.comment,
    })  # fmt: skip
    return ReportResponse(report_id=report_id)
