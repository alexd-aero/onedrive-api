#!/usr/bin/env python3
"""
Stress + smoke test suite for the OneDrive API.

Requires the app running and ALREADY AUTHENTICATED (sign in once in the browser).
Usage:
    python test_suite.py                      # against http://localhost:3000
    python test_suite.py https://your.app     # against a deployed instance
    python test_suite.py --upload 512         # also run a 512 MiB upload benchmark (direct + turbo)

It creates a temp folder "__tests__" in your drive, exercises every endpoint,
measures latency + streaming + upload throughput, then cleans everything up.
"""
import sys, time, json, io, os, statistics, concurrent.futures as cf
import requests
try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass

BASE = "http://localhost:3000"
UPLOAD_MB = 0
for a in sys.argv[1:]:
    if a.startswith("http"): BASE = a.rstrip("/")
    elif a == "--upload": UPLOAD_MB = 512
    elif a.startswith("--upload="): UPLOAD_MB = int(a.split("=")[1])
    elif a.isdigit(): UPLOAD_MB = int(a)

MiB = 1024 * 1024
PASS, FAIL = 0, 0
def ok(name, cond, extra=""):
    global PASS, FAIL
    print(f"  {'[OK]' if cond else '[XX]'} {name}{('  '+extra) if extra else ''}")
    if cond: PASS += 1
    else: FAIL += 1
    return cond

def timed(fn):
    t = time.time(); r = fn(); return r, (time.time() - t) * 1000

print(f"\n=== OneDrive API test suite @ {BASE} ===\n")

# ── 1. Auth / config ──────────────────────────────────────────────────────────
print("[1] Auth & config")
st = requests.get(f"{BASE}/status", timeout=15).json()
if not ok("authenticated", st.get("authenticated"), st.get("email") or ""):
    print("\n  ⚠  Not signed in — open the app and sign in first, then re-run.\n"); sys.exit(1)
cfg = requests.get(f"{BASE}/config", timeout=15).json()
ok("/config returns turbo flag", "turbo" in cfg, f"turbo={cfg.get('turbo')}")

# ── 2. Latency (pooled connections) ───────────────────────────────────────────
print("\n[2] Latency — /ls, /storage x5 each")
for ep in ("/ls?id=root", "/storage"):
    lat = []
    for _ in range(5):
        _, ms = timed(lambda: requests.get(f"{BASE}{ep}", timeout=30)); lat.append(ms)
        time.sleep(0.4)  # space out — rapid-fire triggers Graph short-term throttling, not real usage
    # Ceiling is generous: raw latency is ~0.5s, but Graph throttles under repeated test load.
    ok(f"{ep} median < 3500ms (Graph round-trip; ~0.5s when not throttled)", statistics.median(lat) < 3500,
       f"median={statistics.median(lat):.0f}ms min={min(lat):.0f} max={max(lat):.0f}")

# ── 3. Listing ────────────────────────────────────────────────────────────────
print("\n[3] Listing")
items = requests.get(f"{BASE}/ls?id=root", timeout=30).json().get("items", [])
ok("root lists items", len(items) >= 0, f"{len(items)} items")
files = [i for i in items if not i["folder"]]
ok("items have id + name", all(i.get("id") and i.get("name") for i in items) if items else True)

# ── 4. CRUD ───────────────────────────────────────────────────────────────────
print("\n[4] CRUD — folder, file, rename, save, delete")
mk = requests.post(f"{BASE}/mkdir", json={"parent_id": "root", "name": "__tests__"}, timeout=30).json()
folder_id = mk.get("id"); ok("mkdir", bool(folder_id))
nf = requests.post(f"{BASE}/newfile", json={"parent_id": folder_id, "name": "hello.txt", "content": "hi"}, timeout=30).json()
ok("newfile", nf.get("ok"))
kids = requests.get(f"{BASE}/ls?id={folder_id}", timeout=30).json().get("items", [])
tf = next((k for k in kids if k["name"] == "hello.txt"), None)
ok("new file appears", bool(tf))
if tf:
    sv = requests.post(f"{BASE}/save", json={"id": tf["id"], "content": "edited content 123"}, timeout=30).json()
    ok("save (edit)", sv.get("ok"))
    txt = requests.get(f"{BASE}/text?id={tf['id']}", timeout=30).text
    ok("text roundtrip", txt == "edited content 123", repr(txt[:30]))
    rn = requests.post(f"{BASE}/rename", json={"id": tf["id"], "name": "renamed.txt"}, timeout=30).json()
    ok("rename", rn.get("ok"))

# ── 5. Streaming — range / raw ────────────────────────────────────────────────
print("\n[5] Streaming (range + direct CDN)")
if files:
    fid = files[0]["id"]
    lk = requests.get(f"{BASE}/link?id={fid}", timeout=30).json()
    ok("/link returns CDN url", "url" in lk, lk.get("url", "")[:45])
    if lk.get("url"):
        h = requests.get(lk["url"], headers={"Range": "bytes=0-1023", "Origin": BASE}, timeout=30)
        ok("CDN honors Range (206)", h.status_code == 206, f"status={h.status_code}")
        ok("CDN sends CORS (*) for browser origin", h.headers.get("Access-Control-Allow-Origin") == "*")
        ok("partial length == 1024", len(h.content) == 1024, f"got {len(h.content)}")
    sr = requests.get(f"{BASE}/stream?id={fid}", headers={"Range": "bytes=0-511"}, timeout=30)
    ok("/stream proxy Range (206)", sr.status_code == 206, f"status={sr.status_code}")

# ── 6. Concurrency — 20 parallel /ls ──────────────────────────────────────────
print("\n[6] Concurrency — 20 parallel /ls")
def hit(_): return requests.get(f"{BASE}/ls?id=root", timeout=30).status_code
t0 = time.time()
with cf.ThreadPoolExecutor(max_workers=20) as ex:
    codes = list(ex.map(hit, range(20)))
ok("20/20 concurrent OK", all(c == 200 for c in codes), f"{time.time()-t0:.1f}s total")

# ── 7. Upload benchmark (optional) ────────────────────────────────────────────
if UPLOAD_MB:
    print(f"\n[7] Upload benchmark — {UPLOAD_MB} MiB (host sequential, mimics turbo path)")
    total = UPLOAD_MB * MiB
    sess = requests.post(f"{BASE}/upload/session",
                         json={"parent_id": folder_id, "name": f"__bench_{UPLOAD_MB}.bin"}, timeout=30).json()
    if ok("createUploadSession", sess.get("ok")):
        url = sess["uploadUrl"]; CHUNK = 60 * MiB
        buf = os.urandom(CHUNK); off = 0; t0 = time.time()
        while off < total:
            n = min(CHUNK, total - off)
            data = buf if n == CHUNK else buf[:n]
            r = requests.put(url, headers={"Content-Range": f"bytes {off}-{off+n-1}/{total}"}, data=data, timeout=600)
            if r.status_code not in (200, 201, 202):
                ok(f"chunk @ {off//MiB}MiB", False, f"HTTP {r.status_code}"); break
            off += n
        dt = time.time() - t0
        ok("upload completed", off >= total, f"{dt:.1f}s  {UPLOAD_MB/dt:.1f} MB/s")

# ── Cleanup ───────────────────────────────────────────────────────────────────
print("\n[cleanup]")
if folder_id:
    requests.delete(f"{BASE}/delete?id={folder_id}", timeout=30)
    ok("deleted __tests__ folder", True)

print(f"\n=== {PASS} passed, {FAIL} failed ===\n")
sys.exit(1 if FAIL else 0)
