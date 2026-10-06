"""Telegram alerts: best effort, never raise, never log the token."""

from pathlib import Path
from typing import Any

import pytest
import requests

from pricefc import alerts

TOKEN = "123456:ABC-secret_token"


@pytest.fixture
def secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("PRICEFC_SECRETS_DIR", str(tmp_path))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setenv("PRICEFC_ENV", "staging")
    (tmp_path / "telegram_bot_token").write_text(TOKEN + "\n")
    (tmp_path / "telegram_chat_id").write_text("-10042\n")
    return tmp_path


class FakePost:
    def __init__(self, status: int = 200, exc: Exception | None = None) -> None:
        self.status, self.exc = status, exc
        self.calls: list[dict[str, Any]] = []

    def __call__(self, url: str, **kwargs: Any) -> Any:
        self.calls.append({"url": url, **kwargs})
        if self.exc:
            raise self.exc
        return type("Resp", (), {"status_code": self.status})()


def test_sends_plain_text_prefixed_with_environment(
    secrets: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    post = FakePost()
    monkeypatch.setattr(alerts.requests, "post", post)
    assert alerts.send_telegram("forecast-daily Failed") is True
    (call,) = post.calls
    assert call["url"] == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert call["data"]["chat_id"] == "-10042"
    assert call["data"]["text"] == "[staging] forecast-daily Failed"
    assert call["timeout"] == 10 and "parse_mode" not in call["data"]


def test_missing_secret_skips(secrets: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (secrets / "telegram_chat_id").unlink()
    post = FakePost()
    monkeypatch.setattr(alerts.requests, "post", post)
    assert alerts.send_telegram("x") is False
    assert post.calls == []


@pytest.mark.parametrize(
    "post",
    [FakePost(status=401), FakePost(exc=requests.ConnectionError(f"bot{TOKEN} unreachable"))],
)
def test_failures_return_false_without_leaking_the_token(
    secrets: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    post: FakePost,
) -> None:
    monkeypatch.setattr(alerts.requests, "post", post)
    assert alerts.send_telegram("x") is False
    out = capsys.readouterr()
    assert "ABC-secret" not in out.out + out.err


def test_tokens_are_redacted_from_the_message(
    secrets: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    post = FakePost()
    monkeypatch.setattr(alerts.requests, "post", post)
    alerts.send_telegram(f"failed: GET https://x/bot{TOKEN}/y?securityToken=abc&a=1")
    text = post.calls[0]["data"]["text"]
    assert "ABC-secret" not in text and "abc" not in text and "a=1" in text
