"""The metric chat package for the BFF (Phase 19.B, D-091): grounding, messages, versions, the
async SecureGPT client (mocked gateway), a whole turn, the published prompt and the export."""

import asyncio
import hashlib
import json
import re
import subprocess
import sys

import httpx
import pytest
import respx
import yaml
from sqlalchemy import create_engine, func, select

from tests.conftest import CONFIG_DIR, ROOT
from uw_metric_chat import (
    ANSWER_SCHEMA,
    CANNOT_CONFIRM,
    AsyncSecureGPT,
    ChatAnswer,
    ChatContext,
    ChatPromptRow,
    GatewayError,
    HistoryTurn,
    IncompatibleError,
    SecureGPTSettings,
    answer_question,
    build_messages,
    check_answer,
    check_prompt,
)
from uw_metric_chat.chat import UNAVAILABLE
from uw_metric_chat.grounding import mentions
from uw_metric_chat.securegpt import API_VERSION, GATEWAYS, SCOPE, Completion
from uw_pipeline.jobs import run_publish
from uw_pipeline.jobs.run_publish import MetricChatConfig, publish_chat_prompt
from uw_pipeline.schema_app import ChatPrompt, app_metadata

PACKAGE = ROOT / "src" / "uw_metric_chat"


def _ctx(**extra) -> ChatContext:
    points = [
        ("dp:FY2025:interest_coverage", "Interest Coverage FY2025", 4.0013755158, "ratio"),
        ("dp:FY2025:ebit", "EBIT FY2025", 2909e6, "USD"),
        ("dp:FY2025:interest_expense", "Interest Expense FY2025", 727e6, "USD"),
        ("dp:FY2024:interest_coverage", "Interest Coverage FY2024", 15.1111111111, "ratio"),
        ("dp:2025Q4:ebit", "EBIT FY2025 Q4", -3646e6, "USD"),
        ("dp:2026Q1:ebit", "EBIT FY2026 Q1", 2926e6, "USD"),
        ("dp:2025Q4:interest_coverage", "Interest Coverage FY2025 Q4", None, "ratio"),
    ]
    return ChatContext.model_validate({
        "version": 1, "company": {"ticker": "GM"}, "metric": {"code": "interest_coverage"},
        "periods": [{"period": "2025Q4", "nm_reason": "Not meaningful when EBIT ≤ 0."}],
        "data_points": [{"id": i, "label": label, "value": v, "unit": u,
                         "status": "OK" if v is not None else "NM"}
                        for i, label, v, u in points],
        **extra,
    })  # fmt: skip


def _answer(text: str, cites: list[str], **kw) -> ChatAnswer:
    return ChatAnswer.model_validate({"answer": text, "citations": cites, "in_scope": True,
                                      "possible_issue": False, "issue_summary": None,
                                      "period": None} | kw)  # fmt: skip


FY = ["dp:FY2025:interest_coverage", "dp:FY2025:ebit", "dp:FY2025:interest_expense"]


# -- the package stands alone ------------------------------------------------------------------


def test_package_imports_nothing_from_the_pipeline() -> None:
    for path in PACKAGE.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "uw_pipeline" not in text, path.name
        assert not re.search(r"^from uw_metric_chat|^import uw_metric_chat", text, re.M), (
            f"{path.name}: use relative imports (the BFF puts the folder anywhere)")  # fmt: skip
        imports = set(re.findall(r"^(?:from|import) (\w+)", text, re.M))
        assert imports <= {"__future__", "asyncio", "collections", "dataclasses", "itertools",
                           "json", "logging", "os", "re", "time", "typing", "httpx",
                           "pydantic"}, (path.name, imports)  # fmt: skip


def test_gateways_match_the_pipeline_config() -> None:
    cfg = yaml.safe_load((CONFIG_DIR / "securegpt.yaml").read_text(encoding="utf-8"))
    assert cfg["environments"] == GATEWAYS
    assert (cfg["scope"], cfg["api_version"]) == (SCOPE, API_VERSION)


# -- grounding ---------------------------------------------------------------------------------


def test_numbers_are_read_with_their_units_and_labels_are_skipped() -> None:
    text = ("In FY2025 (10-K, Exhibit 99.1, Item 2.02, filed 2026-01-27, accession "
            "0001467858-26-000013, dp:FY2025:ebit, 2026Q3, Q4 2025) coverage was 4.0x on "
            "$2.91B of EBIT and 727 million of interest; margin 4.9%, a loss of -$3.65B, the "
            "5th of 3 peers, 25 bps; the 10-K filed January 27, 2026 and on 3 Feb.")  # fmt: skip
    found = {m.text: m.candidates[0][0] for m in mentions(text)}
    want = {"4.0x": 4.0, "$2.91B": 2.91e9, "727 million": 727e6, "4.9%": 0.049,
            "-$3.65B": -3.65e9, "25 bps": 0.0025}  # fmt: skip
    assert found == pytest.approx(want)


def test_values_of_cited_points_are_grounded() -> None:
    ctx = _ctx()
    ok = check_answer(_answer("Coverage was 4.0x: EBIT of $2.91B over $727M of interest.", FY), ctx)
    assert ok.ok and not ok.computed and not ok.added
    # A loss written without its sign; numbers written in millions.
    assert check_answer(_answer("EBIT was a loss of $3.65 billion.", ["dp:2025Q4:ebit"]), ctx).ok
    assert check_answer(_answer("4.00x (2,909 / 727, in millions).", FY), ctx).ok


def test_computations_on_cited_points_are_grounded() -> None:
    ctx = _ctx()
    diff = check_answer(_answer("EBIT exceeded interest by $2.18B.", FY), ctx)
    assert diff.ok and diff.computed == [
        "$2.18B = dp:FY2025:ebit - dp:FY2025:interest_expense"]  # fmt: skip
    cites = ["dp:FY2025:interest_coverage", "dp:FY2024:interest_coverage"]
    change = check_answer(_answer("Coverage fell 74% from FY2024 to FY2025.", cites), ctx)
    assert change.ok and "change from dp:FY2024" in change.computed[0]


def test_uncited_points_are_added_closest_first() -> None:
    check = check_answer(_answer("EBIT was $2.91B.", ["dp:FY2025:interest_coverage"]), _ctx())
    assert check.ok and check.added == ["dp:FY2025:ebit"]  # 2.909B, not FY2026 Q1's 2.926B


def test_numbers_stated_by_the_context_or_the_question_are_grounded() -> None:
    finding = {"rule": "quarter_jump", "severity": "Warning", "period": "FY2025 Q4",
               "message": "net income FY2025 Q4 changed -707% vs a year earlier"}  # fmt: skip
    ctx = _ctx(dq_findings=[finding])
    assert check_answer(_answer("A finding flags a -707% swing.", []), ctx).ok
    echo = check_answer(_answer("The data does not show the 0.44x you mention.", []), ctx,
                        question="I saw 0.44x before. Is this right?")  # fmt: skip
    assert echo.ok and echo.echoed == ["0.44x"]


def test_invented_numbers_and_unknown_ids_are_rejected() -> None:
    ctx = _ctx()
    bad = check_answer(_answer("Coverage was 5.2x.", FY), ctx)
    assert not bad.ok and bad.unmatched == ["5.2x"]
    assert "5.2x" in bad.detail
    assert not check_answer(_answer("Coverage rose 23%.", FY[:1]), ctx).ok
    unknown = check_answer(_answer("Coverage was 4.0x.", ["dp:FY2099:x"]), ctx)
    assert not unknown.ok and "dp:FY2099:x" in unknown.reason
    # Not numbers: counts up to 10, years, periods.
    assert check_answer(_answer("2 of 5 candidates in FY2025 and 2024; Q3 2026.", []), ctx).ok


# -- messages and versions ---------------------------------------------------------------------


def _prompt(**over) -> ChatPromptRow:
    row = {"promptVersion": "metric_chat_v1", "systemText": "RULES",
           "outputSchema": json.dumps(ANSWER_SCHEMA), "contextVersions": "1",
           "minPackageVersion": "0.1.0"} | over  # fmt: skip
    return ChatPromptRow.model_validate(row)


def test_messages_carry_the_prompt_context_and_recent_turns() -> None:
    history = [HistoryTurn(question=f"q{i}", answer=f"a{i}") for i in range(8)]
    msgs = build_messages(_prompt(), _ctx(), history, "  Why?" + "x" * 2000)
    assert msgs[0]["role"] == "system" and msgs[0]["content"].startswith("RULES")
    assert '"dp:FY2025:ebit"' in msgs[0]["content"]
    assert [m["content"] for m in msgs[1:3]] == ["q2", "a2"]  # the last six turns
    assert len(msgs) == 1 + 12 + 1 and len(msgs[-1]["content"]) == 1000
    assert _prompt().output_schema == ANSWER_SCHEMA


def test_prompt_and_context_versions_are_checked() -> None:
    check_prompt(_prompt())
    with pytest.raises(IncompatibleError, match=r"needs uw_metric_chat 0\.2\.0"):
        check_prompt(_prompt(minPackageVersion="0.2.0"))
    with pytest.raises(IncompatibleError, match="context versions"):
        check_prompt(_prompt(contextVersions="2"))
    reply_ctx = _ctx().model_dump() | {"version": 2}
    with pytest.raises(IncompatibleError, match="context version 2"):
        asyncio.run(answer_question(_Fake({}), _prompt(), reply_ctx, [], "q"))


# -- one turn ----------------------------------------------------------------------------------


class _Fake:
    def __init__(self, output: dict | Exception) -> None:
        self.output, self.calls = output, []

    async def complete_json(self, *, model, messages, schema, max_tokens=1500) -> Completion:
        self.calls.append((model, messages, schema))
        if isinstance(self.output, Exception):
            raise self.output
        return Completion(self.output, json.dumps(self.output), model, 5000, 300, 1200)


def _turn(output, question="How is it computed?") -> tuple:
    fake = _Fake(output)
    reply = asyncio.run(answer_question(fake, _prompt(), _ctx().as_json(), [], question))
    return reply, fake


def test_a_grounded_answer_is_shown_with_its_citations() -> None:
    out = {"answer": "FY2025 coverage was 4.0x: EBIT of $2.91B over $727M.",
           "citations": ["dp:FY2025:interest_coverage", "dp:FY2025:ebit"], "in_scope": True,
           "possible_issue": True, "issue_summary": "Check the basis.",
           "period": "FY2025"}  # fmt: skip
    reply, fake = _turn(out)
    assert reply.grounding == "ok" and reply.answer == out["answer"]
    assert [c.id for c in reply.citations] == [*out["citations"], "dp:FY2025:interest_expense"]
    assert reply.citations[1].period == "FY2025" and reply.citations[1].value == 2909e6
    assert (reply.possible_issue, reply.issue_summary, reply.period) == (
        True, "Check the basis.", "FY2025")  # fmt: skip
    assert fake.calls[0][0] == "gpt-6-luna-2026-09-22" and fake.calls[0][2] == ANSWER_SCHEMA
    row = reply.log_row()
    assert (row["Grounding"], row["InputTokens"], row["LatencyMs"]) == ("ok", 5000, 1200)
    assert json.loads(row["Citations"])[-1] == "dp:FY2025:interest_expense"
    # Every logged column exists in the BFF's table.
    sql = (ROOT / "deploy" / "sql" / "bff_metric_chat.sql").read_text(encoding="utf-8")
    table = sql.split("CREATE TABLE dbo_professional360.UserMetricChat (", 1)[1].split(");")[0]
    assert set(row) <= set(re.findall(r"^\s+(\w+) ", table, re.M))


def test_failures_show_fixed_text() -> None:
    invented = {"answer": "Coverage was 9.9x.", "citations": [], "in_scope": True,
                "possible_issue": False, "issue_summary": None, "period": None}  # fmt: skip
    reply, _ = _turn(invented)
    assert (reply.answer, reply.grounding) == (CANNOT_CONFIRM, "rejected")
    assert "9.9x" in reply.grounding_detail and reply.raw_answer and not reply.citations
    reply, _ = _turn({"answer": "x"})  # not the schema
    assert (reply.answer, reply.grounding) == (CANNOT_CONFIRM, "error")
    reply, _ = _turn(GatewayError("transient", "SecureGPT chat completion: gave up (429)"))
    assert (reply.answer, reply.grounding) == (UNAVAILABLE, "error")
    assert reply.grounding_detail.startswith("transient:") and reply.input_tokens is None


# -- the SecureGPT client (mocked gateway) -----------------------------------------------------

TOKEN = GATEWAYS["dv"]["token_url"]
CHAT = f"{GATEWAYS['dv']['base_url']}/gpt-6-luna-2026-09-22/chat/completions"
SECRETS = {"SECUREGPT_CLIENT_ID": "cid", "SECUREGPT_CLIENT_SECRET": "very-secret",
           "SECUREGPT_SUBSCRIPTION_KEY": "sub-key"}  # fmt: skip


def _completion(content: str, **message) -> dict:
    return {
        "model": "gpt-6-luna",
        "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        "choices": [{"finish_reason": "stop", "message": {"content": content, **message}}],
    }


async def _call(client: AsyncSecureGPT) -> Completion:
    return await client.complete_json(model="gpt-6-luna-2026-09-22",
                                      messages=[{"role": "user", "content": "q"}],
                                      schema=ANSWER_SCHEMA)  # fmt: skip


def _client(sleeps: list[float], **over) -> AsyncSecureGPT:
    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    return AsyncSecureGPT(SecureGPTSettings.from_env(SECRETS, **over), sleep=sleep)


def test_settings_need_the_three_secrets() -> None:
    with pytest.raises(GatewayError, match="SECUREGPT_CLIENT_SECRET"):
        SecureGPTSettings.from_env({"SECUREGPT_CLIENT_ID": "x"})
    with pytest.raises(GatewayError, match="'xx' is not dv, pp or pr"):
        AsyncSecureGPT(SecureGPTSettings.from_env(SECRETS | {"SECUREGPT_ENV": "xx"}))


def test_token_is_cached_refreshed_after_401_and_calls_are_retried() -> None:
    sleeps: list[float] = []

    async def run() -> list[Completion]:
        client = _client(sleeps)
        try:
            return [await _call(client) for _ in range(3)]
        finally:
            await client.aclose()

    with respx.mock(assert_all_called=True) as router:
        token = router.post(TOKEN).mock(side_effect=[
            httpx.Response(200, json={"access_token": "t1", "expires_in": 3600}),
            httpx.Response(200, json={"access_token": "t2", "expires_in": 3600})])  # fmt: skip
        chat = router.post(CHAT).mock(side_effect=[
            httpx.Response(200, json=_completion('{"a": 1}')),
            httpx.Response(429, headers={"retry-after": "3"}),
            httpx.Response(200, json=_completion('```json\n{"a": 2}\n```')),
            httpx.Response(401),
            httpx.Response(200, json=_completion('{"a": 3}'))])  # fmt: skip
        done = asyncio.run(run())
    assert [d.output for d in done] == [{"a": 1}, {"a": 2}, {"a": 3}]
    assert token.call_count == 2 and sleeps == [3.0]
    first = chat.calls[0].request
    assert first.headers["authorization"] == "Bearer t1"
    assert first.url.params["api-version"] == API_VERSION
    body = json.loads(first.content)
    assert body["response_format"]["json_schema"]["strict"] is True
    assert chat.calls[-1].request.headers["authorization"] == "Bearer t2"
    assert done[0].input_tokens == 10


@pytest.mark.parametrize(("status", "body", "kind"), [
    (200, _completion("", refusal="no"), "output"),
    (200, _completion("not json"), "output"),
    (400, {"error": {"code": "content_filter", "message": "x"}}, "output"),
    (404, {"error": {"code": "DeploymentNotFound", "message": "x"}}, "setup"),
    (503, {}, "transient"),
])  # fmt: skip
def test_gateway_failures_are_typed_and_keep_secrets_out(status, body, kind) -> None:
    sleeps: list[float] = []

    async def run() -> Completion:
        client = _client(sleeps)
        try:
            return await _call(client)
        finally:
            await client.aclose()

    with respx.mock() as router:
        router.post(TOKEN).mock(return_value=httpx.Response(200, json={"access_token": "t"}))
        router.post(CHAT).mock(return_value=httpx.Response(status, json=body))
        with pytest.raises(GatewayError) as err:
            asyncio.run(run())
    assert err.value.kind == kind
    assert "very-secret" not in str(err.value) and "sub-key" not in str(err.value)


def test_network_errors_are_retried_then_reported() -> None:
    sleeps: list[float] = []

    async def run() -> Completion:
        client = _client(sleeps, max_attempts=2)
        try:
            return await _call(client)
        finally:
            await client.aclose()

    with respx.mock() as router:
        router.post(TOKEN).mock(side_effect=httpx.ConnectTimeout("slow"))
        with pytest.raises(GatewayError, match="gave up after 2 attempts") as err:
            asyncio.run(run())
    assert err.value.kind == "transient" and sleeps == [1.0, 2.0]


# -- the pipeline side: published prompt, export ------------------------------------------------


def test_publish_writes_one_active_prompt(tmp_path, monkeypatch) -> None:
    from datetime import UTC, datetime

    monkeypatch.setattr(run_publish, "PROMPTS_DIR", tmp_path)
    (tmp_path / "metric_chat_v1.md").write_text("one", encoding="utf-8")
    (tmp_path / "metric_chat_v2.md").write_text("two", encoding="utf-8")
    app = create_engine("sqlite://")
    app_metadata.create_all(app)
    v1 = MetricChatConfig(prompt_version="metric_chat_v1", min_package_version="0.1.0",
                          models=["m"])  # fmt: skip
    day1, day2 = datetime(2026, 11, 7, tzinfo=UTC), datetime(2026, 11, 8, tzinfo=UTC)
    with app.begin() as c:
        assert publish_chat_prompt(c, v1, day1)
    with app.begin() as c:
        assert not publish_chat_prompt(c, v1, day2)  # unchanged: not rewritten
        row = c.execute(select(ChatPrompt)).mappings().one()
    assert (row["PromptVersion"], row["SystemText"], row["IsActive"]) == ("metric_chat_v1", "one",
                                                                          True)  # fmt: skip
    assert json.loads(row["OutputSchema"]) == ANSWER_SCHEMA and row["ContextVersions"] == "1"
    assert row["UpdatedAt"].date() == day1.date()
    with app.begin() as c:
        assert publish_chat_prompt(c, v1.model_copy(update={"prompt_version": "metric_chat_v2"}),
                                   day2)  # fmt: skip
        rows = {r.PromptVersion: r.IsActive for r in c.execute(select(ChatPrompt))}
        assert c.execute(select(func.count()).select_from(ChatPrompt)).scalar_one() == 2
    assert rows == {"metric_chat_v1": False, "metric_chat_v2": True}


def test_prompt_of_the_config_exists_and_names_no_provider() -> None:
    cfg = yaml.safe_load((CONFIG_DIR / "metric_chat.yaml").read_text(encoding="utf-8"))
    prompt = ROOT / "src" / "uw_pipeline" / "enrich" / "prompts" / f"{cfg['prompt_version']}.md"
    text = prompt.read_text(encoding="utf-8")
    assert "citations" in text and not re.search(r"claude|anthropic|openai", text, re.I)


def test_export_writes_headed_files_checksums_and_readme(tmp_path) -> None:
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import export_metric_chat
    finally:
        sys.path.pop(0)
    out = export_metric_chat.export(tmp_path, allow_dirty=True)
    assert out.name == "uw_metric_chat-0.1.0"
    files = sorted(p.name for p in (out / "uw_metric_chat").glob("*.py"))
    assert files == sorted(p.name for p in PACKAGE.glob("*.py"))
    for line in (out / "CHECKSUMS").read_text().splitlines():
        digest, name = line.split("  ")
        data = (out / name).read_bytes()
        assert hashlib.sha256(data).hexdigest() == digest
        assert data.startswith(b"# Generated by prof360") and b"Do not edit" in data
    readme = (out / "README.zh-CN.md").read_text(encoding="utf-8")
    assert "uw_metric_chat 0.1.0" in readme and "{VERSION}" not in readme
    assert (out / "bff_metric_chat.sql").exists() and (out / "INTERFACE.zh-CN.md").exists()
    assert sorted(p.name for p in (out / "reference_bff").glob("*.py")) == [
        "__init__.py", "api.py", "main.py", "store.py"]  # fmt: skip
    # The reference BFF imports beside the package and builds its app.
    code = (f"import sys; sys.path.insert(0, {str(out)!r}); "
            "from reference_bff.main import create_app; "
            "print(len(create_app().routes))")  # fmt: skip
    ref = subprocess.run([sys.executable, "-I", "-c", code], cwd=tmp_path,
                         capture_output=True, text=True, check=False)  # fmt: skip
    assert ref.returncode == 0 and int(ref.stdout) > 3, ref.stderr
    # The exported folder imports on its own, under another parent package.
    vendor = tmp_path / "bff" / "vendor"
    vendor.mkdir(parents=True)
    (vendor / "__init__.py").write_text("")
    (out / "uw_metric_chat").rename(vendor / "uw_metric_chat")
    bff = str(tmp_path / "bff")
    code = (
        f"import sys; sys.path.insert(0, {bff!r}); "
        "from vendor.uw_metric_chat import VERSION, answer_question; print(VERSION)"
    )
    run = subprocess.run([sys.executable, "-I", "-c", code], cwd=tmp_path,
                         capture_output=True, text=True, check=False)  # fmt: skip
    assert run.stdout.strip() == "0.1.0", run.stderr
