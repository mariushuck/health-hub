import base64
import json
import time
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def fake_jwt() -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": time.time() + 86400}).encode())
    return f"x.{payload.decode().rstrip('=')}.y"


class FakeResponse:
    def __init__(self, body=None, status_code=200, text=None):
        self.status_code = status_code
        self.ok = status_code < 400
        self.text = text if text is not None else json.dumps(body)
        self.content = b"" if body is None and text is None else self.text.encode()

    def json(self):
        return json.loads(self.text)


class FakeGarminSession:
    """Antwortet wie Garmin. So läuft der echte Client mit seinen echten Pfaden."""

    ROUTES = {
        "/sleep-service/sleep/dailySleepSummariesByDate": "sleep_summaries",
        "/hrv-service/hrv/daily/": "hrv_summaries",
        "/usersummary-service/v2/dailysummaries": "daily_summaries",
        "/weight-service/weight/range/": "weight_range",
        "/activitylist-service/activities/search/activities": "activities",
        "/wellness-service/wellness/dailyHeartRate": "daily_heart_rate",
    }

    def __init__(self):
        self.gets: list[tuple[str, dict | None]] = []
        self.token_requests = 0
        self.failures: list[int] = []  # Statuscodes, die die nächsten GETs beantworten
        self.overrides: dict[str, object] = {}
        self.fail_when = None  # (url, params) -> Statuscode oder None

    def post(self, url, headers=None, timeout=None):
        self.token_requests += 1
        return FakeResponse(text=fake_jwt())

    def get(self, url, headers=None, params=None, timeout=None):
        self.gets.append((url, params))
        if self.failures:
            return FakeResponse(status_code=self.failures.pop(0), text="fail")
        if self.fail_when and (status := self.fail_when(url, params)):
            return FakeResponse(status_code=status, text="fail")
        for path, name in self.ROUTES.items():
            if path in url:
                body = self.overrides.get(name, fixture(f"garmin/{name}.json"))
                if callable(body):
                    body = body(params)
                return FakeResponse(body)
        raise AssertionError(f"Unerwarteter Aufruf: {url}")

    def close(self):
        pass


@pytest.fixture
def garmin_session(monkeypatch):
    monkeypatch.setenv("GARMIN_PERSONAL_API_KEY", "test-key")
    return FakeGarminSession()


@pytest.fixture(autouse=True)
def withings_env(monkeypatch):
    monkeypatch.setenv("WITHINGS_CLIENT_ID", "client-id")
    monkeypatch.setenv("WITHINGS_CLIENT_SECRET", "client-secret")
