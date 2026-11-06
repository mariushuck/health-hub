import threading
from urllib.request import urlopen
from urllib.error import HTTPError

import httpx
import pytest

from conftest import fixture
from providers.withings import TOKEN_FILE, request_token
from setup_withings import CallbackServer, extract_code, wait_for_code


def test_extract_code_returns_code_for_matching_state():
    assert extract_code("/callback?code=abc&state=s1", "s1") == "abc"


@pytest.mark.parametrize(
    "url",
    [
        "/callback?code=abc&state=other",  # CSRF
        "/callback?code=abc",
        "/callback?error=access_denied&state=s1",
    ],
)
def test_extract_code_rejects_bad_callbacks(url):
    with pytest.raises(SystemExit):
        extract_code(url, "s1")


def test_callback_server_ignores_other_paths_and_returns_code():
    server = CallbackServer(0, "/callback")
    port = server.server_address[1]
    assert server.server_address[0] == "127.0.0.1"
    result = []
    thread = threading.Thread(target=lambda: result.append(wait_for_code(server, "s1")))
    thread.start()

    with pytest.raises(HTTPError):
        urlopen(f"http://127.0.0.1:{port}/favicon.ico")
    with urlopen(f"http://127.0.0.1:{port}/callback?code=abc&state=s1") as response:
        assert response.status == 200

    thread.join(timeout=5)
    assert result == ["abc"]


def test_code_exchange_writes_token_file(tmp_path):
    seen = []

    def handler(request):
        seen.append(request.content.decode())
        return httpx.Response(200, json=fixture("withings/token_response.json"))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        token = request_token(
            client,
            tmp_path / TOKEN_FILE,
            {"grant_type": "authorization_code", "code": "abc", "redirect_uri": "http://localhost"},
        )

    assert "action=requesttoken" in seen[0]
    assert "grant_type=authorization_code" in seen[0]
    assert "code=abc" in seen[0]
    assert "client_secret=client-secret" in seen[0]
    assert token["refresh_token"] == "new-refresh"
    assert (tmp_path / TOKEN_FILE).exists()
