#!/usr/bin/env python3
"""
Server-side contract tests for the fixes: throttle retries, the single-shot small-file
upload, batch delete, streaming proxy fidelity, reverse-proxy header handling and name
escaping.

Needs the harness running:
    ./tests/restart.sh --port 3300 --mock-url http://127.0.0.1:5999 --proxy 3400
    python tests/test_server.py
"""
import argparse
import hashlib
import os
import sys
import time

import requests

ap = argparse.ArgumentParser()
ap.add_argument("--app", default="http://127.0.0.1:3300")
ap.add_argument("--mock", default="http://127.0.0.1:5999")
ap.add_argument("--user", default="tester")
ap.add_argument("--password", default="hunter2pass")
a = ap.parse_args()

MiB = 1024 * 1024
PASS = FAIL = 0


def ok(name, cond, extra=""):
    global PASS, FAIL
    print(f"  {'[OK]' if cond else '[XX]'} {name}{('  ' + str(extra)) if extra else ''}")
    if cond:
        PASS += 1
    else:
        FAIL += 1
    return cond


def reset(**cfg):
    requests.post(f"{a.mock}/__reset", timeout=30)
    if cfg:
        requests.post(f"{a.mock}/__config", json=cfg, timeout=30)


def files():
    return requests.get(f"{a.mock}/__items", timeout=30).json()


s = requests.Session()
assert s.post(f"{a.app}/api/login", json={"user": a.user, "pass": a.password},
              timeout=30).json().get("ok"), "login failed"

print(f"\n=== server contract tests @ {a.app} ===")

# ── 1. createUploadSession survives throttling ────────────────────────────────
print("\n[1] Throttled createUploadSession is retried, not dropped")
reset(throttle_after=1, throttle_window=2.0)
t0 = time.time()
r1 = s.post(f"{a.app}/upload/session", json={"parent_id": "root", "name": "a.bin"}, timeout=60)
r2 = s.post(f"{a.app}/upload/session", json={"parent_id": "root", "name": "b.bin"}, timeout=60)
dt = time.time() - t0
ok("first session created", r1.json().get("ok"))
ok("second session created despite a 429", r2.json().get("ok"),
   f"HTTP {r2.status_code} after {dt:.1f}s")
ok("it actually waited for the throttle window", dt >= 1.0, f"{dt:.1f}s")

# ── 2. exhausted throttling reports 429 + retryable, not a fake 200 ──────────
print("\n[2] Unrecoverable throttling is reported honestly")
reset(throttle_after=0, throttle_window=600.0)
requests.post(f"{a.mock}/__config", json={"throttle_after": 1, "throttle_window": 600.0}, timeout=30)
s.post(f"{a.app}/upload/session", json={"parent_id": "root", "name": "x.bin"}, timeout=120)
r = s.post(f"{a.app}/upload/session", json={"parent_id": "root", "name": "y.bin"}, timeout=120)
ok("status is 429, not 200", r.status_code == 429, f"HTTP {r.status_code}")
body = r.json()
ok("marked retryable so the browser retries", body.get("retryable") is True, body)

# ── 3. single-shot small upload ───────────────────────────────────────────────
print("\n[3] Small-file upload takes one request and no session")
reset()
payload = os.urandom(512 * 1024)
r = s.put(f"{a.app}/upload/small", params={"parent_id": "root", "name": "small.bin"},
          data=payload, headers={"Content-Type": "application/octet-stream"}, timeout=120)
ok("upload ok", r.status_code == 200 and r.json().get("ok"), r.text[:120])
st = files()
ok("bytes landed intact", st["files"].get("small.bin") == len(payload),
   st["files"].get("small.bin"))
ok("no upload session was created", st["open_sessions"] == 0)
ok("response carries the new item id", bool(r.json().get("id")))

r = s.put(f"{a.app}/upload/small", params={"parent_id": "root", "name": "toobig.bin"},
          data=os.urandom(64), headers={"Content-Length": "64"}, timeout=60)
ok("oversize is rejected with 413 so the client falls back",
   s.put(f"{a.app}/upload/small", params={"parent_id": "root", "name": "big.bin"},
         data=os.urandom(9 * MiB), timeout=300).status_code == 413)

# ── 4. streaming proxy fidelity ───────────────────────────────────────────────
print("\n[4] Streaming upload proxy forwards bytes exactly")
reset()
total = 6 * MiB
body = os.urandom(total)
sess = s.post(f"{a.app}/upload/session", json={"parent_id": "root", "name": "stream.bin"},
              timeout=60).json()
ok("session created", sess.get("ok"))
off, chunk = 0, 2 * MiB
while off < total:
    n = min(chunk, total - off)
    rr = s.put(f"{a.app}/upload/proxy", params={"url": sess["uploadUrl"]},
               headers={"Content-Range": f"bytes {off}-{off+n-1}/{total}",
                        "Content-Type": "application/octet-stream"},
               data=body[off:off + n], timeout=300)
    if rr.status_code not in (200, 201, 202):
        ok(f"fragment @{off // MiB}MiB", False, f"HTTP {rr.status_code} {rr.text[:120]}")
        break
    off += n
got = s.get(f"{a.app}/dl", params={"id": "x"}, allow_redirects=False, timeout=30)  # noqa: F841
st = files()
ok("all bytes landed", st["files"].get("stream.bin") == total, st["files"].get("stream.bin"))
ok("no session left open", st["open_sessions"] == 0)
# verify content, not just length
ids = s.get(f"{a.app}/ls", params={"id": "root"}, timeout=30).json()["items"]
sid = next((i["id"] for i in ids if i["name"] == "stream.bin"), None)
raw = s.get(f"{a.app}/stream", params={"id": sid}, timeout=120).content if sid else b""
ok("content matches byte-for-byte",
   hashlib.sha256(raw).hexdigest() == hashlib.sha256(body).hexdigest(),
   f"{len(raw)} bytes")

# ── 5. batch delete ───────────────────────────────────────────────────────────
print("\n[5] Batch delete removes everything in one call")
reset()
requests.post(f"{a.mock}/__seed", json={"n": 20}, timeout=30)
ids = [i["id"] for i in s.get(f"{a.app}/ls", params={"id": "root"}, timeout=30).json()["items"]]
ok("20 items listed", len(ids) == 20, len(ids))
t0 = time.time()
r = s.post(f"{a.app}/delete/batch", json={"ids": ids}, timeout=120).json()
dt = time.time() - t0
ok("all reported deleted", r.get("ok") and len(r.get("deleted", [])) == 20, r)
ok("gone server-side", not files()["files"], files()["files"])
ok("one batched call is quick", dt < 5.0, f"{dt:.2f}s for 20")
ok("already-deleted ids are not errors",
   s.post(f"{a.app}/delete/batch", json={"ids": ids[:3]}, timeout=60).json().get("ok"))

# ── 6. names needing escaping ─────────────────────────────────────────────────
print("\n[6] Awkward file names survive path building")
reset()
for name in ["a b.txt", "100% done.txt", "what#now.txt", "q?x.txt", "über.txt", "a+b&c.txt"]:
    rr = s.put(f"{a.app}/upload/small", params={"parent_id": "root", "name": name},
               data=b"hello", timeout=60)
    ok(f"upload {name!r}", rr.status_code == 200 and rr.json().get("ok"),
       rr.text[:80] if rr.status_code != 200 else "")
landed = files()["files"]
ok("every name landed with its bytes",
   all(landed.get(n) == 5 for n in ["a b.txt", "100% done.txt", "what#now.txt",
                                    "q?x.txt", "über.txt", "a+b&c.txt"]), landed)

# ── 7. reverse-proxy headers ──────────────────────────────────────────────────
print("\n[7] Reverse-proxy headers are honoured")
h = {"X-Forwarded-Proto": "https", "X-Forwarded-Prefix": "/od", "X-Forwarded-Host": "ext.example"}
page = requests.get(f"{a.app}/", headers=h, timeout=30).text
ok("page advertises the proxy prefix", '"/od"' in page or "'/od'" in page,
   [l for l in page.splitlines() if "__BASE__" in l][:1])
bare = requests.get(f"{a.app}/", timeout=30).text
ok("no prefix when unproxied", '__BASE__ = ""' in bare,
   [l for l in bare.splitlines() if "__BASE__" in l][:1])
lr = requests.post(f"{a.app}/api/login", json={"user": a.user, "pass": a.password},
                   headers=h, timeout=30)
ok("cookie gets Secure when the proxy terminates TLS",
   "Secure" in lr.headers.get("Set-Cookie", ""), lr.headers.get("Set-Cookie", "")[:90])
lr2 = requests.post(f"{a.app}/api/login", json={"user": a.user, "pass": a.password}, timeout=30)
ok("and not over plain http", "Secure" not in lr2.headers.get("Set-Cookie", ""),
   lr2.headers.get("Set-Cookie", "")[:90])

# ── 8. host-driven turbo stream ───────────────────────────────────────────────
print("\n[8] Turbo stream: host drives the fragment protocol")
reset()
N = 48 * MiB
blob = os.urandom(N)
t0 = time.time()
r = s.put(f"{a.app}/upload/stream", params={"parent_id": "root", "name": "turbo.bin", "size": N},
          data=blob, headers={"Content-Type": "application/octet-stream"}, timeout=300)
dt = time.time() - t0
ok("one request uploads the whole file", r.status_code == 200 and r.json().get("ok"),
   f"HTTP {r.status_code} in {dt:.1f}s ({N/MiB/dt:.0f} MB/s)")
st = files()
ok("bytes land intact", st["files"].get("turbo.bin") == N, st["files"].get("turbo.bin"))
ok("no session left open", st["open_sessions"] == 0, st["open_sessions"])

# The regression that hung a client for 20 minutes: the reader thread touched Flask's
# thread-local `request`, died instantly, and we answered while the client was still
# sending — leaving it blocked writing into a socket nobody drained.
print("\n[9] A mid-upload failure answers instead of hanging the client")
reset(session_ttl=1, chunk_delay=0.7)
N = 96 * MiB
t0 = time.time()
try:
    r = s.put(f"{a.app}/upload/stream",
              params={"parent_id": "root", "name": "halfway.bin", "size": N},
              data=os.urandom(N), headers={"Content-Type": "application/octet-stream"},
              timeout=90)
    dt = time.time() - t0
    body = r.json()
    ok("client gets a response, not a hang", True, f"HTTP {r.status_code} in {dt:.1f}s")
    ok("failure is reported as resumable", body.get("resumable") is True, body.get("error"))
    ok("with the committed offset to resume from",
       isinstance(body.get("offset"), int) and body["offset"] > 0, body.get("offset"))
    ok("and the session url", bool(body.get("uploadUrl")))
except requests.exceptions.Timeout:
    ok("client gets a response, not a hang", False, "timed out — the deadlock is back")
reset()

print(f"\n=== {PASS} passed, {FAIL} failed ===")
sys.exit(1 if FAIL else 0)
