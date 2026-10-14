"""LLM settings (config/llm.yaml) and cached, logged LLM calls (`llm_call` table)."""

from __future__ import annotations

import hashlib
import json
import logging
import os
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
) -> dict[str, Any] | None:
    """Structured LLM call, reused from `llm_call` when the same inputs were seen before.

    Returns None when the model gave no usable output (refusal, truncation, bad JSON); the
    error is stored and retried on the next run."""
    key_src = json.dumps(
        [task, prompt_version, spec.model, spec.effort, system, user, schema], sort_keys=True
    )
    input_hash = hashlib.sha256(key_src.encode()).hexdigest()
    cached = conn.execute(
        select(llm_call.c.output_json).where(llm_call.c.input_hash == input_hash)
    ).scalar_one_or_none()
    if cached is not None:
        out: dict[str, Any] = json.loads(cached)
        return out

    row: dict[str, Any] = {
        "input_hash": input_hash,
        "task": task,
        "prompt_version": prompt_version,
        "model": spec.model,
        "created_at": now,
    }
    try:
        result = client.complete_json(
            model=spec.model, effort=spec.effort, max_tokens=spec.max_tokens,
            system=system, user=user, schema=schema,
        )  # fmt: skip
    except LLMError as exc:
        log.warning("%s on %s: %s", task, spec.model, exc)
        upsert(conn, llm_call, [row | {"error": str(exc)[:500]}], key_cols=["input_hash"])
        return None
    except LLMSetupError as exc:
        raise LLMUnavailableError(str(exc)) from exc
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
        raise LLMUnavailableError(str(exc)) from exc
    except anthropic.BadRequestError as exc:
        raise LLMUnavailableError(str(exc)) from exc
    upsert(
        conn,
        llm_call,
        [
            row
            | {
                "served_model": result.model,
                "output_json": json.dumps(result.output),
                "error": None,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
            }
        ],
        key_cols=["input_hash"],
    )
    return result.output
