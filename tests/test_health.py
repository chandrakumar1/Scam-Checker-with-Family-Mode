from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app

client = TestClient(app)


def test_health_returns_ok():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_settings_load_from_env(monkeypatch):
    monkeypatch.setenv("APP_NAME", "RukoTest")
    assert Settings().app_name == "RukoTest"
