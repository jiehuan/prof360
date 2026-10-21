"""`clinical` stage: the pipeline of clinical-stage companies (Phase 17c, D-058).

Only companies with the `clinical_stage` trait (companies.yaml). Both models read the program
passages of the latest 10-K and of each 10-Q after it, oldest first; `enrich/clinical.py` says
what is kept. A later filing updates a program's phase and milestone. The programs found in this
run are current; earlier ones no longer found are kept with is_current = False (never deleted).
The insight package lists the current ones; they are not published as metrics.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Connection, Engine, select

from uw_pipeline.clients.llm import LLMClient
from uw_pipeline.clients.storage import Storage
from uw_pipeline.config import CompanyConfig
from uw_pipeline.enrich.clinical import Program, agreed, checked, program_key, same_program
from uw_pipeline.enrich.llm import PROMPTS_DIR, LLMConfig, cached_call
from uw_pipeline.enrich.schemas import ClinicalPrograms, strict_json_schema
from uw_pipeline.enrich.text_metrics import select_chunks
from uw_pipeline.enrich.untrusted import data_block
from uw_pipeline.jobs.run_company import TaskResult
from uw_pipeline.jobs.run_legal import _filings
from uw_pipeline.jobs.run_xbrl import company_row
from uw_pipeline.load.upsert import upsert
from uw_pipeline.normalize.html_text import html_to_text
from uw_pipeline.normalize.sections import chunk
from uw_pipeline.schema import clinical_program

log = logging.getLogger(__name__)

TRAIT = "clinical_stage"
PROMPT_VERSION = "clinical_pipeline_v1"
SCHEMA = strict_json_schema(ClinicalPrograms)
MAX_CHARS = 45_000
KEYWORDS = [
    ["phase 3", "phase iii", "pivotal", "registrational"],
    ["phase 2", "phase ii", "phase 1", "phase i", "phase 1/2", "first-in-human"],
    ["topline", "top-line", "data readout", "expect to report", "anticipate", "initiate"],
    ["nda", "bla", "pdufa", "marketing application", "regulatory submission"],
    ["product candidate", "pipeline", "clinical trial", "lead program"],
]  # fmt: skip


class LLMUnavailable(Exception):
    """A model gave no usable answer: nothing is changed, the next run reads the filing again."""


@dataclass
class Stats:
    documents: int = 0
    programs: int = 0  # kept (both models, grounded) across filings
    unpaired: int = 0
    current: int = 0


def read_filing(
    conn: Connection, client: LLMClient, llm: LLMConfig, storage: Storage, doc: Any,
    legal_name: str, now: datetime,
) -> tuple[list[Program], int]:  # fmt: skip
    """Programs both models found in one filing, and the readings left unpaired."""
    chunks = [c.text for c in chunk(html_to_text(storage.read_bytes(doc["raw_path"])))]
    flat = [" ".join(c.split()) for c in chunks]
    idx = select_chunks(flat, KEYWORDS, MAX_CHARS, per_group=3, round_robin=True)
    if not idx:
        return [], 0
    source = "\n".join(chunks[i] for i in idx)
    user = (f"Company: {legal_name}\nDocument: {doc['form_type']} filed {doc['filed_at']}"
            f"\n\n{data_block('excerpt', source)}")  # fmt: skip
    system = (PROMPTS_DIR / f"{PROMPT_VERSION}.md").read_text(encoding="utf-8")
    reads = []
    for spec in (llm.extractor, llm.tiebreaker):
        raw = cached_call(conn, client, spec, task="clinical_pipeline",
                          prompt_version=PROMPT_VERSION, system=system, user=user,
                          schema=SCHEMA, now=now,
                          validate=ClinicalPrograms.model_validate)  # fmt: skip
        if raw is None:
            raise LLMUnavailable(f"{doc['form_type']} filed {doc['filed_at']}")
        reads.append(checked(ClinicalPrograms.model_validate(raw).programs, source))
    return agreed(reads[0], reads[1])


def process_company(
    cfg: CompanyConfig, client: LLMClient, llm: LLMConfig, storage: Storage, conn: Connection,
    now: datetime,
) -> Stats:  # fmt: skip
    row = company_row(conn, cfg)
    company_id = row["company_id"]
    stats = Stats()
    query = select(clinical_program).where(clinical_program.c.company_id == company_id)
    stored = {r["program_key"]: dict(r) for r in conn.execute(query).mappings()}
    model = f"{llm.extractor.model}+{llm.tiebreaker.model}"
    current: dict[str, dict[str, Any]] = {}  # this run's programs, latest filing last
    for doc in _filings(conn, company_id):
        programs, unpaired = read_filing(conn, client, llm, storage, doc, row["legal_name"], now)
        stats.documents += 1
        stats.programs += len(programs)
        stats.unpaired += unpaired
        for p in programs:
            key = next((k for k, r in {**stored, **current}.items() if same_program(p, r)),
                       program_key(p))  # fmt: skip
            current[key] = {
                "company_id": company_id, "program_key": key, "name": p.name,
                "indication": p.indication, "phase": p.phase,
                "next_milestone": p.next_milestone, "milestone_timing": p.milestone_timing,
                "quote": p.quote, "milestone_quote": p.milestone_quote,
                "doc_id": doc["doc_id"], "is_current": True, "model": model,
                "prompt_version": PROMPT_VERSION, "updated_at": now,
            }  # fmt: skip
    gone = [r | {"is_current": False, "updated_at": now} for k, r in stored.items()
            if k not in current and r["is_current"]]  # fmt: skip
    upsert(conn, clinical_program, [*current.values(), *gone],
           key_cols=["company_id", "program_key"])  # fmt: skip
    stats.current = len(current)
    return stats


def run_clinical_stage(
    companies: Sequence[CompanyConfig],
    llm: tuple[LLMClient, LLMConfig] | None,
    storage: Storage,
    engine: Engine,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> list[TaskResult]:
    results = []
    for cfg in companies:
        if TRAIT not in cfg.traits:
            results.append(TaskResult(cfg.ticker, "clinical", ok=True,
                                      detail="not clinical-stage"))  # fmt: skip
            continue
        if llm is None:
            results.append(TaskResult(cfg.ticker, "clinical", ok=False,
                                      detail="LLM not configured"))  # fmt: skip
            continue
        try:
            with engine.begin() as conn:
                stats = process_company(cfg, llm[0], llm[1], storage, conn, now())
            detail = (f"{stats.current} current programs from {stats.documents} filings "
                      f"({stats.unpaired} readings unpaired)")  # fmt: skip
            log.info("%s: clinical %s", cfg.ticker, detail)
            results.append(TaskResult(cfg.ticker, "clinical", ok=True, detail=detail))
        except Exception as exc:
            log.exception("%s: clinical stage failed", cfg.ticker)
            results.append(TaskResult(cfg.ticker, "clinical", ok=False, detail=str(exc)))
    return results
