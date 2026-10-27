import runpy
from unittest.mock import Mock

import pytest


@pytest.mark.parametrize(
    "failures", [[], [ValueError("export failed"), "second failure"]]
)
def test_export_reports_result(monkeypatch, job_environment, slack_module, failures):
    credential = Mock()
    credentials = Mock(return_value=credential)
    storage = Mock()
    storage_client = Mock(return_value=storage)
    engine = Mock()
    create_engine = Mock(return_value=engine)
    export = Mock(return_value=failures)
    notify = Mock()
    monkeypatch.setattr("azure.identity.DefaultAzureCredential", credentials)
    monkeypatch.setattr("azure.storage.blob.BlobServiceClient", storage_client)
    monkeypatch.setattr("sqlalchemy.create_engine", create_engine)
    monkeypatch.setattr("schematools.exports.export", export)
    monkeypatch.setattr(slack_module.SlackMessenger, "send_slack_message", notify)

    runpy.run_module("exports.export", run_name="__main__")

    credentials.assert_called_once_with(managed_identity_client_id="test-client")
    storage_client.assert_called_once_with(
        job_environment["BLOBHOST"], credential=credential
    )
    create_engine.assert_called_once_with(
        "postgresql+psycopg://mdbdataservices_read:test-token"
        "@database.example.test:5432/test_database",
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
        pool_recycle=3600,
        connect_args={"cursor_factory": None},
    )
    export.assert_called_once_with(engine, storage, job_environment["OUTPUT_PATH"])
    status = slack_module.SlackMessenger.Status
    notify.assert_called_once_with(
        [str(failure) for failure in failures],
        status=status.FAILURE if failures else status.SUCCESS,
    )


def test_export_propagates_unexpected_errors(
    monkeypatch, job_environment, slack_module
):
    monkeypatch.setattr("azure.storage.blob.BlobServiceClient", Mock())
    monkeypatch.setattr("sqlalchemy.create_engine", Mock())
    monkeypatch.setattr(
        "schematools.exports.export", Mock(side_effect=RuntimeError("database offline"))
    )
    notify = Mock()
    monkeypatch.setattr(slack_module.SlackMessenger, "send_slack_message", notify)

    with pytest.raises(RuntimeError, match="database offline"):
        runpy.run_module("exports.export", run_name="__main__")

    notify.assert_not_called()
