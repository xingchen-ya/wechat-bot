import json

from scripts import bootstrap_infrastructure as bootstrap


def test_bootstrap_creates_once_and_preserves_existing_config(tmp_path, monkeypatch):
    config = tmp_path / "data" / "app_config.json"
    monkeypatch.setattr(bootstrap, "get_app_config_path", lambda: str(config))
    bootstrap.main()
    payload = json.loads(config.read_text(encoding="utf-8"))
    assert payload["bot"]["storage_backend"] == "postgres"
    assert payload["bot"]["scheduled_messages_enabled"] is False
    assert "WECHAT_BOT_DATABASE_URL" in config.read_text(encoding="utf-8")
    original = config.read_bytes()
    bootstrap.main()
    assert config.read_bytes() == original
