from enum import Enum

import environ
import logging
import os
import requests
import sys

env = environ.Env()
logger = logging.getLogger(__name__)
logging.basicConfig(stream=sys.stdout, level=logging.INFO)

ENVIRONMENT = os.environ["ENVIRONMENT"]

# Slack webhook settings
SLACK_WEBHOOK_URL = os.environ["SLACK_WEBHOOK_URL"]
REPORT_SUCCESS_TO_SLACK = env.bool("REPORT_SUCCESS_TO_SLACK", ENVIRONMENT == "prd")
REPORT_FAILURE_TO_SLACK = env.bool(
    "REPORT_FAILURE_TO_SLACK", ENVIRONMENT in ["dev", "prd"]
)
REPORT_WARNING_TO_SLACK = env.bool(
    "REPORT_WARNING_TO_SLACK", ENVIRONMENT in ["dev", "prd"]
)


class SlackMessenger:
    def __init__(self, script_name: str):
        self.script_name = script_name

    class Status(Enum):
        SUCCESS = "Success"
        FAILURE = "Failure"
        WARNING = "Warning"

    def send_slack_message(self, messages: list[str], status: Status = Status.SUCCESS):
        prefix = ""
        message_text = "\n".join(messages)
        foldout_title = {
            self.Status.SUCCESS: "Details",
            self.Status.FAILURE: "Errors",
            self.Status.WARNING: "Warnings",
        }[status]
        do_send = True
        if status == self.Status.FAILURE:
            prefix = f"🚨 *[{ENVIRONMENT}] {self.script_name} failed!* 🚨"
            do_send = REPORT_FAILURE_TO_SLACK
        elif status == self.Status.WARNING:
            prefix = f"⚠ *[{ENVIRONMENT}] {self.script_name} warning!* ⚠"
            do_send = REPORT_WARNING_TO_SLACK
        elif status == self.Status.SUCCESS:
            prefix = f"✅ *[{ENVIRONMENT}] {self.script_name} success!*"
            do_send = REPORT_SUCCESS_TO_SLACK
        if do_send:
            logger.info(f"Sending slack message: {prefix} {foldout_title}: {message_text}")
            return requests.post(
                SLACK_WEBHOOK_URL,
                headers={"Content-type": "application/json"},
                json={
                    "blocks": [
                        {
                            "type": "section",
                            "text": {"type": "mrkdwn", "text": prefix},
                        },
                        {
                            "type": "container",
                            "block_id": "bkb_container_collapsible",
                            "title": {
                                "type": "plain_text",
                                "text": foldout_title,
                            },
                            "is_collapsible": True,
                            "default_collapsed": True,
                            "child_blocks": [
                                {
                                    "type": "section",
                                    "text": {
                                        "type": "plain_text",
                                        "text": message
                                    }
                                } for message in messages
                            ]
                        },
                    ]
                },
            )
        else:
            logger.info(f"Not sending slack message: {prefix} {foldout_title}: {message_text}")
            return
