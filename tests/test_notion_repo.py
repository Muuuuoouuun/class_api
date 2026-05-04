from typing import Any

from classin_toolkit.storage.notion_repo import NotionRepo


def test_query_database_uses_legacy_databases_query_when_available() -> None:
    repo = _repo()
    client = LegacyNotionClient()
    repo._nc = client

    res = repo._query_database(database_id="db-1", filter={"property": "x"})

    assert res == {"results": [], "has_more": False}
    assert client.databases.calls == [("db-1", {"filter": {"property": "x"}})]


def test_query_database_resolves_data_source_for_current_notion_sdk() -> None:
    repo = _repo()
    client = DataSourceNotionClient()
    repo._nc = client

    res = repo._query_database(database_id="db-1", page_size=10)

    assert res == {"results": [{"id": "page-1"}], "has_more": False}
    assert client.databases.retrieved == ["db-1"]
    assert client.data_sources.calls == [("source-1", {"page_size": 10})]


def _repo() -> NotionRepo:
    return NotionRepo(
        token="secret_test",
        students_db="students",
        lessons_db="lessons",
        reports_db="reports",
    )


class LegacyNotionClient:
    def __init__(self) -> None:
        self.databases = LegacyDatabasesEndpoint()


class LegacyDatabasesEndpoint:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def query(self, *, database_id: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((database_id, kwargs))
        return {"results": [], "has_more": False}


class DataSourceNotionClient:
    def __init__(self) -> None:
        self.databases = DataSourceDatabasesEndpoint()
        self.data_sources = DataSourcesEndpoint()


class DataSourceDatabasesEndpoint:
    def __init__(self) -> None:
        self.retrieved: list[str] = []

    def retrieve(self, *, database_id: str) -> dict[str, Any]:
        self.retrieved.append(database_id)
        return {"data_sources": [{"id": "source-1"}]}


class DataSourcesEndpoint:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def query(self, *, data_source_id: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((data_source_id, kwargs))
        return {"results": [{"id": "page-1"}], "has_more": False}
