import pytest
from fastapi.testclient import TestClient
from vartija_core.settings import Settings

from ruletrace.app import create_app

ENG = {"X-User": "mikko", "X-Roles": "engineer"}
CEO = {"X-User": "sanna", "X-Roles": "ceo"}


@pytest.fixture
def client(tmp_path):
    s = Settings(database_url=f"sqlite:///{tmp_path}/t.db", event_secret="test", llm_backend="rules",
                 upload_dir=tmp_path / "data")
    with TestClient(create_app(s)) as c:
        yield c
