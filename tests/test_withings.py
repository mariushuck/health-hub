import json
import stat
import time
from datetime import date
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

import httpx
import pytest

from conftest import fixture
from providers.withings import TOKEN_FILE, WithingsError, WithingsProvider

TZ = ZoneInfo("Europe/Berlin")
START, END = date(2026, 11, 4), date(2026, 11, 6)


class FakeWithings:
    def __init__(self):
        self.measure_requests: list[dict] = []
        self.token_requests: list[dict] = []
        self.on_measure = None
        self.responses = {"token": fixture("withings/token_response.json")}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        data = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        if request.url.path == "/v2/oauth2":
            self.token_requests.append(data)
            return httpx.Response(200, json=self.responses["token"])
        assert request.url.path == "/measure"
        assert request.headers["authorization"].startswith("Bearer ")
        self.measure_requests.append(data)
        if self.on_measure:
            self.on_measure(request)
        if "measure" in self.responses:
            return httpx.Response(200, json=self.responses["measure"])
        if data.get("offset") == "2":
            return httpx.Response(200, json=fixture("withings/getmeas_page2.json"))
        return httpx.Response(200, json=fixture("withings/getmeas_page1.json"))


def _write_token(tmp_path, expires_in=3600) -> None:
    (tmp_path / TOKEN_FILE).write_text(
        json.dumps(
            {
                "access_token": "old-access",
                "refresh_token": "old-refresh",
                "expires_at": time.time() + expires_in,
            }
        )
    )


def _measure_response(*groups, updatetime="1794000000") -> dict:
    return {"status": 0, "body": {"updatetime": updatetime, "more": 0, "measuregrps": list(groups)}}


@pytest.fixture
def api():
    return FakeWithings()


@pytest.fixture
def provider(tmp_path, api):
    _write_token(tmp_path)
    return WithingsProvider(tmp_path, TZ, transport=httpx.MockTransport(api))


def _payloads(groups) -> dict:
    return {f"getmeas/{g['grpid']}": g for g in groups}


def test_parse_day_applies_unit_and_takes_latest_measure(provider):
    groups = fixture("withings/getmeas_page1.json")["body"]["measuregrps"]

    values = provider.parse_day(_payloads(reversed(groups)))

    assert values == {"weight_kg": 82.0, "fat_ratio_pct": 18.2, "muscle_mass_kg": 37.0}


def test_parse_day_ignores_ambiguous_measures(provider):
    guest = fixture("withings/getmeas_page2.json")["body"]["measuregrps"]

    assert provider.parse_day(_payloads(guest)) == {}


def test_parse_day_without_data_returns_nothing(provider):
    assert provider.parse_day({}) == {}


def test_parse_day_ignores_foreign_endpoints(provider):
    assert provider.parse_day({"other": {"date": 1, "measures": [{"type": 1, "value": 1, "unit": 0}]}}) == {}


def test_first_fetch_requests_whole_history_with_lastupdate_only(provider, api):
    provider.fetch(START, END, None)

    request = api.measure_requests[0]
    assert request["lastupdate"] == "0"
    assert request["meastypes"] == "1,6,76"
    assert request["category"] == "1"
    assert "startdate" not in request
    assert "enddate" not in request


def test_later_fetch_uses_cursor_and_ignores_window(provider, api):
    provider.fetch(START, END, 1700000000)

    assert [r["lastupdate"] for r in api.measure_requests] == ["1700000000", "1700000000"]
    assert not [r for r in api.measure_requests if "startdate" in r]


def test_fetch_pages_until_more_is_zero(provider, api):
    fetched = provider.fetch(START, END, None)

    assert [r.get("offset") for r in api.measure_requests] == [None, "2"]
    assert [r.endpoint for r in fetched.rows] == ["getmeas/11", "getmeas/12", "getmeas/13"]
    assert fetched.cursor == 1794000000


def test_cursor_comes_from_first_page(tmp_path):
    _write_token(tmp_path)
    pages = [
        {"status": 0, "body": {"updatetime": "100", "more": 1, "offset": 1, "measuregrps": []}},
        {"status": 0, "body": {"updatetime": "200", "more": 0, "measuregrps": []}},
    ]
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=pages.pop(0)))

    fetched = WithingsProvider(tmp_path, TZ, transport=transport).fetch(START, END, None)

    assert fetched.cursor == 100


def test_nothing_changed_yields_no_rows(provider, api):
    api.responses["measure"] = _measure_response()

    fetched = provider.fetch(START, END, 1700000000)

    assert fetched.rows == []
    assert fetched.cursor == 1794000000


def test_rows_use_local_day(provider, api):
    # 5.11. 00:30 in Berlin, aber noch der 4.11. in UTC
    api.responses["measure"] = _measure_response({"grpid": 1, "date": 1793835000, "measures": []})

    fetched = provider.fetch(START, END, None)

    assert fetched.rows[0].day == date(2026, 11, 5)


def test_valid_token_is_not_rotated(provider, api):
    provider.fetch(START, END, None)

    assert api.token_requests == []


def test_new_refresh_token_is_on_disk_before_any_data_call(tmp_path, api):
    _write_token(tmp_path, expires_in=10)
    provider = WithingsProvider(tmp_path, TZ, transport=httpx.MockTransport(api))
    seen = []
    api.on_measure = lambda _: seen.append(json.loads((tmp_path / TOKEN_FILE).read_text()))

    provider.fetch(START, END, None)

    assert api.token_requests[0]["grant_type"] == "refresh_token"
    assert api.token_requests[0]["refresh_token"] == "old-refresh"
    assert seen[0]["refresh_token"] == "new-refresh"
    assert seen[0]["access_token"] == "new-access"
    assert seen[0]["expires_at"] > time.time() + 10000


def test_token_survives_failed_data_call(tmp_path, api):
    _write_token(tmp_path, expires_in=10)
    provider = WithingsProvider(tmp_path, TZ, transport=httpx.MockTransport(api))

    def fail(_):
        raise httpx.ConnectError("offline")

    api.on_measure = fail

    with pytest.raises(httpx.ConnectError):
        provider.fetch(START, END, None)

    assert json.loads((tmp_path / TOKEN_FILE).read_text())["refresh_token"] == "new-refresh"


def test_failed_refresh_keeps_old_token(tmp_path, api):
    _write_token(tmp_path, expires_in=10)
    api.responses["token"] = {"status": 503, "body": {}}
    provider = WithingsProvider(tmp_path, TZ, transport=httpx.MockTransport(api))

    with pytest.raises(WithingsError):
        provider.fetch(START, END, None)

    assert json.loads((tmp_path / TOKEN_FILE).read_text())["refresh_token"] == "old-refresh"
    assert api.measure_requests == []


def test_token_file_is_private_and_leaves_no_temp_file(tmp_path, api):
    _write_token(tmp_path, expires_in=10)
    WithingsProvider(tmp_path, TZ, transport=httpx.MockTransport(api)).fetch(START, END, None)

    assert stat.S_IMODE((tmp_path / TOKEN_FILE).stat().st_mode) == 0o600
    assert [p.name for p in tmp_path.iterdir()] == [TOKEN_FILE]


def test_api_error_in_body_is_raised(provider, api):
    api.responses["measure"] = {"status": 601, "body": {}}

    with pytest.raises(WithingsError, match="601"):
        provider.fetch(START, END, None)


def test_missing_token_file_names_the_fix(tmp_path, api):
    provider = WithingsProvider(tmp_path, TZ, transport=httpx.MockTransport(api))

    with pytest.raises(WithingsError, match="setup_withings"):
        provider.fetch(START, END, None)
