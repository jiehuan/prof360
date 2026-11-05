"""The OpenShift Secret exposes AV credentials without enabling APIM by default."""

import pytest
import yaml

from tests.conftest import ROOT
from uw_pipeline.clients.alphavantage import (
    AlphaVantageSetupError,
    load_alphavantage_config,
    validate_alphavantage_settings,
)
from uw_pipeline.settings import Settings

SECRET = ROOT / "deploy/openshift/secret.example.yaml"
AV_CREDENTIALS = {
    "ALPHAVANTAGE_API_KEY",
    "ALPHAVANTAGE_CLIENT_ID",
    "ALPHAVANTAGE_CLIENT_SECRET",
    "ALPHAVANTAGE_SUBSCRIPTION_KEY",
}


def test_secret_template_lists_empty_credentials_and_defaults_to_direct():
    secret = yaml.safe_load(SECRET.read_text())
    assert secret["kind"] == "Secret" and secret["type"] == "Opaque"
    values = secret["stringData"]
    assert all(values[name] == "" for name in AV_CREDENTIALS)
    assert values["ALPHAVANTAGE_MODE"] == "direct"


def test_deployment_values_work_with_a_mounted_apim_profile(tmp_path):
    values = yaml.safe_load(SECRET.read_text())["stringData"]
    values.update({name: "fixture-not-a-real-credential" for name in AV_CREDENTIALS})
    values.update(ALPHAVANTAGE_MODE="apim", ALPHAVANTAGE_ENV="runtime")
    cfg = {
        "apim": {
            "default_env": "runtime",
            "environments": {
                "runtime": {
                    "query_url": "https://av-gateway.example.org/query",
                    "oauth": {
                        "token_url": "https://identity.example.org/token",
                        "scope": "av.read",
                    },
                }
            },
        },
    }
    (tmp_path / "alphavantage.yaml").write_text(yaml.safe_dump(cfg))
    settings = Settings(_env_file=None, **{key.lower(): value for key, value in values.items()})
    env = validate_alphavantage_settings(settings, load_alphavantage_config(tmp_path))
    assert env is not None and env.query_url == "https://av-gateway.example.org/query"


@pytest.mark.parametrize("missing", sorted(AV_CREDENTIALS))
def test_deployment_apim_values_require_each_credential(tmp_path, missing):
    values = yaml.safe_load(SECRET.read_text())["stringData"]
    values.update({name: "fixture-not-a-real-credential" for name in AV_CREDENTIALS})
    values.update(ALPHAVANTAGE_MODE="apim")
    values[missing] = ""
    settings = Settings(_env_file=None, **{key.lower(): value for key, value in values.items()})
    with pytest.raises(AlphaVantageSetupError, match=missing):
        validate_alphavantage_settings(settings, load_alphavantage_config(tmp_path))
