"""First browser authorization for CFO.ai, saving its rotating credential to Actions."""

import base64
import hashlib
import json
import re
import secrets
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

from . import check
from .cfo_actions import MCP, TOKEN


def _json_request(url, fields, step, *, form=False):
    data = urllib.parse.urlencode(fields) if form else json.dumps(fields)
    content_type = "application/x-www-form-urlencoded" if form else "application/json"
    try:
        response = subprocess.run(
            ["curl", "--silent", "--show-error", "--max-time", "30", "--request", "POST",
             "--header", f"Content-Type: {content_type}", "--header", "Accept: application/json",
             "--data-binary", "@-",
             "--write-out", "\nHTTP_STATUS:%{http_code}", url],
            input=data, text=True, capture_output=True,
        )
    except OSError:
        raise RuntimeError(f"CFO.ai {step} failed: curl is unavailable") from None
    body, marker, status = response.stdout.rpartition("\nHTTP_STATUS:")
    if response.returncode or not marker:
        raise RuntimeError(f"CFO.ai {step} failed (curl exit {response.returncode})")
    try:
        result = json.loads(body)
    except ValueError:
        result = None
    if status not in ("200", "201"):
        details = []
        if isinstance(result, dict):
            for key in ("error", "error_description"):
                value = result.get(key)
                if isinstance(value, str):
                    for secret in (fields.get("code"), fields.get("code_verifier")):
                        if secret:
                            value = value.replace(secret, "[redacted]")
                    details.append(re.sub(r"[A-Za-z0-9_-]{24,}", "[redacted]", " ".join(value.split()))[:200])
        raise RuntimeError(f"CFO.ai {step} failed (HTTP {status})" + (": " + "; ".join(details) if details else ""))
    if not isinstance(result, dict):
        raise RuntimeError(f"CFO.ai {step} returned no JSON object")
    return result


def _set_secret(repo, name, value):
    try:
        result = subprocess.run(["gh", "secret", "set", name, "--repo", repo], input=value,
                                text=True, capture_output=True, check=False)
    except OSError:
        raise RuntimeError("gh is unavailable; install and authenticate GitHub CLI") from None
    if result.returncode:
        raise RuntimeError(f"cannot write Actions secrets in {repo} (gh exit {result.returncode})")


def _listener(port):
    class Callback(BaseHTTPRequestHandler):
        result = None

        def do_GET(self):
            query = urllib.parse.urlparse(self.path)
            if query.path != "/callback":
                self.send_error(404)
                return
            values = urllib.parse.parse_qs(query.query)
            if values.get("state") != [state]:
                type(self).result = (None, "callback state did not match")
            elif "error" in values:
                type(self).result = (None, "CFO.ai denied sign-in: " + re.sub(r"[^a-zA-Z_]", "", values["error"][0])[:40])
            elif len(values.get("code", [])) != 1:
                type(self).result = (None, "CFO.ai callback had no authorization code")
            else:
                type(self).result = (values["code"][0], None)
            body = b"<h1>Sign-in received. Return to the terminal.</h1>" if type(self).result[0] else b"<h1>Sign-in failed. Return to the terminal.</h1>"
            self.send_response(200 if type(self).result[0] else 400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    state = secrets.token_urlsafe(32)
    return HTTPServer(("127.0.0.1", port), Callback), state, Callback


def login(args):
    if not 1 <= args.timeout:
        raise ValueError("--timeout must be positive")
    if bool(args.client_id) != (args.port is not None):
        raise ValueError("--client-id and its registered --port must be given together")
    if args.port is not None and not 1 <= args.port <= 65535:
        raise ValueError("--port must be between 1 and 65535")
    if any(not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) for repo in args.repo):
        raise ValueError("every --repo must be owner/name")

    port = args.port or 8765
    try:
        server, state, callback = _listener(port)
    except OSError:
        if args.client_id:
            raise RuntimeError(f"port {port} is unavailable; --client-id requires its exact registered port") from None
        server, state, callback = _listener(0)
        port = server.server_address[1]
    print(f"Loopback port: {port}", flush=True)
    redirect_uri = f"http://127.0.0.1:{port}/callback"
    try:
        for repo in dict.fromkeys(args.repo):
            _set_secret(repo, "CFO_SECRET_WRITE_PREFLIGHT", "ok")
        print("GitHub Actions secret access checked.", flush=True)

        # Discover endpoint names from the same authorization server used by refresh.
        issuer = TOKEN.split("/oauth2/token", 1)[0]
        try:
            with urllib.request.urlopen(issuer + "/.well-known/oauth-authorization-server", timeout=30) as response:
                metadata = json.load(response)
            authorize = metadata["authorization_endpoint"]
            register = metadata["registration_endpoint"]
        except (urllib.error.URLError, KeyError, ValueError) as error:
            raise RuntimeError("could not read CFO.ai OAuth endpoint metadata") from None

        client_id = args.client_id
        if not client_id:
            registered = _json_request(register, {
                "client_name": "Prism CLI", "redirect_uris": [redirect_uri],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"], "token_endpoint_auth_method": "none",
            }, "registration")
            client_id = registered.get("client_id")
            if not isinstance(client_id, str) or not client_id:
                raise RuntimeError("CFO.ai registration returned no client ID")
        print(f"Public client ID: {client_id}", flush=True)

        verifier = secrets.token_urlsafe(32)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        link = authorize + "?" + urllib.parse.urlencode({
            "response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri,
            "scope": "openid profile email offline_access", "state": state,
            "nonce": secrets.token_urlsafe(32), "resource": MCP,
            "code_challenge": challenge, "code_challenge_method": "S256",
        })
        print(f"Sign in to app.cfo.ai in this browser, then approve: {check.link(link)}", flush=True)
        try:
            webbrowser.open(link)
        except Exception:
            pass

        deadline = time.monotonic() + args.timeout
        while callback.result is None and time.monotonic() < deadline:
            server.timeout = min(1, max(0, deadline - time.monotonic()))
            server.handle_request()
        if callback.result is None:
            raise RuntimeError(f"timed out waiting for CFO.ai callback after {args.timeout} seconds")
        code, error = callback.result
        if error:
            raise RuntimeError(error)
        print("CFO.ai callback accepted.", flush=True)
    finally:
        server.server_close()

    result = _json_request(TOKEN, {
        "grant_type": "authorization_code", "client_id": client_id, "code": code,
        "redirect_uri": redirect_uri, "code_verifier": verifier,
    }, "code exchange", form=True)
    refresh_token = result.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise RuntimeError("CFO.ai exchange returned no refresh token")
    print("Authorization code exchanged.", flush=True)
    for repo in dict.fromkeys(args.repo):
        try:
            _set_secret(repo, "CFO_OAUTH_CLIENT_ID", client_id)
            _set_secret(repo, "CFO_OAUTH_REFRESH_TOKEN", refresh_token)
        except RuntimeError as error:
            raise RuntimeError(f"{error}; a new sign-in is needed") from None
    print("CFO.ai login saved to all requested repositories.", flush=True)
    return client_id, refresh_token
