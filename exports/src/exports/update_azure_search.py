import contextlib
import gc
import json
import logging
import os
import shutil
import sys
import time
import traceback
import urllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import backoff
import environ
import requests
from azure.identity import DefaultAzureCredential
from schematools.exports import export_tables
from schematools.loaders import get_schema_loader
from schematools.types import (
    DatasetSchema,
    DatasetTableSchema,
    Export,
    ExportContext,
    Scope,
)
from sqlalchemy import Engine, create_engine

from .slack import SlackMessenger

env = environ.Env()

logger = logging.getLogger(__name__)
logging.basicConfig(stream=sys.stdout, level=logging.INFO)
messenger = SlackMessenger("Dataselectie data import")

# Provide entratoken through FilePath or directly in environment variable for debugging
ENTRATOKEN = None
if (token_path := os.environ.get("ENTRA_TOKEN_PATH")) is not None:
    ENTRATOKEN = Path(token_path).read_text()
PGPASSWORD = env.str("ENTRATOKEN", ENTRATOKEN)

# Warn about big changes in number of index records before changing indexes
WARN_BIG_CHANGES = env.bool("WARN_BIG_CHANGES", True)

# Limit the number of exported records (for easier debugging).
RECORD_LIMIT = env.int("RECORD_LIMIT", None)

# Search endpoint to update, Default None for all endpoints
SELECT_ENDPOINT = env.str("SELECT_ENDPOINT", None)

PGHOST = os.environ["PGHOST"]
PGUSER = env.str("DATASELECTIEPGUSER", "mdbdataservices_read")
PGDATABASE = os.environ["PGDATABASE"]
SCHEMA_URL = os.environ["SCHEMA_URL"]
ENVIRONMENT = os.environ["ENVIRONMENT"]
SEARCH_API_KEY = env.str("SEARCH_API_KEY", None)
CLIENT_ID = os.environ["CLIENT_ID"]
OUTPUT_PATH = env.str("OUTPUT_PATH", "tmp")

DATABASE_URL = f"postgresql+psycopg://{PGUSER}:{PGPASSWORD}@{PGHOST}:5432/{PGDATABASE}"
DEFAULT_CREDENTIAL = DefaultAzureCredential(managed_identity_client_id=CLIENT_ID)
SEARCH_API_URL = f"https://{ENVIRONMENT}-bbn1-search.search.windows.net/"
SEARCH_API_VERSION = "2025-08-01-preview"
BATCH_SIZE = 10000


@dataclass
class Endpoint:
    """An Azure Search endpoint.

    An Azure Search endpoint and ams-schema source data
    Relates Azure Search indexes to ams-schema source data.

    Attributes:
        source_table_id (str) : The id of the ams-schema table that is the source for this endpoint
        source_table_version (str) : The version of the ams-schema source table.
        source_dataset_id (str) : The id of the ams-schema dataset that the source table belongs to.
        search_alias (str) : The name of the Azure Search alias of the endpoint
        scopes (list) : A list of the ams-schema scopes required to read the source data. ex: ["BRK/RSN"]
        indexes (frozenset) : A set of 2 Azure Search indexes used for blue/green swap.
        template_index (str) : Name of an index that is only used as a template for recreating the blue/green indexes. (If fields are modified they should be modified in this index)
        transform_source_fields (list) : fields used by the transform function that are not in the index. (default [])
        transform (function): Transformation that should be applied to a record before it is inserted in te index. (default 'lambda record : record')

    """

    source_dataset_id: str
    source_dataset_version: str
    source_table_id: str
    search_alias: str
    scopes: list
    indexes: frozenset
    template_index: str
    transform_source_fields: list = field(default_factory=list)
    transform: Callable[[dict], dict] = lambda record: record


def bag_transform(record):
    """
    Add missing fields
    """
    nullable_fields = [
        "panden",
        "gebruiksdoelOmschrijvingen",
        "standplaatsGebruiksdoelOmschrijvingen",
        "ligplaatsGebruiksdoelOmschrijvingen",
    ]
    record["huisnummerStr"] = str(record["huisnummer"])
    try:
        record["longitude"] = record["verblijfsobjectGeometrie"]["coordinates"][0]
    except (KeyError, TypeError):
        record["longitude"] = None
    try:
        record["latitude"] = record["verblijfsobjectGeometrie"]["coordinates"][1]
    except (KeyError, TypeError):
        record["latitude"] = None
    for field in nullable_fields:
        record[field] = record[field] if record[field] else []
    return record


def brk_transform(record):
    record["id"] = record["id"].replace(".", "")
    return record


def hr_transform(record):
    """
    Make sure activiteitenVes is sent as an empty array and not None
    """
    record["activiteitenVes"] = (
        record["activiteitenVes"] if record["activiteitenVes"] else []
    )
    return record


SEARCH_ENDPOINTS = {
    "benkagg-brkbasisdataselectie": Endpoint(
        source_dataset_id="benkagg",
        source_dataset_version="v1",
        source_table_id="brkbasisdataselectie",
        search_alias="benkagg-brkbasisdataselectie",
        scopes=["BRK/RSN"],
        indexes=frozenset(
            (
                "benkagg_brkbasisdataselectie-index-blue",
                "benkagg_brkbasisdataselectie-index-green",
            )
        ),
        template_index="benkagg_brkbasisdataselectie-index-template",
        transform=brk_transform,
    ),
    "benkagg-adresseerbareobjecten": Endpoint(
        source_dataset_id="benkagg",
        source_dataset_version="v1",
        source_table_id="adresseerbareobjecten",
        search_alias="benkagg-adresseerbareobjecten",
        scopes=["OPENBAAR"],
        indexes=frozenset(
            (
                "benkagg_adresseerbareobjecten-index-blue",
                "benkagg_adresseerbareobjecten-index-green",
            )
        ),
        template_index="benkagg_adresseerbareobjecten-index-template",
        transform_source_fields=["volgnummer", "beginGeldigheid", "eindGeldigheid"],
        transform=bag_transform,
    ),
    "benkagg-handelsregisterkvk": Endpoint(
        source_dataset_id="benkagg",
        source_dataset_version="v1",
        source_table_id="handelsregisterkvk",
        search_alias="benkagg-handelsregisterkvk",
        scopes=["HR/R", "FP/MDW"],
        indexes=frozenset(
            (
                "benkagg_handelsregisterkvk-index-blue",
                "benkagg_handelsregisterkvk-index-green",
            )
        ),
        template_index="benkagg_handelsregisterkvk-index-template",
        transform=hr_transform,
    ),
}

class SearchAPI:
    def __init__(
        self,
        url=SEARCH_API_URL,
        version=SEARCH_API_VERSION,
        api_key=SEARCH_API_KEY,
        default_credential=DEFAULT_CREDENTIAL,
    ):
        self.url = url
        self.version = version
        self.api_key = api_key
        self.default_credential = default_credential

    def get_authorization_header(self):
        if self.api_key is None:
            token = self.default_credential.get_token(
                "https://search.azure.com/.default"
            )
            return {"Authorization": f"Bearer {token.token}"}
        else:
            return {"api-key": SEARCH_API_KEY}

    def get(self, path, params={}, raise_for_status=True):
        response = requests.get(
            url=urllib.parse.urljoin(self.url, path),
            params={"api-version": self.version} | params,
            headers=self.get_authorization_header(),
        )
        if raise_for_status:
            response.raise_for_status()
        return response

    def delete(self, path):
        return requests.delete(
            url=urllib.parse.urljoin(self.url, path),
            params={"api-version": self.version},
            headers=self.get_authorization_header(),
        )

    def post(self, path, json, raise_for_status=True):
        response = requests.post(
            url=urllib.parse.urljoin(self.url, path),
            params={"api-version": self.version},
            headers=self.get_authorization_header(),
            json=json,
        )
        if raise_for_status:
            response.raise_for_status()
        return response

    def put(self, path, json, raise_for_status=True):
        response = requests.put(
            url=urllib.parse.urljoin(self.url, path),
            params={"api-version": self.version},
            headers=self.get_authorization_header(),
            json=json,
        )
        if raise_for_status:
            response.raise_for_status()
        return response


def update_search_endpoint(
    engine: Engine,
    dataset_schema: DatasetSchema,
    endpoint: Endpoint,
):
    """Update a search endpoint with new data from the database

    Will recreate the index that is not currently in use by the
    endpoint alias from the template index.
    The new index will be filled from the database.
    If this was successful the alias will be set to point to the new index.
    """

    api = SearchAPI()
    result = {"status": "Success", "message": ""}

    # Determine which index is currently in use and which index will be next
    alias = api.get(f"aliases/{endpoint.search_alias}").json()
    current_indexes = set(alias.get("indexes"))
    if len(current_indexes) != 1:
        raise RuntimeError(
            f"Alias {endpoint.search_alias} has {len(current_indexes)} indexes there can be only one."
        )

    current_index_name = alias.get("indexes")[0]
    logger.info(f"Current index is {current_index_name}")
    next_index_name = list(endpoint.indexes.difference(current_indexes))[0]

    # (Re)create next index from template index
    template_index = api.get(f"indexes/{endpoint.template_index}").json()
    logger.info(f"Next index is {next_index_name}")
    logger.info(f"Replacing next index from template index '{endpoint.template_index}'")
    indexes = [
        idx["name"] for idx in api.get("indexes", {"$select": "name"}).json()["value"]
    ]
    if next_index_name in indexes:
        response = api.delete(f"indexes/{next_index_name}")
        logger.info(
            f"Index {next_index_name} deleted: {response.status_code} {response.reason}"
        )
    response = api.post("indexes", template_index | {"name": next_index_name})
    logger.info(
        f"Index {next_index_name} created: {response.status_code} {response.reason}"
    )

    # Remove fields that are not part of the index from the schema (So only required fields are processed)
    index_fields = [field["name"] for field in template_index.get("fields")]
    table = dataset_schema.get_table_by_id(
        endpoint.source_table_id, endpoint.source_dataset_version
    )
    table.fields = [
        field
        for field in table.fields
        if field.id in index_fields or field.id in endpoint.transform_source_fields
    ]

    logger.info(f"Exporting table {table.db_name} with scopes {endpoint.scopes}")
    uploaded_doc_count = 0
    file_name: Path | None = None
    try:
        file_name = export_jsonl_from_db(engine, table, endpoint.scopes, OUTPUT_PATH)
        logger.info(f"Table exported to: {file_name}")
        # Upload the jsonlines file to the index in batches
        uploaded_doc_count = upload_file_to_index(file_name, endpoint, next_index_name)

    except Exception as e:
        logger.exception(
            f"Failed to export data for endpoint {endpoint.search_alias}: {str(e)}"
        )
        result = {
            "status": "Failure",
            "message": f"Exporting table {table.db_name} with scopes {endpoint.scopes} for endpoint {endpoint.search_alias} failed: {str(e)}",
        }
    else:
        # Validate that the data was succesfully inserted into the index
        doc_count_difference = wait_for_index_to_fill(
            next_index_name, uploaded_doc_count
        )
        if doc_count_difference != 0:
            logger.exception(
                f"Failed to fill index: Timeout while waiting for '{next_index_name}' index to fill. {-doc_count_difference} uploaded records missing from index."
            )
            result = {
                "status": "Failure",
                "message": f"Failed to fill index: Timeout while waiting for '{next_index_name}' index to fill. {-doc_count_difference} uploaded records missing from index.",
            }
        else:
            logger.info(
                f"{uploaded_doc_count} records succesfully uploaded to {next_index_name}."
            )

            # Check if the number of records in the new index is comparable to the number of records in the old index
            # If the difference is large something might have gone wrong.
            if (
                index_record_counts_are_similar(current_index_name, next_index_name)
                or not WARN_BIG_CHANGES
            ):
                set_alias_to_index(endpoint.search_alias, next_index_name)
                result = {
                    "status": "Success",
                    "message": f"Index {next_index_name} successfully updated with {uploaded_doc_count} records and activated.",
                }
            else:
                logger.warning(
                    "Big difference between record counts in current and next index. Not transferring alias."
                )
                result = {
                    "status": "Warning",
                    "message": f"Index {next_index_name} has big change in number of records. Index not activated. \nDid something go wrong? Check and set alias manually.",
                }

    finally:
        # Cleanup files on success; keep them on failure for debugging.
        if file_name is not None and result.get("status") == "Success":
            with contextlib.suppress(FileNotFoundError):
                file_name.unlink()
        gc.collect()  # Force garbage collection after each table
        return result


def set_alias_to_index(alias_name: str, next_index_name: str):
    api = SearchAPI()
    alias = api.get(f"aliases/{alias_name}").json()
    alias["indexes"] = [next_index_name]
    response = api.put(f"aliases/{alias_name}", alias)
    logger.info(
        f"transferred alias {alias_name} to {next_index_name}: {response.status_code} {response.reason}"
    )
    return alias


def upload_file_to_index(file_name, endpoint, index_name, batch_size=BATCH_SIZE):
    """Process the file to match the index and upload in batches and return record count"""
    api = SearchAPI()
    uploaded_doc_count = 0
    with Path(file_name).open("rb") as zf:
        data = []
        count = 0
        batch_count = 0
        for line in zf:
            count += 1
            record = json.loads(line)
            record = endpoint.transform(record)
            # Remove fields used for transform from record
            for field in endpoint.transform_source_fields:
                del record[field]
            record["@search.action"] = "mergeOrUpload"
            data.append(record)
            if count == batch_size:
                batch_count += 1
                response = api.post(
                    f"indexes('{index_name}')/docs/search.index", {"value": data}
                )
                logger.info(
                    f"Upload batch {batch_count} to {index_name}: {response.status_code} {response.reason}"
                )
                data = []
                count = 0
        if count > 0:
            response = api.post(
                f"indexes('{index_name}')/docs/search.index", {"value": data}
            )
            logger.info(
                f"Upload batch {batch_count+1} to {index_name}: {response.status_code} {response.reason}"
            )
        uploaded_doc_count = batch_count * batch_size + count
        logger.info(
            f"{batch_count} batches of {batch_size} records uploaded to {index_name}."
        )
    return uploaded_doc_count


def index_record_counts_are_similar(current_index_name: str, next_index_name: str):
    """Check if number of records on old and new index are within 10% of each other."""
    api = SearchAPI()
    current_index_stats = api.get(f"indexes/{current_index_name}/search.stats").json()
    next_index_stats = api.get(f"indexes/{next_index_name}/search.stats").json()
    current_doc_count = current_index_stats.get("documentCount")
    next_doc_count = next_index_stats.get("documentCount")
    logger.info(
        f"Current index contains {current_doc_count} records, size {current_index_stats.get('storageSize')}"
    )
    logger.info(
        f"Next index contains {next_doc_count} records, size {next_index_stats.get('storageSize')}"
    )
    return abs(current_doc_count - next_doc_count) < current_doc_count / 10


@backoff.on_predicate(backoff.fibo, lambda x: x < 0, max_value=1800)
def wait_for_index_to_fill(index_name, expected_doc_count):
    """Wait for all uploaded records to become available in the index.

    After uploading records to an Azure Search index,
    it takes some time for all records to become available.
    This backoff function queries the index until the record count is equal
    to an expected number of records.
    """
    api = SearchAPI()
    stats = api.get(f"indexes/{index_name}/search.stats").json()
    count = stats.get("documentCount")
    logger.info(
        f"Index {index_name} contains {count} records, expecting {expected_doc_count}."
    )
    return count - expected_doc_count


def export_jsonl_from_db(
    engine: Engine,
    table: DatasetTableSchema,
    scopes,
    output_path=OUTPUT_PATH,
    record_limit=RECORD_LIMIT,
) -> Path:
    """Export table from database to jsonl file in output_path
    Retries on common intermittent errors.
    """
    output_dir = Path(output_path)
    output_dir.mkdir(parents=True, exist_ok=True)

    export_definition = Export.from_table(
        table,
        filetype="jsonl",
        scopes={Scope.from_string(scope) for scope in scopes},
    )
    context = ExportContext(
        engine=engine,
        client=None,  # No storage client needed for this.
        dataset=table.schema,
        folder=output_dir,
        export=export_definition,
        size=record_limit,  # Set this for quicker debugging
    )
    failures = export_tables(context)
    if failures:
        errors = "\n".join([str(failure) for failure in failures])
        raise RuntimeError(f"Export failed with errors: {errors}")

    expected_name = context.export.table_filename(table.id)
    expected_path = output_dir / expected_name
    if expected_path.exists():
        return expected_path
    raise FileNotFoundError(
        f"Expected export file '{expected_name}' not found under '{output_dir}'"
    )


def run(selected_endpoints):
    # Configure SQLAlchemy for lower memory usage
    engine = create_engine(
        DATABASE_URL,
        pool_pre_ping=True,
        pool_size=5,  # Reduce pool size
        max_overflow=10,
        pool_recycle=3600,
        # Enable server-side cursors by default
        connect_args={"cursor_factory": None},
    )

    try:
        loader = get_schema_loader(SCHEMA_URL)
        logger.info("Loaded schemas")
    except Exception as e:
        logger.exception(f"Failed to load schemas with error {e}")
        raise

    results = []
    for endpoint_name, endpoint in SEARCH_ENDPOINTS.items():
        if endpoint_name in selected_endpoints:
            dataset_schema = loader.get_dataset(endpoint.source_dataset_id)
            result = update_search_endpoint(
                engine,
                dataset_schema,
                endpoint,
            )
            results.append(result)

            # Force garbage collection after each endpoint
            gc.collect()

    compound_message = [result["message"] for result in results]
    compound_status = SlackMessenger.Status.SUCCESS
    for result in results:
        if result["status"] == "Failure":
            compound_status = SlackMessenger.Status.FAILURE
            break
        if result["status"] == "Warning":
            compound_status = SlackMessenger.Status.WARNING
    # Raise error if one of the endpoint updates failed
    if compound_status == SlackMessenger.Status.FAILURE:
        raise RuntimeError(compound_message)
    else:
        messenger.send_slack_message(compound_message, status=compound_status)



if __name__ == "__main__":
    logger.info("Exporting")
    selected_endpoints = [SELECT_ENDPOINT] if SELECT_ENDPOINT else None
    if len(sys.argv) > 1:
        selected_endpoints = sys.argv[1:]
    # If no endpoints selected select all endpoints
    if not selected_endpoints:
        selected_endpoints = SEARCH_ENDPOINTS.keys()
    elif any(
        endpoint not in SEARCH_ENDPOINTS.keys() for endpoint in selected_endpoints
    ):
        raise RuntimeError(
            f"Endpoint '{selected_endpoints[0]}' does not exist, should be one of {SEARCH_ENDPOINTS.keys()}"
        )
    logger.info(f"Selected endpoints: {selected_endpoints}")
    try:
        run(selected_endpoints)
    except Exception as e:
        logger.error(traceback.format_exc())
        messenger.send_slack_message(
            [f"Importing data into Azure Search failed. Error: {e}"], status=SlackMessenger.Status.FAILURE
        )
    gc.collect()
