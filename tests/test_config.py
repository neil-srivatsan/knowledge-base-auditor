"""Tests for configuration loading."""

from kb_audit.config import Config


def test_load_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv("NOTION_API_KEY", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    # Prevent dotenv from loading the real .env
    monkeypatch.setattr("kb_audit.config.load_dotenv", lambda: None)

    cfg = Config.load()
    assert cfg.notion_api_key == ""
    assert cfg.analyzers.timestamp.warning_days == 90
    assert cfg.analyzers.timestamp.critical_days == 180
    assert cfg.analyzers.similarity.threshold == 0.80


def test_load_from_yaml(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NOTION_API_KEY", "test-key")

    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        """
notion:
  root_page_id: "abc123"
analyzers:
  timestamp:
    warning_days: 30
    critical_days: 60
  similarity:
    threshold: 0.90
"""
    )

    cfg = Config.load(config_file)
    assert cfg.notion.root_page_id == "abc123"
    assert cfg.analyzers.timestamp.warning_days == 30
    assert cfg.analyzers.timestamp.critical_days == 60
    assert cfg.analyzers.similarity.threshold == 0.90
    assert cfg.notion_api_key == "test-key"


def test_load_env_vars(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NOTION_API_KEY", "my-secret-key")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///custom.db")

    cfg = Config.load()
    assert cfg.notion_api_key == "my-secret-key"
    assert cfg.database_url == "sqlite:///custom.db"
