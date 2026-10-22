import logging
import os
import sys
from pathlib import Path

from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient
from schematools.exports import export
from sqlalchemy import create_engine

from .slack import SlackMessenger

PGHOST = os.environ["PGHOST"]
PGDATABASE = os.environ["PGDATABASE"]
PGUSER = os.environ.get("PGUSER", "mdbdataservices_read")
PGPASSWORD = Path(os.environ.get("ENTRA_TOKEN_PATH")).read_text()
DATABASE_URL = f"postgresql+psycopg://{PGUSER}:{PGPASSWORD}@{PGHOST}:5432/{PGDATABASE}"
BLOBHOST = os.environ["BLOBHOST"]
CLIENT_ID = os.environ["CLIENT_ID"]
OUTPUT_PATH = os.getenv("OUTPUT_PATH", "tmp")

logger = logging.getLogger(__name__)
logging.basicConfig(stream=sys.stdout, level=logging.INFO)
messenger = SlackMessenger("Exports")


if __name__ == "__main__":
    default_credential = DefaultAzureCredential(managed_identity_client_id=CLIENT_ID)
    storage_client = BlobServiceClient(BLOBHOST, credential=default_credential)
    engine = create_engine(
        DATABASE_URL,
        pool_pre_ping=True,
        pool_size=5,  # Reduce pool size
        max_overflow=10,
        pool_recycle=3600,
        # Enable server-side cursors by default
        connect_args={"cursor_factory": None},
    )
    failures = export(engine, storage_client, OUTPUT_PATH)
    if failures:
        errors = [str(failure) for failure in failures]
        messenger.send_slack_message(
            errors,
            status=SlackMessenger.Status.FAILURE,
        )
    else:
        messenger.send_slack_message([], status=SlackMessenger.Status.SUCCESS)
