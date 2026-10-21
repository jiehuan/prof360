"""Runs one (stage, company) task: owns the clients and configuration the stages need."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import datetime
from types import TracebackType
from typing import Any

from sqlalchemy import Engine, func, select

from uw_pipeline.clients.alphavantage import AlphaVantageClient
from uw_pipeline.clients.db import app_engine, ensure_schema
from uw_pipeline.clients.llm import ClaudeClient, LLMClient
from uw_pipeline.clients.llm_limits import LimitedClient
from uw_pipeline.clients.ratelimit import TokenBucket
from uw_pipeline.clients.runware import RunwareClient, load_runware_config
from uw_pipeline.clients.sec import SecClient
from uw_pipeline.clients.securegpt import SecureGPTClient, load_securegpt_config
from uw_pipeline.clients.storage import make_storage
from uw_pipeline.compute.registry import load_registry
from uw_pipeline.config import CompanyConfig, load_companies, load_industry_config
from uw_pipeline.enrich.llm import DailyBudget, LLMConfig, load_llm_config
from uw_pipeline.enrich.package import load_insights_config
from uw_pipeline.enrich.sources import load_source_labels
from uw_pipeline.enrich.text_metrics import load_text_config
from uw_pipeline.jobs.run_clinical import run_clinical_stage
from uw_pipeline.jobs.run_company import TaskResult, run_company_stage
from uw_pipeline.jobs.run_documents import run_documents_stage
from uw_pipeline.jobs.run_dq import run_dq_stage
from uw_pipeline.jobs.run_governance import run_governance_stage
from uw_pipeline.jobs.run_insights import run_insights_stage
from uw_pipeline.jobs.run_legal import run_legal_stage
from uw_pipeline.jobs.run_market import run_market_stage
from uw_pipeline.jobs.run_metric_detail import run_metric_detail_stage
from uw_pipeline.jobs.run_news import load_news_config, run_news_stage
from uw_pipeline.jobs.run_publish import load_publish_config, run_publish_stage
from uw_pipeline.jobs.run_scores import load_scores_config, run_scores_stage
from uw_pipeline.jobs.run_signals import run_signals_stage
from uw_pipeline.jobs.run_text_metrics import run_text_metrics_stage
from uw_pipeline.jobs.run_transcripts import run_transcripts_stage
from uw_pipeline.jobs.run_xbrl import run_compute_stage, run_facts_stage
from uw_pipeline.quality.dq import load_dq_config
from uw_pipeline.schema import llm_call
from uw_pipeline.schema_app import app_metadata
from uw_pipeline.settings import Settings
from uw_pipeline.signals.rules import load_signal_rules

log = logging.getLogger(__name__)


class StageRunner:
    """Callable as runner(stage, ticker) -> TaskResult; use as a context manager."""

    def __init__(
        self, settings: Settings, engine: Engine, *, force: bool = False, refresh_sec: bool = False
    ) -> None:
        self.settings = settings
        self.engine = engine
        self.force = force
        self.refresh_sec = refresh_sec
        self.run_id: int | None = None  # set by the CLI once the run exists (dq findings)
        cfg_dir = settings.config_dir
        self.companies = {c.ticker: c for c in load_companies(cfg_dir)}
        self.industry = load_industry_config(cfg_dir)
        self.industry.check_companies(list(self.companies.values()))
        self.registry = load_registry(cfg_dir)
        self.registry.check_groups(self.industry.groups.values())
        self.signal_rules = load_signal_rules(cfg_dir)
        self.signal_rules.check_groups(self.industry.groups.values())
        self.storage = make_storage(settings)
        self._sec: SecClient | None = None
        self._av: AlphaVantageClient | None = None
        self._av_calls_at_start: int | None = None
        self._llm: tuple[LLMClient, LLMConfig] | None = None
        self._llm_built = False
        self._app_engine: Engine | None = None
        # Parallel tasks of one stage (Phase 15i) share these clients: build each only once.
        self._lock = threading.RLock()

    # -- lazily built clients --------------------------------------------------------------

    @property
    def sec(self) -> SecClient:
        with self._lock:
            return self._sec_client()

    def _sec_client(self) -> SecClient:
        if self._sec is None:
            s = self.settings
            self._sec = SecClient(
                s.sec_user_agent, self.storage,
                rate_limiter=TokenBucket(rate=s.sec_rate_limit_per_sec),
                timeout=s.http_timeout_sec, max_attempts=s.sec_max_attempts,
                refresh=self.refresh_sec,
            )  # fmt: skip
        return self._sec

    @property
    def av(self) -> AlphaVantageClient | None:
        with self._lock:
            return self._av_client()

    def _av_client(self) -> AlphaVantageClient | None:
        s = self.settings
        if self._av is None and s.alphavantage_api_key is not None:
            self._av = AlphaVantageClient(
                s.alphavantage_api_key.get_secret_value(), self.storage,
                rate_limiter=TokenBucket(rate=s.alphavantage_calls_per_minute / 60),
                daily_quota=s.alphavantage_daily_quota or None,
            )  # fmt: skip
            self._av_calls_at_start = self._av.calls_today()
        return self._av

    @property
    def llm(self) -> tuple[LLMClient, LLMConfig] | None:
        with self._lock:
            return self._llm_client()

    def _llm_client(self) -> tuple[LLMClient, LLMConfig] | None:
        if not self._llm_built:
            self._llm_built = True
            s = self.settings
            cfg = load_llm_config(s.config_dir, s.llm_provider)
            client: LLMClient | None = None
            if not s.llm_enabled:
                pass
            elif cfg.provider == "securegpt":
                if s.securegpt_client_id and s.securegpt_client_secret and \
                        s.securegpt_subscription_key:  # fmt: skip
                    gcfg = load_securegpt_config(s.config_dir)
                    client = SecureGPTClient(
                        gcfg, env=s.securegpt_env or gcfg.default_env,
                        client_id=s.securegpt_client_id,
                        client_secret=s.securegpt_client_secret.get_secret_value(),
                        subscription_key=s.securegpt_subscription_key.get_secret_value(),
                    )  # fmt: skip
            elif cfg.provider.startswith("runware"):
                if s.runware_api_key is not None:
                    key = s.runware_api_key.get_secret_value()
                    client = RunwareClient(load_runware_config(s.config_dir), api_key=key)
            elif s.anthropic_api_key is not None:
                client = ClaudeClient(
                    s.anthropic_api_key.get_secret_value(), workspace_id=s.anthropic_workspace_id,
                    timeout=s.anthropic_timeout_sec,
                )  # fmt: skip
            if client is not None:
                # Per-model RPM / TPM limits shared by parallel workers (Phase 15i).
                if cfg.rate_limits:
                    client = LimitedClient(client, cfg.rate_limits)
                # Daily spend cap in the profile's currency (D-043); 0 = unlimited.
                unpriced = sorted(
                    {m.model for m in (cfg.extractor, cfg.tiebreaker, cfg.synthesis)}
                    - cfg.pricing.keys()
                )
                if unpriced and s.llm_daily_budget > 0:
                    log.warning("no price for %s in config/llm.yaml: the daily LLM budget does "
                                "not count their calls", ", ".join(unpriced))  # fmt: skip
                client.budget = DailyBudget(s.llm_daily_budget, cfg.pricing,  # type: ignore[attr-defined]
                                            cfg.pricing_currency)  # fmt: skip
                client.task_max_tokens = cfg.task_max_tokens  # type: ignore[attr-defined]
                self._llm = (client, cfg)
        return self._llm

    @property
    def app_engine(self) -> Engine:
        with self._lock:
            return self._app_db()

    def _app_db(self) -> Engine:
        if self._app_engine is None:
            self._app_engine = app_engine(self.settings)
            if self._app_engine.dialect.name == "sqlite":  # dev stand-in for the app database
                ensure_schema(self._app_engine, app_metadata)
        return self._app_engine

    # -- running ---------------------------------------------------------------------------

    def __call__(self, stage: str, ticker: str) -> TaskResult:
        cfg = self.companies[ticker]
        results = self._stage(stage)([cfg])
        return results[0]

    def _stage(self, stage: str) -> Callable[[list[CompanyConfig]], list[TaskResult]]:
        s, e, f = self.settings, self.engine, self.force
        reg, ind = self.registry, self.industry
        if stage == "company":
            return lambda c: run_company_stage(c, self.sec, ind, e, force=f)
        if stage == "facts":
            return lambda c: run_facts_stage(
                c, self.sec, reg, ind, e, history_years=s.xbrl_history_years, force=f
            )
        if stage == "compute":
            return lambda c: run_compute_stage(c, reg, ind, e)
        if stage == "documents":
            return lambda c: run_documents_stage(
                c, self.sec, reg, self.signal_rules, e,
                text_lookback_days=s.text_lookback_days,
                event_lookback_days=s.event_lookback_days, force=f,
                history_years={k: g.mdna_history_years for k, g in ind.groups.items()},
            )  # fmt: skip
        if stage in ("transcripts", "news", "market"):
            return lambda c: self._av_stage(stage, c)
        if stage == "textmetrics":
            return lambda c: run_text_metrics_stage(
                c,
                reg,
                ind,
                load_text_config(s.config_dir),
                self.llm,
                self.storage,
                e,
                labels=load_source_labels(s.config_dir),
                identities=load_dq_config(s.config_dir).metric_identities,
            )
        if stage == "governance":
            return lambda c: run_governance_stage(c, self.llm, self.storage, e)
        if stage == "legal":
            return lambda c: run_legal_stage(c, self.llm, self.storage, e)
        if stage == "clinical":
            return lambda c: run_clinical_stage(c, self.llm, self.storage, e)
        if stage == "signals":
            return lambda c: run_signals_stage(
                c, self.sec, self.signal_rules, e, llm=self.llm, industry=ind,
                hidden_metrics=self.registry.hidden_codes,
            )  # fmt: skip
        if stage == "dq":
            return lambda c: run_dq_stage(
                c, reg, ind, load_dq_config(s.config_dir), s.config_dir, e, run_id=self.run_id
            )
        if stage == "insights":
            return lambda c: run_insights_stage(
                c,
                reg,
                ind,
                self.llm,
                load_insights_config(s.config_dir, s.llm_provider),
                load_source_labels(s.config_dir),
                e,
            )
        if stage == "scores":
            return lambda c: run_scores_stage(
                c, self.llm, load_scores_config(s.config_dir), e, run_id=self.run_id
            )
        if stage == "publish":
            return lambda c: run_publish_stage(
                c,
                reg,
                ind,
                load_publish_config(s.config_dir),
                load_source_labels(s.config_dir),
                e,
                self.app_engine,
            )
        if stage == "metricdetail":
            return lambda c: run_metric_detail_stage(
                c,
                reg,
                ind,
                self.llm,
                load_insights_config(s.config_dir, s.llm_provider),
                load_source_labels(s.config_dir),
                e,
            )
        raise ValueError(f"unknown stage {stage}")

    def _av_stage(self, stage: str, companies: list[CompanyConfig]) -> list[TaskResult]:
        av = self.av
        if av is None:
            return [TaskResult(c.ticker, stage, ok=False, detail="ALPHAVANTAGE_API_KEY not set")
                    for c in companies]  # fmt: skip
        if stage == "transcripts":
            return run_transcripts_stage(companies, av, self.engine)
        if stage == "market":
            return run_market_stage(companies, av, self.registry, self.industry, self.engine)
        news_cfg = load_news_config(self.settings.config_dir)
        return run_news_stage(companies, av, self.engine, news_cfg, llm=self.llm)

    # -- statistics ------------------------------------------------------------------------

    def usage(self, since: datetime) -> dict[str, Any]:
        """External calls made by this runner, and LLM tokens/cost since `since`."""
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(llm_call.c.model, func.count(), func.sum(llm_call.c.input_tokens),
                       func.sum(llm_call.c.output_tokens))
                .where(llm_call.c.created_at >= since)
                .group_by(llm_call.c.model)
            ).all()  # fmt: skip
        pricing = self.llm[1].pricing if self.llm else {}
        currency = self.llm[1].pricing_currency if self.llm else "USD"
        llm: dict[str, Any] = {}
        cost = 0.0
        for model, calls, tin, tout in rows:
            price_in, price_out = pricing.get(model, (0.0, 0.0))
            amount = ((tin or 0) * price_in + (tout or 0) * price_out) / 1e6
            cost += amount
            llm[model] = {"calls": calls, "input_tokens": tin or 0, "output_tokens": tout or 0}
        av_calls = None
        if self._av is not None and self._av_calls_at_start is not None:
            av_calls = self._av.calls_today() - self._av_calls_at_start
        return {
            "sec_requests": self._sec.requests if self._sec else 0,
            "alphavantage_calls": av_calls,
            "llm": llm,
            "llm_cost_estimate": round(cost, 2),
            "llm_cost_currency": currency,
        }

    def __enter__(self) -> StageRunner:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:  # fmt: skip
        if self._sec is not None:
            self._sec.close()
        if self._av is not None:
            self._av.close()
