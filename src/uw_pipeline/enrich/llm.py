"""LLM settings (config/llm.yaml) and cached, logged LLM calls (`llm_call` table)."""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import os
import threading
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel
from sqlalchemy import Connection, Engine, select

from uw_pipeline.clients.llm import LLMClient, LLMError, LLMSetupError
from uw_pipeline.clients.llm_limits import ModelLimits
from uw_pipeline.enrich.untrusted import GUARD_PROMPT, neutralize
from uw_pipeline.load.upsert import upsert
from uw_pipeline.schema import llm_call

log = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent / "prompts"


class ModelSpec(BaseModel):
    model: str
    effort: str
    max_tokens: int


class ProviderProfile(BaseModel):
    """The models of one provider (config/llm.yaml `providers`)."""

    extractor: ModelSpec
    tiebreaker: ModelSpec
    synthesis: ModelSpec  # company-level tasks (insights.yaml)
    pricing: dict[str, tuple[float, float]] = {}  # per million tokens (input, output)
    pricing_currency: str = "USD"
    # Phase 15i: per-model requests / tokens per minute shared by parallel workers, and a lower
    # max_tokens per task (providers reserve max_tokens against the token quota).
    rate_limits: dict[str, ModelLimits] = {}
    task_max_tokens: dict[str, int] = {}


class LLMConfig(BaseModel):
    """llm.yaml with the active provider's profile filled in."""

    provider: str  # securegpt (gateway, D-039)
    extractor: ModelSpec
    tiebreaker: ModelSpec
    synthesis: ModelSpec
    pricing: dict[str, tuple[float, float]] = {}
    pricing_currency: str = "USD"
    rate_limits: dict[str, ModelLimits] = {}
    task_max_tokens: dict[str, int] = {}
    prompt_version: str
    tiebreak_prompt_version: str
    max_input_chars: int
    topics: dict[str, list[str]]
    # Model calls of one company asked at a time by `prefetch` (D-062).
    company_calls: int = 1

    def prompt(self) -> str:
        return (PROMPTS_DIR / f"{self.prompt_version}.md").read_text(encoding="utf-8")

    def tiebreak_prompt(self) -> str:
        """Tie-break instructions followed by the extractor's topic definitions."""
        head = (PROMPTS_DIR / f"{self.tiebreak_prompt_version}.md").read_text(encoding="utf-8")
        return f"{head}\n{self.prompt()}"


def active_provider(raw: dict[str, Any], provider: str | None) -> str:
    """`provider` if given, else the LLM_PROVIDER environment variable, else the default."""
    return provider or os.environ.get("LLM_PROVIDER") or str(raw["default_provider"])


def load_llm_config(config_dir: Path, provider: str | None = None) -> LLMConfig:
    with (config_dir / "llm.yaml").open(encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    name = active_provider(raw, provider)
    profiles = raw.get("providers") or {}
    if name not in profiles:
        raise ValueError(f"LLM provider {name!r} is not in config/llm.yaml ({sorted(profiles)})")
    profile = ProviderProfile.model_validate(profiles[name])
    rest = {k: v for k, v in raw.items() if k not in ("default_provider", "providers")}
    return LLMConfig.model_validate({**rest, **profile.model_dump(), "provider": name})


class LLMUnavailableError(Exception):
    """Credentials or request setup are wrong; stop using the LLM for this run."""


def _store(conn: Connection, row: dict[str, Any], commit: bool = False) -> None:
    """Write an llm_call row so that a later failure in the stage cannot roll back answers
    already paid for. `commit`: the caller works in short transactions (D-052), so the row is
    committed on the caller's connection. Otherwise it commits on its own connection on a server
    database; SQLite allows a single writer, so there it stays in the caller's transaction."""
    if commit:
        upsert(conn, llm_call, [row], key_cols=["input_hash"])
        conn.commit()
    elif conn.dialect.name == "sqlite":
        upsert(conn, llm_call, [row], key_cols=["input_hash"])
    else:
        with conn.engine.begin() as own:
            upsert(own, llm_call, [row], key_cols=["input_hash"])


@functools.cache
def _guard() -> str:
    return (PROMPTS_DIR / f"{GUARD_PROMPT}.md").read_text(encoding="utf-8").strip()


class BudgetExceededError(Exception):
    """Today's new LLM calls have reached the daily budget (D-043)."""


class DailyBudget:
    """Spend cap for new LLM calls per UTC day; cached results never count against it.

    Today's spend is read once from `llm_call` (tokens x `pricing`), then kept up to date as
    calls are made. Over the cap, each further call fails as an item (like a refusal): the
    stage records it and goes on, and the next day's run picks the item up again."""

    def __init__(self, limit: float, pricing: dict[str, tuple[float, float]], currency: str):
        self.limit, self.pricing, self.currency = limit, pricing, currency
        self.spent: float | None = None
        self.day: str | None = None
        self._lock = threading.Lock()  # parallel workers share one budget (Phase 15i)

    def cost(self, model: str, input_tokens: int | None, output_tokens: int | None) -> float:
        price_in, price_out = self.pricing.get(model, (0.0, 0.0))
        return ((input_tokens or 0) * price_in + (output_tokens or 0) * price_out) / 1e6

    def check(self, conn: Connection, now: datetime) -> None:
        if self.limit <= 0:
            return
        day = now.astimezone(UTC).strftime("%Y-%m-%d")
        with self._lock:
            self._check(conn, day)

    def _check(self, conn: Connection, day: str) -> None:
        if self.spent is None or self.day != day:
            start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC)
            rows = conn.execute(
                select(llm_call.c.model, llm_call.c.input_tokens, llm_call.c.output_tokens)
                .where(llm_call.c.created_at >= start)
            ).all()  # fmt: skip
            self.spent, self.day = sum(self.cost(*r) for r in rows), day
        if self.spent >= self.limit:
            raise BudgetExceededError(
                f"daily LLM budget of {self.limit:g} {self.currency} reached "
                f"({self.spent:.2f} spent today)"
            )

    def add(self, model: str, input_tokens: int | None, output_tokens: int | None) -> None:
        with self._lock:
            if self.spent is not None:
                self.spent += self.cost(model, input_tokens, output_tokens)


SHORT_TRANSACTIONS = "uw_short_transactions"  # Connection.info key


@contextmanager
def short_transactions(engine: Engine) -> Iterator[Connection]:
    """A connection for one company of a stage in short transactions (D-052, D-061):
    `cached_call` commits the pending writes before each model call and the answer right after,
    and the last group is committed on leaving. An exception rolls back only the unfinished
    group; a rerun picks up from the LLM cache."""
    with engine.connect() as conn:
        conn.info[SHORT_TRANSACTIONS] = True
        try:
            yield conn
            conn.commit()
        finally:
            conn.info.pop(SHORT_TRANSACTIONS, None)  # the DBAPI connection goes back to the pool


def _prepared(
    spec: ModelSpec, task: str, prompt_version: str, system: str, user: str,
    schema: dict[str, Any],
) -> tuple[str, str, str, str]:  # fmt: skip
    """(system, user, prompt version, cache key) as sent and stored."""
    # Source text is data (D-044): invisible characters made visible, and the data rule
    # appended to the system prompt; its version is part of the stored prompt version.
    user = neutralize(user)
    system = f"{system.rstrip()}\n{_guard()}"
    prompt_version = f"{prompt_version}+{GUARD_PROMPT}"
    key_src = json.dumps(
        [task, prompt_version, spec.model, spec.effort, system, user, schema], sort_keys=True
    )
    return system, user, prompt_version, hashlib.sha256(key_src.encode()).hexdigest()


@dataclass(frozen=True)
class Request:
    """A `cached_call` to be made, for `prefetch`."""

    spec: ModelSpec
    task: str
    prompt_version: str
    system: str
    user: str
    schema: dict[str, Any]
    validate: Callable[[Any], Any] | None = None


def prefetch(
    conn: Connection, client: LLMClient, requests: Sequence[Request], *, now: datetime,
    workers: int,
) -> int:  # fmt: skip
    """Ask the model for the requests not answered yet, `workers` at a time, and store the
    answers in `llm_call`, where the `cached_call`s that follow find them (D-062). Only the model
    calls run in threads; the database is used on this thread. A failed call is not stored: the
    call that follows asks again and records the error. Returns the answers stored."""
    todo: dict[str, tuple[Request, str, str, str]] = {}
    for r in requests:
        system, user, version, key = _prepared(r.spec, r.task, r.prompt_version, r.system,
                                               r.user, r.schema)  # fmt: skip
        if key in todo:
            continue
        cached = conn.execute(
            select(llm_call.c.output_json).where(llm_call.c.input_hash == key)
        ).scalar_one_or_none()
        if cached is None:
            todo[key] = (r, system, user, version)
    if workers < 2 or len(todo) < 2:
        return 0  # nothing to overlap: the calls are made one by one as before
    budget: DailyBudget | None = getattr(client, "budget", None)
    if budget is not None:
        try:
            budget.check(conn, now)
        except BudgetExceededError:
            return 0  # the calls that follow report it
    caps: dict[str, int] = getattr(client, "task_max_tokens", None) or {}
    commit = bool(conn.info.get(SHORT_TRANSACTIONS))
    if commit:
        conn.commit()  # nothing stays locked while the calls run

    def ask(item: tuple[Request, str, str, str]) -> Any:
        r, system, user, _ = item
        try:
            return client.complete_json(
                model=r.spec.model, effort=r.spec.effort,
                max_tokens=min(r.spec.max_tokens, caps.get(r.task, r.spec.max_tokens)),
                system=system, user=user, schema=r.schema,
            )  # fmt: skip
        except Exception as exc:  # asked again, and the error recorded, by the call that follows
            log.info("%s on %s: prefetch failed: %s", r.task, r.spec.model, exc)
            return None

    with ThreadPoolExecutor(max_workers=min(workers, len(todo))) as pool:
        answers = list(pool.map(ask, todo.values()))
    stored = 0
    for (key, (r, _, _, version)), result in zip(todo.items(), answers, strict=True):
        if result is None:
            continue
        if budget is not None:
            budget.add(r.spec.model, result.input_tokens, result.output_tokens)
        row: dict[str, Any] = {
            "input_hash": key, "task": r.task, "prompt_version": version, "model": r.spec.model,
            "created_at": now, "served_model": result.model, "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
        }  # fmt: skip
        if r.validate is not None and not _valid(r.validate, result.output):
            # Stored so its tokens count; not cached as an answer, so the call asks again.
            _store(conn, row | {"output_json": None, "error": "output failed schema validation"},
                   commit)  # fmt: skip
            continue
        _store(conn, row | {"output_json": json.dumps(result.output), "error": None}, commit)
        stored += 1
    return stored


def cached_call(
    conn: Connection,
    client: LLMClient,
    spec: ModelSpec,
    *,
    task: str,
    prompt_version: str,
    system: str,
    user: str,
    schema: dict[str, Any],
    now: datetime,
    validate: Callable[[Any], Any] | None = None,
    commit: bool = False,
) -> dict[str, Any] | None:
    """Structured LLM call, reused from `llm_call` when the same inputs were seen before.

    Returns None when the model gave no usable output (refusal, truncation, bad JSON, or output
    that fails `validate`, usually the output model's `model_validate`); the error is stored and
    the call is retried on the next run instead of the bad output being reused.

    `commit` (stages in short transactions, D-052): the caller's pending writes are committed
    before the model is called and the answer right after, so no transaction stays open while
    waiting for the model and other companies can write meanwhile (parallel workers on SQLite).
    A connection opened with `short_transactions` always does so."""
    commit = commit or bool(conn.info.get(SHORT_TRANSACTIONS))
    system, user, prompt_version, input_hash = _prepared(
        spec, task, prompt_version, system, user, schema
    )
    cached = conn.execute(
        select(llm_call.c.output_json).where(llm_call.c.input_hash == input_hash)
    ).scalar_one_or_none()
    row: dict[str, Any] = {
        "input_hash": input_hash,
        "task": task,
        "prompt_version": prompt_version,
        "model": spec.model,
        "created_at": now,
    }
    if cached is not None:
        out: dict[str, Any] = json.loads(cached)
        if validate is None or _valid(validate, out):
            return out
        # Stored before validation was checked here: forget it so the next run asks again.
        _store(conn, row | {"output_json": None, "error": "output failed schema validation"},
               commit)  # fmt: skip
        return None

    budget: DailyBudget | None = getattr(client, "budget", None)
    if budget is not None:
        try:
            budget.check(conn, now)
        except BudgetExceededError as exc:
            log.warning("%s on %s: %s", task, spec.model, exc)
            return None  # not stored: tomorrow's run asks again
    # A task's own cap (Phase 15i) when lower than the model's: max_tokens is not in the cache
    # key, so a cached answer stays valid when the cap changes.
    caps: dict[str, int] = getattr(client, "task_max_tokens", None) or {}
    max_tokens = min(spec.max_tokens, caps.get(task, spec.max_tokens))
    if commit:
        conn.commit()  # the stage's finished writes; nothing stays locked during the call
    try:
        result = client.complete_json(
            model=spec.model, effort=spec.effort, max_tokens=max_tokens,
            system=system, user=user, schema=schema,
        )  # fmt: skip
    except LLMError as exc:
        log.warning("%s on %s: %s", task, spec.model, exc)
        _store(conn, row | {"error": str(exc)[:500]}, commit)
        return None
    except LLMSetupError as exc:
        raise LLMUnavailableError(str(exc)) from exc
    usage = {"served_model": result.model, "input_tokens": result.input_tokens,
             "output_tokens": result.output_tokens}  # fmt: skip
    if budget is not None:
        budget.add(spec.model, result.input_tokens, result.output_tokens)
    if validate is not None and not _valid(validate, result.output):
        log.warning("%s on %s: output failed schema validation", task, spec.model)
        bad = {"output_json": None, "error": "output failed schema validation"}
        _store(conn, row | usage | bad, commit)
        return None
    _store(conn, row | usage | {"output_json": json.dumps(result.output), "error": None}, commit)
    return result.output


def _valid(validate: Callable[[Any], Any], output: Any) -> bool:
    try:
        validate(output)
    except Exception:  # pydantic ValidationError, or a caller's own check
        return False
    return True
