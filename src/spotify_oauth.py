"""Spotify Authorization Code OAuth flow — reusable by bootstrap + setup wizard."""

from __future__ import annotations

import secrets
import sys
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

REDIRECT_HOST = "127.0.0.1"
REDIRECT_PORT = 8765
REDIRECT_URI = f"http://{REDIRECT_HOST}:{REDIRECT_PORT}/callback"
AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
SCOPES = (
    "playlist-read-private playlist-read-collaborative "
    "playlist-modify-public playlist-modify-private"
)


class _Handler(BaseHTTPRequestHandler):
    captured: dict[str, str] = {}

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        qs = urllib.parse.parse_qs(parsed.query)
        if "code" in qs:
            _Handler.captured["code"] = qs["code"][0]
            _Handler.captured["state"] = qs.get("state", [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"OK. You can close this tab.")
        else:
            err = qs.get("error", ["unknown"])[0]
            _Handler.captured["error"] = err
            self.send_response(400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"Error: {err}".encode())

    def log_message(self, *args, **kwargs):
        pass


def fetch_refresh_token(client_id: str, client_secret: str) -> str:
    """Run the OAuth dance: open browser → capture code → exchange → refresh_token."""
    _Handler.captured = {}
    state = secrets.token_urlsafe(16)
    authorize_url = f"{AUTH_URL}?" + urllib.parse.urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPES,
            "state": state,
            "show_dialog": "true",
        }
    )

    server = HTTPServer((REDIRECT_HOST, REDIRECT_PORT), _Handler)
    print(f"已在浏览器打开授权页: {authorize_url}", file=sys.stderr)
    webbrowser.open(authorize_url)

    while "code" not in _Handler.captured and "error" not in _Handler.captured:
        server.handle_request()
    server.server_close()

    if "error" in _Handler.captured:
        raise RuntimeError(f"OAuth 失败: {_Handler.captured['error']}")
    if _Handler.captured.get("state") != state:
        raise RuntimeError("state 不匹配（可能是 CSRF）")

    resp = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": _Handler.captured["code"],
            "redirect_uri": REDIRECT_URI,
        },
        auth=(client_id, client_secret),
        timeout=30,
    )
    resp.raise_for_status()
    refresh_token = resp.json().get("refresh_token")
    if not refresh_token:
        raise RuntimeError("返回里没有 refresh_token")
    return refresh_token
