"""LLM settings (config/llm.yaml) and cached, logged LLM calls (`llm_call` table)."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import anthropic
import yaml
from pydantic import BaseModel
from sqlalchemy import Connection, select

from uw_pipeline.clients.llm import LLMClient, LLMError, LLMSetupError
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


class LLMConfig(BaseModel):
    """llm.yaml with the active provider's profile filled in."""

    provider: str  # anthropic (Claude API) | securegpt (gateway, D-039)
    extractor: ModelSpec
    tiebreaker: ModelSpec
    synthesis: ModelSpec
    pricing: dict[str, tuple[float, float]] = {}
    pricing_currency: str = "USD"
    prompt_version: str
    tiebreak_prompt_version: str
    max_input_chars: int
    topics: dict[str, list[str]]

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


# Context-window or output-size errors concern one request, not the setup (review 2026-10-14).
_REQUEST_TOO_LARGE = ("prompt is too long", "context length", "context_length", "too many tokens",
                      "maximum context", "string_above_max_length")  # fmt: skip


def _store(conn: Connection, row: dict[str, Any]) -> None:
    """Write an llm_call row. On a server database it commits on its own, so a later failure in
    the stage's transaction cannot roll back answers already paid for. (SQLite allows a single
    writer, so there it stays in the caller's transaction.)"""
    if conn.dialect.name == "sqlite":
        upsert(conn, llm_call, [row], key_cols=["input_hash"])
        return
    with conn.engine.begin() as own:
        upsert(own, llm_call, [row], key_cols=["input_hash"])


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
) -> dict[str, Any] | None:
    """Structured LLM call, reused from `llm_call` when the same inputs were seen before.

    Returns None when the model gave no usable output (refusal, truncation, bad JSON, or output
    that fails `validate`, usually the output model's `model_validate`); the error is stored and
    the call is retried on the next run instead of the bad output being reused."""
    key_src = json.dumps(
        [task, prompt_version, spec.model, spec.effort, system, user, schema], sort_keys=True
    )
    input_hash = hashlib.sha256(key_src.encode()).hexdigest()
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
        _store(conn, row | {"output_json": None, "error": "output failed schema validation"})
        return None

    try:
        result = client.complete_json(
            model=spec.model, effort=spec.effort, max_tokens=spec.max_tokens,
            system=system, user=user, schema=schema,
        )  # fmt: skip
    except LLMError as exc:
        log.warning("%s on %s: %s", task, spec.model, exc)
        _store(conn, row | {"error": str(exc)[:500]})
        return None
    except LLMSetupError as exc:
        raise LLMUnavailableError(str(exc)) from exc
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
        raise LLMUnavailableError(str(exc)) from exc
    except anthropic.BadRequestError as exc:
        if any(m in str(exc).lower() for m in _REQUEST_TOO_LARGE):
            log.warning("%s on %s: request too large: %s", task, spec.model, exc)
            _store(conn, row | {"error": f"request too large: {str(exc)[:450]}"})
            return None
        raise LLMUnavailableError(str(exc)) from exc
    usage = {"served_model": result.model, "input_tokens": result.input_tokens,
             "output_tokens": result.output_tokens}  # fmt: skip
    if validate is not None and not _valid(validate, result.output):
        log.warning("%s on %s: output failed schema validation", task, spec.model)
        _store(conn, row | usage | {"output_json": None,
                                    "error": "output failed schema validation"})  # fmt: skip
        return None
    _store(conn, row | usage | {"output_json": json.dumps(result.output), "error": None})
    return result.output


def _valid(validate: Callable[[Any], Any], output: Any) -> bool:
    try:
        validate(output)
    except Exception:  # pydantic ValidationError, or a caller's own check
        return False
    return True
