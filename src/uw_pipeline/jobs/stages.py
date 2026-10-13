"""Runs one (stage, company) task: owns the clients and configuration the stages need."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from types import TracebackType
from typing import Any

from sqlalchemy import Engine, func, select

from uw_pipeline.clients.alphavantage import AlphaVantageClient
from uw_pipeline.clients.llm import ClaudeClient, LLMClient
from uw_pipeline.clients.ratelimit import TokenBucket
from uw_pipeline.clients.sec import SecClient
from uw_pipeline.clients.storage import LocalStorage
from uw_pipeline.compute.registry import load_registry
from uw_pipeline.config import CompanyConfig, load_companies, load_industry_config
from uw_pipeline.enrich.llm import LLMConfig, load_llm_config
from uw_pipeline.enrich.package import load_insights_config
from uw_pipeline.enrich.sources import load_source_labels
from uw_pipeline.enrich.text_metrics import load_text_config
from uw_pipeline.jobs.run_company import TaskResult, run_company_stage
from uw_pipeline.jobs.run_documents import run_documents_stage
from uw_pipeline.jobs.run_dq import run_dq_stage
from uw_pipeline.jobs.run_insights import run_insights_stage
from uw_pipeline.jobs.run_metric_detail import run_metric_detail_stage
from uw_pipeline.jobs.run_news import load_news_config, run_news_stage
from uw_pipeline.jobs.run_signals import run_signals_stage
from uw_pipeline.jobs.run_text_metrics import run_text_metrics_stage
from uw_pipeline.jobs.run_transcripts import run_transcripts_stage
from uw_pipeline.jobs.run_xbrl import run_compute_stage, run_facts_stage
from uw_pipeline.quality.dq import load_dq_config
from uw_pipeline.schema import llm_call
from uw_pipeline.settings import Settings
from uw_pipeline.signals.rules import load_signal_rules


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
        self.registry = load_registry(cfg_dir)
        self.registry.check_groups(self.industry.groups.values())
        self.signal_rules = load_signal_rules(cfg_dir)
        self.storage = LocalStorage(settings.raw_dir)
        self._sec: SecClient | None = None
        self._av: AlphaVantageClient | None = None
        self._av_calls_at_start: int | None = None
        self._llm: tuple[LLMClient, LLMConfig] | None = None
        self._llm_built = False

    # -- lazily built clients --------------------------------------------------------------

    @property
    def sec(self) -> SecClient:
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
        if not self._llm_built:
            self._llm_built = True
            s = self.settings
            if s.llm_enabled and s.anthropic_api_key is not None:
                client = ClaudeClient(
                    s.anthropic_api_key.get_secret_value(), workspace_id=s.anthropic_workspace_id
                )
                self._llm = (client, load_llm_config(s.config_dir))
        return self._llm

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
        if stage in ("transcripts", "news"):
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
            )
        if stage == "signals":
            return lambda c: run_signals_stage(c, self.sec, self.signal_rules, e, llm=self.llm)
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
                load_insights_config(s.config_dir),
                load_source_labels(s.config_dir),
                e,
            )
        if stage == "metricdetail":
            return lambda c: run_metric_detail_stage(
                c,
                reg,
                ind,
                self.llm,
                load_insights_config(s.config_dir),
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
        llm: dict[str, Any] = {}
        cost = 0.0
        for model, calls, tin, tout in rows:
            price_in, price_out = pricing.get(model, (0.0, 0.0))
            usd = ((tin or 0) * price_in + (tout or 0) * price_out) / 1e6
            cost += usd
            llm[model] = {"calls": calls, "input_tokens": tin or 0, "output_tokens": tout or 0}
        av_calls = None
        if self._av is not None and self._av_calls_at_start is not None:
            av_calls = self._av.calls_today() - self._av_calls_at_start
        return {
            "sec_requests": self._sec.requests if self._sec else 0,
            "alphavantage_calls": av_calls,
            "llm": llm,
            "llm_cost_usd_estimate": round(cost, 2),
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
