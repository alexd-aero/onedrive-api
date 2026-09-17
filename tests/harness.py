#!/usr/bin/env python3
"""
Boots app.py against tests/mock_graph.py with a pre-configured account, so the whole
app (gate + explorer + upload + delete) is exercisable offline.

  python tests/harness.py            # app on :3300, mock graph on :5999
  python tests/harness.py --port 3300 --mock 5999 --turbo
  python tests/harness.py --proxy 3400   # also run a path-prefix reverse proxy on :3400
"""
import argparse
import base64
import json
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

USER, PASSWORD = "tester", "hunter2pass"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=3300)
    ap.add_argument("--mock", type=int, default=5999)
    ap.add_argument("--mock-url", default=None,
                    help="use an already-running mock instead of starting one in-process "
                         "(so memory/throughput can be measured for the app alone)")
    ap.add_argument("--proxy", type=int, default=0)
    ap.add_argument("--turbo", action="store_true")
    ap.add_argument("--app-dir", default=None,
                    help="import app.py from here instead of the repo root (A/B benchmarking)")
    a = ap.parse_args()

    if a.turbo:
        os.environ["TURBO_UPLOAD"] = "1"
    os.environ["PORT"] = str(a.port)
    # keep the real repo's creds.yml/token.yml untouched
    os.environ.setdefault("OD_STATE_DIR", os.path.join(HERE, "_state"))
    os.makedirs(os.environ["OD_STATE_DIR"], exist_ok=True)

    from werkzeug.serving import make_server

    if a.mock_url:
        base = a.mock_url.rstrip("/")
    else:
        import mock_graph
        msrv = make_server("127.0.0.1", a.mock, mock_graph.app, threaded=True)
        threading.Thread(target=msrv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{a.mock}"

    if a.app_dir:
        sys.path.insert(0, os.path.abspath(a.app_dir))
    import app as A
    A.GRAPH = f"{base}/v1.0"
    A.AUTH_BASE = f"{base}/oauth2/v2.0"

    # ── configure creds.yml-equivalent in memory (basic method) ──
    import hashlib
    salt = base64.urlsafe_b64encode(b"0123456789abcdef").decode().rstrip("=")
    phash = A.pbkdf2(PASSWORD, salt, 100000)
    A.CREDS = {"v": 1, "method": "basic", "user": USER, "salt": salt,
               "iters": 100000, "phash": phash, "sess": "testsecret"}
    A.SESSION_SECRET = b"testsecret"
    A.TOKENCFG = {"v": 1, "method": "basic", "accounts": [
        {"id": "acct1", "email": "mock@example.com", "rt": "mock-rt"}]}
    A.ACCOUNTS = [{"id": "acct1", "email": "mock@example.com"}]
    A.RT_STORE["acct1"] = "mock-rt"
    A.ACTIVE["id"] = "acct1"
    A._UNLOCKED["ok"] = True
    A.TOKENS["refresh_token"] = "mock-rt"
    A.TOKENS["access_token"] = "mock-at"
    A.TOKENS["expires_at"] = time.time() + 3600
    A.TOKENS["email"] = "mock@example.com"

    asrv = make_server("127.0.0.1", a.port, A.app, threaded=True)
    threading.Thread(target=asrv.serve_forever, daemon=True).start()

    if a.proxy:
        start_proxy(a.proxy, a.port)

    print(json.dumps({"app": f"http://127.0.0.1:{a.port}", "mock": base,
                      "proxy": f"http://127.0.0.1:{a.proxy}/od" if a.proxy else None,
                      "user": USER, "pass": PASSWORD,
                      "turbo": A.TURBO}), flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


def start_proxy(pport, appport):
    """A deliberately hostile reverse proxy: strips TLS (serves http) and mounts the
    app under a /od path prefix — i.e. what a real nginx/Cloudflare/school proxy does."""
    import requests as rq
    from flask import Flask, Response, request as rqst
    from werkzeug.serving import make_server as mk

    p = Flask("proxy")
    up = f"http://127.0.0.1:{appport}"

    @p.route("/od", defaults={"path": ""}, methods=["GET", "POST", "PUT", "DELETE", "HEAD"])
    @p.route("/od/", defaults={"path": ""}, methods=["GET", "POST", "PUT", "DELETE", "HEAD"])
    @p.route("/od/<path:path>", methods=["GET", "POST", "PUT", "DELETE", "HEAD"])
    def fwd(path):
        url = f"{up}/{path}"
        h = {k: v for k, v in rqst.headers if k.lower() not in ("host", "content-length")}
        # a real proxy announces the original scheme this way
        h["X-Forwarded-Proto"] = "https"
        h["X-Forwarded-Prefix"] = "/od"
        r = rq.request(rqst.method, url, headers=h, data=rqst.get_data(),
                       params=rqst.args, allow_redirects=False, timeout=600)
        excl = {"content-encoding", "transfer-encoding", "connection", "content-length"}
        return Response(r.content, status=r.status_code,
                        headers=[(k, v) for k, v in r.headers.items()
                                 if k.lower() not in excl])

    srv = mk("127.0.0.1", pport, p, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()


if __name__ == "__main__":
    main()
