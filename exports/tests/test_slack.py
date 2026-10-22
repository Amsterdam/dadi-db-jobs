import importlib
from unittest.mock import Mock

import pytest
import requests


@pytest.mark.parametrize(
    ("status_name", "setting", "title", "summary"),
    [
        ("SUCCESS", "REPORT_SUCCESS_TO_SLACK", "Details", "success!"),
        ("FAILURE", "REPORT_FAILURE_TO_SLACK", "Errors", "failed!"),
        ("WARNING", "REPORT_WARNING_TO_SLACK", "Warnings", "warning!"),
    ],
)
@pytest.mark.parametrize("enabled", [True, False])
def test_slack_notification(
    slack_module, monkeypatch, status_name, setting, title, summary, enabled
):
    monkeypatch.setattr(slack_module, setting, enabled)
    post = Mock()
    monkeypatch.setattr(slack_module.requests, "post", post)
    messenger = slack_module.SlackMessenger("Test job")

    result = messenger.send_slack_message(
        ["first message", "second message"],
        status=getattr(messenger.Status, status_name),
    )

    if not enabled:
        assert result is None
        post.assert_not_called()
        return

    assert result is post.return_value
    post.assert_called_once()
    assert post.call_args.args == ("https://slack.example.test/webhook",)
    assert post.call_args.kwargs["headers"] == {"Content-type": "application/json"}
    blocks = post.call_args.kwargs["json"]["blocks"]
    assert f"[dev] Test job {summary}" in blocks[0]["text"]["text"]
    assert blocks[1]["title"] == {"type": "plain_text", "text": title}
    assert blocks[1]["is_collapsible"] is True
    assert blocks[1]["default_collapsed"] is True
    assert [block["text"]["text"] for block in blocks[1]["child_blocks"]] == [
        "first message",
        "second message",
    ]


@pytest.mark.parametrize(
    ("environment", "expected"),
    [
        ("prd", (True, True, True)),
        ("dev", (False, True, True)),
        ("test", (False, False, False)),
    ],
)
def test_notification_defaults(slack_module, monkeypatch, environment, expected):
    monkeypatch.setenv("ENVIRONMENT", environment)
    importlib.reload(slack_module)

    assert (
        slack_module.REPORT_SUCCESS_TO_SLACK,
        slack_module.REPORT_FAILURE_TO_SLACK,
        slack_module.REPORT_WARNING_TO_SLACK,
    ) == expected


def test_notification_environment_override(slack_module, monkeypatch):
    monkeypatch.setenv("REPORT_SUCCESS_TO_SLACK", "true")
    monkeypatch.setenv("REPORT_FAILURE_TO_SLACK", "false")
    importlib.reload(slack_module)

    assert slack_module.REPORT_SUCCESS_TO_SLACK is True
    assert slack_module.REPORT_FAILURE_TO_SLACK is False


def test_slack_propagates_connection_errors(slack_module, monkeypatch):
    monkeypatch.setattr(
        slack_module.requests,
        "post",
        Mock(side_effect=requests.ConnectionError("Slack unavailable")),
    )
    with pytest.raises(requests.ConnectionError, match="Slack unavailable"):
        slack_module.SlackMessenger("Test job").send_slack_message(
            ["failed"], status=slack_module.SlackMessenger.Status.FAILURE
        )
