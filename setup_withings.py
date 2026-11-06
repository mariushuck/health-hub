"""Einmaliger Withings-Login auf dem Laptop.

uv run --env-file .env.kumpel setup_withings.py data/kumpel

Die Callback-URL muss in der Withings-App exakt so eingetragen sein wie --redirect-uri.
Ist sie nicht localhost, fragt das Skript nach der Adresse aus dem Browser. Der Code darin
gilt nur 30 Sekunden.
"""

import argparse
import os
import secrets
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from providers.withings import AUTHORIZE_URL, SCOPE, TOKEN_FILE, request_token

DEFAULT_REDIRECT_URI = "http://localhost:8765/callback"
LOGIN_TIMEOUT_S = 300


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if urlsplit(self.path).path != self.server.callback_path:
            self.send_error(404)  # etwa favicon.ico
            return
        self.server.received = self.path
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write("Fertig, das Fenster kann zu.".encode())

    def log_message(self, format: str, *args: object) -> None:
        pass  # Der Pfad enthält den Code, er gehört nicht ins Terminal.


class CallbackServer(HTTPServer):
    timeout = LOGIN_TIMEOUT_S
    received: str | None = None

    def __init__(self, port: int, callback_path: str) -> None:
        # Nur lokal lauschen: Der Callback bleibt auf dem Laptop.
        super().__init__(("127.0.0.1", port), _CallbackHandler)
        self.callback_path = callback_path

    def handle_timeout(self) -> None:
        raise SystemExit("Abbruch: Login nicht innerhalb von 5 Minuten abgeschlossen.")


def wait_for_code(server: CallbackServer, state: str) -> str:
    with server:
        while server.received is None:
            server.handle_request()
    return extract_code(server.received, state)


def extract_code(url: str, state: str) -> str:
    query = parse_qs(urlsplit(url).query)
    if not secrets.compare_digest(query.get("state", [""])[0], state):
        raise SystemExit("Abbruch: state stimmt nicht überein.")
    if "code" not in query:
        raise SystemExit(f"Abbruch: kein Code erhalten ({query.get('error', ['unbekannt'])[0]}).")
    return query["code"][0]


def main() -> None:
    parser = argparse.ArgumentParser(description="Withings-Login für eine Instanz")
    parser.add_argument("data_dir", type=Path, help="z.B. data/kumpel")
    parser.add_argument("--redirect-uri", default=DEFAULT_REDIRECT_URI)
    args = parser.parse_args()

    state = secrets.token_urlsafe(16)
    login_url = f"{AUTHORIZE_URL}?" + urlencode(
        {
            "response_type": "code",
            "client_id": os.environ["WITHINGS_CLIENT_ID"],
            "scope": SCOPE,
            "redirect_uri": args.redirect_uri,
            "state": state,
        }
    )
    target = urlsplit(args.redirect_uri)
    # Der Server lauscht schon, bevor der Browser aufgeht.
    server = (
        CallbackServer(target.port or 80, target.path or "/")
        if target.hostname in ("localhost", "127.0.0.1")
        else None
    )

    print(f"Login im Browser, falls er nicht von selbst aufgeht:\n{login_url}\n")
    webbrowser.open(login_url)
    if server:
        code = wait_for_code(server, state)
    else:
        code = extract_code(input("Adresse nach dem Login hier einfügen: ").strip(), state)

    args.data_dir.mkdir(parents=True, exist_ok=True)
    token_path = args.data_dir / TOKEN_FILE
    with httpx.Client(timeout=30) as client:
        token = request_token(
            client,
            token_path,
            {"grant_type": "authorization_code", "code": code, "redirect_uri": args.redirect_uri},
        )
    print(f"Token für Withings-Nutzer {token['userid']} gespeichert: {token_path}")
    print("Diese Datei jetzt ins Datenverzeichnis der Person auf dem Pi kopieren.")


if __name__ == "__main__":
    main()
