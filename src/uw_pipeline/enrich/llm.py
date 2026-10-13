"""LLM settings (config/llm.yaml) and cached, logged LLM calls (`llm_call` table)."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import anthropic
import yaml
from pydantic import BaseModel
from sqlalchemy import Connection, select

from uw_pipeline.clients.llm import LLMClient, LLMError
from uw_pipeline.load.upsert import upsert
from uw_pipeline.schema import llm_call

log = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent / "prompts"


class ModelSpec(BaseModel):
    model: str
    effort: str
    max_tokens: int


class LLMConfig(BaseModel):
    extractor: ModelSpec
    tiebreaker: ModelSpec
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


def load_llm_config(config_dir: Path) -> LLMConfig:
    with (config_dir / "llm.yaml").open(encoding="utf-8") as f:
        return LLMConfig.model_validate(yaml.safe_load(f))


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
