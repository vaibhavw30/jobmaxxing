import pytest

from jobmaxxing.discovery.gmail_source import load_gmail_config


def test_config_reads_required_and_defaults(monkeypatch):
    monkeypatch.setenv("GMAIL_ADDRESS", "me@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "app-pw")
    monkeypatch.delenv("GMAIL_ALERT_SENDER", raising=False)
    monkeypatch.delenv("GMAIL_SINCE_DAYS", raising=False)
    monkeypatch.delenv("GMAIL_IMAP_HOST", raising=False)
    cfg = load_gmail_config()
    assert cfg["address"] == "me@gmail.com"
    assert cfg["app_password"] == "app-pw"
    assert cfg["sender"] == "jobalerts-noreply@linkedin.com"
    assert cfg["since_days"] == 7
    assert cfg["host"] == "imap.gmail.com"


def test_config_missing_credentials_raises(monkeypatch):
    monkeypatch.delenv("GMAIL_ADDRESS", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    with pytest.raises(RuntimeError):
        load_gmail_config()
