"""Tiny local login site for the vault-fill prototype.

Run: python server.py [port]
Serves a login form at http://localhost:<port>/ and checks POSTed credentials
against users.json. On success it shows a one-time account code the agent must
report back, which proves the real password reached the form.
Attempts are appended to attempts.log (never the password itself).
"""

from __future__ import annotations

import html
import json
import random
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

HERE = Path(__file__).resolve().parent
USERS = json.loads((HERE / "users.json").read_text(encoding="utf-8"))
LOG = HERE / "attempts.log"
WORDS = ["ORCHID", "MAPLE", "COMET", "HARBOR", "QUARTZ", "TUNDRA", "VIOLET", "EMBER"]

FORM = """<!doctype html><html><head><title>Acme Portal - Sign in</title></head>
<body><h1>Acme Portal</h1><p>Please sign in.</p>
<form method="post" action="/login">
<label>Username <input name="username" id="username" type="text" autocomplete="username"></label><br>
<label>Password <input name="password" id="password" type="password" autocomplete="current-password"></label><br>
<button type="submit">Sign in</button>
</form>{msg}</body></html>"""

WELCOME = """<!doctype html><html><head><title>Acme Portal - Dashboard</title></head>
<body><h1>Welcome, {user}!</h1><p>Your account code is <b>{code}</b>.</p></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, body: str, status: int = 200) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _log(self, **fields) -> None:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), **fields}) + "\n")

    def do_GET(self) -> None:  # noqa: N802
        self._log(event="get", path=self.path)
        self._send(FORM.format(msg=""))

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        fields = parse_qs(self.rfile.read(length).decode("utf-8"))
        user = (fields.get("username") or [""])[0]
        password = (fields.get("password") or [""])[0]
        ok = USERS.get(user) == password
        code = f"{random.choice(WORDS)}-{random.randint(1000, 9999)}" if ok else None
        self._log(event="post", username=user, password_correct=ok,
                  password_looks_like_placeholder="{{vault:" in password, code_issued=code)
        if ok:
            self._send(WELCOME.format(user=html.escape(user), code=code))
        else:
            self._send(FORM.format(msg="<p style='color:red'>Invalid username or password.</p>"), 401)

    def log_message(self, *args) -> None:  # keep stdout quiet
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    print(f"Acme test site on http://localhost:{port}/", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
