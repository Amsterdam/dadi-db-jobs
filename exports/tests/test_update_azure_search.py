import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests


@pytest.mark.parametrize(
    ("geometry", "coordinates"),
    [
        ({"coordinates": [4.9, 52.3]}, (4.9, 52.3)),
        (None, (None, None)),
        ({}, (None, None)),
    ],
)
def test_bag_transform(search_module, geometry, coordinates):
    record = {
        "huisnummer": 12,
        "verblijfsobjectGeometrie": geometry,
        "panden": None,
        "gebruiksdoelOmschrijvingen": ["wonen"],
        "standplaatsGebruiksdoelOmschrijvingen": [],
        "ligplaatsGebruiksdoelOmschrijvingen": None,
    }

    result = search_module.bag_transform(record)

    assert result["huisnummerStr"] == "12"
    assert (result["longitude"], result["latitude"]) == coordinates
    assert result["panden"] == []
    assert result["gebruiksdoelOmschrijvingen"] == ["wonen"]
    assert result["standplaatsGebruiksdoelOmschrijvingen"] == []
    assert result["ligplaatsGebruiksdoelOmschrijvingen"] == []


def test_brk_transform(search_module):
    assert search_module.brk_transform({"id": "123.45.6"}) == {"id": "123456"}


@pytest.mark.parametrize("activities", [None, [], ["retail"]])
def test_hr_transform(search_module, activities):
    assert search_module.hr_transform({"activiteitenVes": activities}) == {
        "activiteitenVes": activities or []
    }


@pytest.mark.parametrize("method", ["get", "post", "put", "delete"])
def test_search_api_request(search_module, monkeypatch, method):
    request = Mock()
    monkeypatch.setattr(search_module.requests, method, request)
    api = search_module.SearchAPI(url="https://search.example.test/", version="test")
    monkeypatch.setattr(
        api, "get_authorization_header", Mock(return_value={"api-key": "test"})
    )
    kwargs = {"json": {"name": "new-index"}} if method in ("post", "put") else {}

    response = getattr(api, method)("indexes", **kwargs)

    assert response is request.return_value
    request.assert_called_once_with(
        url="https://search.example.test/indexes",
        params={"api-version": "test"},
        headers={"api-key": "test"},
        **kwargs,
    )
    if method != "delete":
        response.raise_for_status.assert_called_once_with()


@pytest.mark.parametrize("method", ["get", "post", "put"])
def test_search_api_http_errors(search_module, monkeypatch, method):
    request = Mock()
    request.return_value.raise_for_status.side_effect = requests.HTTPError(
        "bad request"
    )
    monkeypatch.setattr(search_module.requests, method, request)
    api = search_module.SearchAPI()
    monkeypatch.setattr(api, "get_authorization_header", Mock(return_value={}))
    kwargs = {"json": {}} if method != "get" else {}

    with pytest.raises(requests.HTTPError, match="bad request"):
        getattr(api, method)("indexes", **kwargs)

    request.return_value.raise_for_status.reset_mock()
    getattr(api, method)("indexes", raise_for_status=False, **kwargs)
    request.return_value.raise_for_status.assert_not_called()


@pytest.mark.parametrize("record_count", [0, 2, 3])
def test_upload_batches_and_transforms(
    search_module, monkeypatch, tmp_path, record_count
):
    api = Mock()
    monkeypatch.setattr(search_module, "SearchAPI", Mock(return_value=api))
    endpoint = SimpleNamespace(
        transform=lambda record: record | {"id": record["id"].replace(".", "")},
        transform_source_fields=["helper"],
    )
    path = tmp_path / "records.jsonl"
    path.write_text(
        "".join(
            json.dumps({"id": f"{i}.1", "helper": "remove"}) + "\n"
            for i in range(record_count)
        )
    )

    assert (
        search_module.upload_file_to_index(path, endpoint, "next", batch_size=2)
        == record_count
    )
    assert api.post.call_count == (record_count + 1) // 2
    uploaded = []
    for call in api.post.call_args_list:
        assert call.args[0] == "indexes('next')/docs/search.index"
        batch = call.args[1]["value"]
        assert 1 <= len(batch) <= 2
        uploaded.extend(batch)
    assert uploaded == [
        {"id": f"{i}1", "@search.action": "mergeOrUpload"} for i in range(record_count)
    ]


@pytest.mark.parametrize(
    ("current", "next_count", "similar"),
    [
        (100, 91, True),
        (100, 90, False),
        (100, 110, False),
        (100, 109, True),
        (0, 0, False),
    ],
)
def test_index_count_threshold(
    search_module, monkeypatch, current, next_count, similar
):
    api = Mock()
    api.get.return_value.json.side_effect = [
        {"documentCount": current},
        {"documentCount": next_count},
    ]
    monkeypatch.setattr(search_module, "SearchAPI", Mock(return_value=api))

    assert search_module.index_record_counts_are_similar("old", "new") is similar


def test_set_alias_to_index(search_module, monkeypatch):
    api = Mock()
    api.get.return_value.json.return_value = {"name": "alias", "indexes": ["old"]}
    monkeypatch.setattr(search_module, "SearchAPI", Mock(return_value=api))

    result = search_module.set_alias_to_index("alias", "new")

    assert result == {"name": "alias", "indexes": ["new"]}
    api.put.assert_called_once_with("aliases/alias", result)


@pytest.mark.parametrize("count", [10, 11])
def test_wait_for_index_already_filled(search_module, monkeypatch, count):
    api = Mock()
    api.get.return_value.json.return_value = {"documentCount": count}
    monkeypatch.setattr(search_module, "SearchAPI", Mock(return_value=api))

    assert search_module.wait_for_index_to_fill("new", 10) == count - 10
    api.get.assert_called_once_with("indexes/new/search.stats")


@pytest.mark.parametrize("outcome", ["success", "failure", "missing"])
def test_export_jsonl_from_db(search_module, monkeypatch, tmp_path, outcome):
    table = SimpleNamespace(id="records", schema=object())
    definition = Mock()
    definition.table_filename.return_value = "records.jsonl"
    from_table = Mock(return_value=definition)
    monkeypatch.setattr(search_module.Export, "from_table", from_table)
    context_factory = Mock()
    context_factory.return_value.export = definition
    monkeypatch.setattr(search_module, "ExportContext", context_factory)
    export_tables = Mock(return_value=["broken export"] if outcome == "failure" else [])
    monkeypatch.setattr(search_module, "export_tables", export_tables)
    output = tmp_path / "output"
    expected = output / "records.jsonl"
    if outcome == "success":
        output.mkdir()
        expected.write_text("{}\n")
    engine = Mock()

    if outcome == "failure":
        with pytest.raises(
            RuntimeError, match="Export failed with errors: broken export"
        ):
            search_module.export_jsonl_from_db(engine, table, ["OPENBAAR"], output, 5)
    elif outcome == "missing":
        with pytest.raises(FileNotFoundError, match="records.jsonl"):
            search_module.export_jsonl_from_db(engine, table, ["OPENBAAR"], output, 5)
    else:
        assert (
            search_module.export_jsonl_from_db(engine, table, ["OPENBAAR"], output, 5)
            == expected
        )

    assert output.is_dir()
    from_table.assert_called_once_with(
        table, filetype="jsonl", scopes={search_module.Scope.from_string("OPENBAAR")}
    )
    context_factory.assert_called_once_with(
        engine=engine,
        client=None,
        dataset=table.schema,
        folder=output,
        export=definition,
        size=5,
    )
    export_tables.assert_called_once_with(context_factory.return_value)


@pytest.mark.parametrize(
    ("outcome", "expected_status", "activated"),
    [
        ("success", "Success", True),
        ("export_failure", "Failure", False),
        ("incomplete", "Failure", False),
        ("large_change", "Warning", False),
        ("warnings_disabled", "Success", True),
    ],
)
def test_update_endpoint_safeguards(
    search_module, monkeypatch, tmp_path, outcome, expected_status, activated
):
    endpoint = search_module.SEARCH_ENDPOINTS["benkagg-brkbasisdataselectie"]
    current, next_index = sorted(endpoint.indexes)
    api = Mock()
    api.get.return_value.json.side_effect = [
        {"indexes": [current]},
        {"fields": [{"name": "id"}]},
        {"value": [{"name": next_index}]},
    ]
    monkeypatch.setattr(search_module, "SearchAPI", Mock(return_value=api))
    table = SimpleNamespace(
        db_name="records",
        fields=[SimpleNamespace(id="id"), SimpleNamespace(id="unused")],
    )
    schema = Mock()
    schema.get_table_by_id.return_value = table
    path = tmp_path / "records.jsonl"
    path.write_text("{}\n")
    export = Mock(return_value=path)
    if outcome == "export_failure":
        export.side_effect = RuntimeError("export unavailable")
    monkeypatch.setattr(search_module, "export_jsonl_from_db", export)
    monkeypatch.setattr(search_module, "upload_file_to_index", Mock(return_value=100))
    monkeypatch.setattr(
        search_module,
        "wait_for_index_to_fill",
        Mock(return_value=-1 if outcome == "incomplete" else 0),
    )
    monkeypatch.setattr(
        search_module,
        "index_record_counts_are_similar",
        Mock(return_value=outcome not in ("large_change", "warnings_disabled")),
    )
    monkeypatch.setattr(
        search_module, "WARN_BIG_CHANGES", outcome != "warnings_disabled"
    )
    activate = Mock()
    monkeypatch.setattr(search_module, "set_alias_to_index", activate)

    result = search_module.update_search_endpoint(Mock(), schema, endpoint)

    assert result["status"] == expected_status
    assert result["message"]
    assert [field.id for field in table.fields] == ["id"]
    api.delete.assert_called_once_with(f"indexes/{next_index}")
    assert path.exists() is (expected_status != "Success")
    if activated:
        activate.assert_called_once_with(endpoint.search_alias, next_index)
    else:
        activate.assert_not_called()


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        (["Success", "Success"], "SUCCESS"),
        (["Success", "Warning"], "WARNING"),
        (["Warning", "Failure"], "FAILURE"),
        (["Failure", "Warning"], "FAILURE"),
    ],
)
def test_run_aggregates_selected_endpoints(
    search_module, monkeypatch, statuses, expected
):
    engine = Mock()
    monkeypatch.setattr(search_module, "create_engine", Mock(return_value=engine))
    loader = Mock()
    monkeypatch.setattr(search_module, "get_schema_loader", Mock(return_value=loader))
    selected = list(search_module.SEARCH_ENDPOINTS)[:2]
    update = Mock(
        side_effect=[
            {"status": status, "message": f"result {i}"}
            for i, status in enumerate(statuses)
        ]
    )
    monkeypatch.setattr(search_module, "update_search_endpoint", update)
    notify = Mock()
    monkeypatch.setattr(search_module.messenger, "send_slack_message", notify)
    messages = ["result 0", "result 1"]

    if expected == "FAILURE":
        with pytest.raises(RuntimeError) as exc_info:
            search_module.run(selected)
        assert exc_info.value.args == (messages,)
        notify.assert_not_called()
    else:
        search_module.run(selected)
        notify.assert_called_once_with(
            messages, status=getattr(search_module.SlackMessenger.Status, expected)
        )

    assert update.call_count == 2
    assert [call.args[2] for call in update.call_args_list] == [
        search_module.SEARCH_ENDPOINTS[name] for name in selected
    ]
    for call in update.call_args_list:
        assert call.args[:2] == (engine, loader.get_dataset.return_value)


def test_run_propagates_schema_loader_failure(search_module, monkeypatch):
    monkeypatch.setattr(search_module, "create_engine", Mock())
    monkeypatch.setattr(
        search_module,
        "get_schema_loader",
        Mock(side_effect=RuntimeError("schemas unavailable")),
    )
    update = Mock()
    monkeypatch.setattr(search_module, "update_search_endpoint", update)

    with pytest.raises(RuntimeError, match="schemas unavailable"):
        search_module.run(list(search_module.SEARCH_ENDPOINTS))

    update.assert_not_called()
