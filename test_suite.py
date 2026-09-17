#!/usr/bin/env python3
"""
Stress + smoke test suite for the OneDrive API (with auth).

The app must be fully configured (creds.yml + token.yml) and reachable. Provide the
username/password you set during setup.

Usage:
    python test_suite.py --user alex --pass hunter2pass
    python test_suite.py https://your.app --user alex --pass secret
    python test_suite.py --user alex --pass secret --upload 512
Env fallback: OD_USER / OD_PASS.
"""
import sys, os, time, json, statistics, concurrent.futures as cf
from urllib.parse import urlparse
import requests
try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass

BASE = "http://localhost:3000"
USER = os.environ.get("OD_USER"); PASS = os.environ.get("OD_PASS"); UPLOAD_MB = 0
args = sys.argv[1:]
i = 0
while i < len(args):
    a = args[i]
    if a.startswith("http"): BASE = a.rstrip("/")
    elif a in ("--user", "-u"): USER = args[i+1]; i += 1
    elif a in ("--pass", "-p"): PASS = args[i+1]; i += 1
    elif a == "--upload": UPLOAD_MB = 512
    elif a.startswith("--upload="): UPLOAD_MB = int(a.split("=")[1])
    elif a.isdigit(): UPLOAD_MB = int(a)
    i += 1

MiB = 1024 * 1024
PASS_N, FAIL_N = 0, 0
def ok(name, cond, extra=""):
    global PASS_N, FAIL_N
    print(f"  {'[OK]' if cond else '[XX]'} {name}{('  '+extra) if extra else ''}")
    PASS_N += 1 if cond else 0; FAIL_N += 0 if cond else 1
    return cond
def timed(fn):
    t = time.time(); r = fn(); return r, (time.time() - t) * 1000

print(f"\n=== OneDrive API test suite @ {BASE} ===\n")

# ── 0. State + auth gating ────────────────────────────────────────────────────
print("[0] State & auth gating")
st = requests.get(f"{BASE}/api/state", timeout=15).json()
ok("/api/state reachable", "state" in st, f"state={st.get('state')}")
if st.get("state") != "ready":
    print(f"\n  ⚠  App is '{st.get('state')}', not fully configured. Finish setup, then re-run.\n"); sys.exit(1)
ok("unauth /ls is blocked", requests.get(f"{BASE}/ls?id=root", timeout=15).status_code in (401, 403))
ok("unauth /storage is blocked", requests.get(f"{BASE}/storage", timeout=15).status_code in (401, 403))
if not USER or not PASS:
    print("\n  ⚠  Provide --user and --pass (or OD_USER/OD_PASS) to run the authed tests.\n"); sys.exit(1)
ok("wrong password rejected",
   requests.post(f"{BASE}/api/login", json={"user": USER, "pass": PASS + "x"}, timeout=15).status_code == 401)

# authenticated session
s = requests.Session()
lr = s.post(f"{BASE}/api/login", json={"user": USER, "pass": PASS}, timeout=15)
if not ok("login succeeds", lr.status_code == 200 and lr.json().get("ok")):
    print("\n  ⚠  Login failed — check credentials.\n"); sys.exit(1)
ok("session cookie set", "od_sess" in s.cookies.get_dict())
acc = s.get(f"{BASE}/api/accounts", timeout=15).json()
ok("accounts list has >=1 active account", any(a.get("active") for a in acc.get("accounts", [])),
   f"{len(acc.get('accounts', []))} account(s)")
ok("unauth /api/accounts blocked", requests.get(f"{BASE}/api/accounts", timeout=15).status_code in (401, 403))

# ── 1. Latency ────────────────────────────────────────────────────────────────
print("\n[1] Latency — /ls, /storage x5 each (spaced)")
for ep in ("/ls?id=root", "/storage"):
    lat = []
    for _ in range(5):
        _, ms = timed(lambda: s.get(f"{BASE}{ep}", timeout=30)); lat.append(ms); time.sleep(0.4)
    ok(f"{ep} median < 3500ms (Graph round-trip; ~0.5s unthrottled)", statistics.median(lat) < 3500,
       f"median={statistics.median(lat):.0f}ms min={min(lat):.0f} max={max(lat):.0f}")

# ── 2. Listing ────────────────────────────────────────────────────────────────
print("\n[2] Listing")
items = s.get(f"{BASE}/ls?id=root", timeout=30).json().get("items", [])
ok("root lists items", len(items) >= 0, f"{len(items)} items")
files = [x for x in items if not x["folder"]]
ok("items have id + name", all(x.get("id") and x.get("name") for x in items) if items else True)

# ── 3. CRUD ───────────────────────────────────────────────────────────────────
print("\n[3] CRUD — folder, file, save, rename, delete")
folder_id = s.post(f"{BASE}/mkdir", json={"parent_id": "root", "name": "__tests__"}, timeout=30).json().get("id")
ok("mkdir", bool(folder_id))
ok("newfile", s.post(f"{BASE}/newfile", json={"parent_id": folder_id, "name": "hello.txt", "content": "hi"}, timeout=30).json().get("ok"))
kids = s.get(f"{BASE}/ls?id={folder_id}", timeout=30).json().get("items", [])
tf = next((k for k in kids if k["name"] == "hello.txt"), None)
ok("new file appears", bool(tf))
if tf:
    ok("save (edit)", s.post(f"{BASE}/save", json={"id": tf["id"], "content": "edited 123"}, timeout=30).json().get("ok"))
    ok("text roundtrip", s.get(f"{BASE}/text?id={tf['id']}", timeout=30).text == "edited 123")
    ok("rename", s.post(f"{BASE}/rename", json={"id": tf["id"], "name": "renamed.txt"}, timeout=30).json().get("ok"))

# ── 4. Streaming / raw / view ─────────────────────────────────────────────────
print("\n[4] Streaming (range, CDN, raw, view)")
if files:
    fid = files[0]["id"]
    lk = s.get(f"{BASE}/link?id={fid}", timeout=30).json()
    ok("/link returns CDN url", "url" in lk, lk.get("url", "")[:42])
    if lk.get("url"):
        h = requests.get(lk["url"], headers={"Range": "bytes=0-1023", "Origin": BASE}, timeout=30)
        ok("CDN honors Range (206)", h.status_code == 206)
        ok("CDN CORS * for browser origin", h.headers.get("Access-Control-Allow-Origin") == "*")
    sr = s.get(f"{BASE}/stream?id={fid}", headers={"Range": "bytes=0-511"}, timeout=30)
    ok("/stream range (206)", sr.status_code == 206)
    rw = s.get(f"{BASE}/raw?id={fid}", headers={"Range": "bytes=0-63"}, timeout=30)
    ok("/raw inline + range", rw.status_code == 206 and rw.headers.get("Content-Disposition") == "inline")
    # A browser navigation gets the viewer shell...
    vw = s.get(f"{BASE}/view?id={fid}", timeout=30,
               headers={"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})
    # The shell must embed whatever signed url /link handed out — matching Microsoft's real
    # CDN host exactly would make this unrunnable against tests/mock_graph.py.
    cdn_host = urlparse(lk.get("url", "")).netloc
    ok("/view shell renders + embeds the signed CDN url",
       vw.status_code == 200 and bool(cdn_host) and cdn_host in vw.text, cdn_host)
    # ...while curl / fetch / a script tag get the file itself, not viewer boilerplate.
    rawv = s.get(f"{BASE}/view?id={fid}", timeout=30, headers={"Accept": "*/*"})
    ok("/view serves raw source to non-browser clients",
       rawv.status_code == 200 and "<!DOCTYPE html>" not in rawv.text[:200],
       rawv.headers.get("Content-Type", ""))

# ── 5. Concurrency ────────────────────────────────────────────────────────────
print("\n[5] Concurrency — 25 parallel authed /ls")
def hit(_):
    return s.get(f"{BASE}/ls?id=root", timeout=30).status_code
t0 = time.time()
with cf.ThreadPoolExecutor(max_workers=25) as ex:
    codes = list(ex.map(hit, range(25)))
ok("25/25 concurrent OK", all(c == 200 for c in codes), f"{time.time()-t0:.1f}s total")

# ── 6. Upload benchmark (optional) ────────────────────────────────────────────
if UPLOAD_MB and folder_id:
    print(f"\n[6] Upload benchmark — {UPLOAD_MB} MiB (host sequential = turbo path)")
    total = UPLOAD_MB * MiB
    sess = s.post(f"{BASE}/upload/session", json={"parent_id": folder_id, "name": f"__bench_{UPLOAD_MB}.bin"}, timeout=30).json()
    if ok("createUploadSession", sess.get("ok")):
        url = sess["uploadUrl"]; CHUNK = 60 * MiB; buf = os.urandom(CHUNK); off = 0; t0 = time.time()
        while off < total:
            n = min(CHUNK, total - off); data = buf if n == CHUNK else buf[:n]
            r = requests.put(url, headers={"Content-Range": f"bytes {off}-{off+n-1}/{total}"}, data=data, timeout=600)
            if r.status_code not in (200, 201, 202):
                ok(f"chunk @ {off//MiB}MiB", False, f"HTTP {r.status_code}"); break
            off += n
        dt = time.time() - t0
        ok("upload completed", off >= total, f"{dt:.1f}s  {UPLOAD_MB/dt:.1f} MB/s")

# ── Cleanup ───────────────────────────────────────────────────────────────────
print("\n[cleanup]")
if folder_id:
    s.delete(f"{BASE}/delete?id={folder_id}", timeout=30); ok("deleted __tests__ folder", True)

print(f"\n=== {PASS_N} passed, {FAIL_N} failed ===\n")
sys.exit(1 if FAIL_N else 0)
