from pathlib import Path

import pytest

from pricefc.secrets import get_secret


@pytest.fixture
def secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("PRICEFC_SECRETS_DIR", str(tmp_path))
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    return tmp_path


def test_reads_rendered_file(secrets: Path) -> None:
    (secrets / "telegram_chat_id").write_text("-100\n")
    assert get_secret("telegram_chat_id") == "-100"


def test_env_overrides_file(secrets: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (secrets / "telegram_chat_id").write_text("-100")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-200")
    assert get_secret("telegram_chat_id") == "-200"


def test_missing_or_empty_is_none(secrets: Path) -> None:
    assert get_secret("telegram_chat_id") is None
    (secrets / "telegram_chat_id").write_text("")  # agent rendered before the secret existed
    assert get_secret("telegram_chat_id") is None
