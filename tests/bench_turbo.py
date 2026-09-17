#!/usr/bin/env python3
"""Measure the host-proxied (turbo) upload path: /upload/proxy throughput + peak RSS.

  python tests/bench_turbo.py [--mb 48] [--chunk 16] [--app http://127.0.0.1:3300]
"""
import argparse, json, os, sys, threading, time
import requests

ap = argparse.ArgumentParser()
ap.add_argument("--app", default="http://127.0.0.1:3300")
ap.add_argument("--tag", default=None, help="substring identifying the app process to watch")
ap.add_argument("--mock", default="http://127.0.0.1:5999")
ap.add_argument("--mb", type=int, default=48)
ap.add_argument("--chunk", type=int, default=16)
ap.add_argument("--user", default="tester")
ap.add_argument("--password", default="hunter2pass")
a = ap.parse_args()

MiB = 1024 * 1024
s = requests.Session()
s.post(f"{a.mock}/__reset", timeout=30)
r = s.post(f"{a.app}/api/login", json={"user": a.user, "pass": a.password}, timeout=30)
assert r.json().get("ok"), r.text

total = a.mb * MiB
sess = s.post(f"{a.app}/upload/session",
              json={"parent_id": "root", "name": f"bench_{a.mb}.bin"}, timeout=30).json()
assert sess.get("ok"), sess
url = sess["uploadUrl"]

CHUNK = a.chunk * MiB
payload = os.urandom(CHUNK)

# sample the app process RSS while it forwards, to expose store-and-forward buffering
PORT_TAG = a.tag or a.app.rsplit(":", 1)[-1]
rss_peak = [0]
stop = threading.Event()
def watch():
    pid = None
    for p in os.listdir("/proc"):
        if not p.isdigit():
            continue
        try:
            cl = open(f"/proc/{p}/cmdline", "rb").read().decode("utf8", "replace")
        except OSError:
            continue
        if "harness.py" in cl and "mock_graph" not in cl and PORT_TAG in cl:
            pid = p
            break
    if not pid:
        return
    while not stop.is_set():
        try:
            for line in open(f"/proc/{pid}/status"):
                if line.startswith("VmRSS:"):
                    rss_peak[0] = max(rss_peak[0], int(line.split()[1]))
        except OSError:
            return
        time.sleep(0.05)
threading.Thread(target=watch, daemon=True).start()

off, t0 = 0, time.time()
while off < total:
    n = min(CHUNK, total - off)
    data = payload if n == CHUNK else payload[:n]
    rr = s.put(f"{a.app}/upload/proxy", params={"url": url},
               headers={"Content-Range": f"bytes {off}-{off+n-1}/{total}",
                        "Content-Type": "application/octet-stream"},
               data=data, timeout=600)
    if rr.status_code not in (200, 201, 202):
        print(f"FAIL chunk @{off//MiB}MiB HTTP {rr.status_code}: {rr.text[:200]}")
        sys.exit(1)
    off += n
dt = time.time() - t0
stop.set()

got = s.get(f"{a.mock}/__items", timeout=30).json()["files"].get(f"bench_{a.mb}.bin")
print(json.dumps({"mb": a.mb, "chunk_mib": a.chunk, "seconds": round(dt, 2),
                  "MB_per_s": round(a.mb / dt, 2), "Mbps": round(a.mb * 8 / dt, 1),
                  "bytes_landed": got, "correct": got == total,
                  "app_peak_rss_mib": round(rss_peak[0] / 1024, 1)}))
