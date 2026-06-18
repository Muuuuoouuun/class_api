from classin_toolkit.storage.notion_setup import create_notion_schema, dry_run_schema


def test_dry_run_schema_lists_five_databases() -> None:
    schema = dry_run_schema("테스트")

    assert [name for name, _props in schema] == [
        "테스트 - 학생 Master",
        "테스트 - 수업 기록",
        "테스트 - 리포트",
        "테스트 - 메모",
        "테스트 - 시험",
    ]
    assert "학생명" in schema[0][1]
    assert "수업일시" in schema[1][1]
    assert "리포트 기간" in schema[2][1]
    assert "내용" in schema[3][1]
    assert "시험명" in schema[4][1]
    assert "응시 여부" in schema[4][1]


def test_create_notion_schema_creates_relation_databases_in_order() -> None:
    client = FakeNotionClient()

    result = create_notion_schema(
        token="secret_test",
        parent_page_id="parent-page",
        prefix="ClassIn Demo",
        client=client,
    )

    assert result.students == "db_1"
    assert result.lessons == "db_2"
    assert result.reports == "db_3"
    assert result.memos == "db_4"
    assert result.exams == "db_5"
    assert (
        client.created[4]["initial_data_source"]["properties"]["학생"]["relation"][
            "data_source_id"
        ]
        == "db_1"
    )
    assert 'exams: "db_5"' in result.config_snippet()
    relation = client.created[1]["initial_data_source"]["properties"]["학생"]["relation"]
    assert relation["data_source_id"] == "db_1"
    assert relation["type"] == "single_property"
    assert relation["single_property"] == {}
    assert client.created[2]["initial_data_source"]["properties"]["학생"]["relation"][
        "data_source_id"
    ] == "db_1"
    assert client.created[3]["initial_data_source"]["properties"]["학생"]["relation"][
        "data_source_id"
    ] == "db_1"
    assert 'students: "db_1"' in result.config_snippet()


class FakeNotionClient:
    def __init__(self) -> None:
        self.created = []
        self.databases = self

    def create(self, **kwargs):
        self.created.append(kwargs)
        return {
            "id": f"container_{len(self.created)}",
            "data_sources": [{"id": f"db_{len(self.created)}"}],
        }
