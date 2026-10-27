import importlib
import sys
from unittest.mock import Mock

import pytest


@pytest.fixture
def job_environment(monkeypatch, tmp_path):
    token = tmp_path / "token"
    token.write_text("test-token")
    values = {
        "ENVIRONMENT": "dev",
        "SLACK_WEBHOOK_URL": "https://slack.example.test/webhook",
        "PGHOST": "database.example.test",
        "PGDATABASE": "test_database",
        "ENTRA_TOKEN_PATH": str(token),
        "BLOBHOST": "https://storage.example.test",
        "CLIENT_ID": "test-client",
        "SCHEMA_URL": "https://schemas.example.test",
        "OUTPUT_PATH": str(tmp_path / "output"),
    }
    for name in (
        "PGUSER",
        "DATASELECTIEPGUSER",
        "ENTRATOKEN",
        "SEARCH_API_KEY",
        "SELECT_ENDPOINT",
        "RECORD_LIMIT",
        "WARN_BIG_CHANGES",
        "REPORT_SUCCESS_TO_SLACK",
        "REPORT_FAILURE_TO_SLACK",
        "REPORT_WARNING_TO_SLACK",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    monkeypatch.setattr("azure.identity.DefaultAzureCredential", Mock())
    yield values
    for name in ("exports.export", "exports.slack", "exports.update_azure_search"):
        sys.modules.pop(name, None)


@pytest.fixture
def slack_module(job_environment):
    return importlib.import_module("exports.slack")


@pytest.fixture
def search_module(job_environment):
    return importlib.import_module("exports.update_azure_search")
