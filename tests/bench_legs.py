#!/usr/bin/env python3
"""
Measure the turbo path the way it actually runs: a client leg and an upstream leg that
both have real latency, rather than a zero-RTT loopback (which hides lock-step stalls
completely — the reason the first round of "fixes" measured fine and ran slow).

Sits a latency-injecting TCP relay in front of the mock Graph, so the host->Microsoft leg
has a realistic RTT, and trickles the client body at a fixed rate.

  python tests/bench_legs.py --app http://127.0.0.1:3300 --rtt-ms 30
"""
import argparse, json, os, queue, socket, threading, time
import requests

ap = argparse.ArgumentParser()
ap.add_argument("--app", default="http://127.0.0.1:3300")
ap.add_argument("--mock", default="http://127.0.0.1:5999")
ap.add_argument("--relay-port", type=int, default=5988)
ap.add_argument("--rtt-ms", type=float, default=30.0, help="upstream one-way delay")
ap.add_argument("--mib", type=int, default=20, help="fragment size to push")
ap.add_argument("--user", default="tester")
ap.add_argument("--password", default="hunter2pass")
a = ap.parse_args()

MOCK_HOST, MOCK_PORT = a.mock.split("//")[1].split(":")
MOCK_PORT = int(MOCK_PORT)
DELAY = a.rtt_ms / 1000.0 / 2


def relay():
    """TCP relay that adds a fixed delay to the first packet of each direction burst,
    approximating a link with RTT (enough to expose lock-step behaviour)."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", a.relay_port))
    srv.listen(64)

    def pipe(src, dst, delay):
        """
        Delay each block by a fixed amount WITHOUT serialising it.

        Sleeping inline before forwarding would cap throughput at block/delay (64 KiB per
        30 ms = ~2 MB/s) and measure the harness rather than the app. Instead each block is
        stamped with a due time and handed to a forwarder, so many blocks are in flight at
        once and the link has latency but not an artificial bandwidth ceiling.
        """
        q = queue.Queue()

        def forward():
            while True:
                item = q.get()
                if item is None:
                    break
                due, data = item
                now = time.monotonic()
                if due > now:
                    time.sleep(due - now)
                try:
                    dst.sendall(data)
                except OSError:
                    break
            try:
                dst.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        t = threading.Thread(target=forward, daemon=True)
        t.start()
        try:
            while True:
                b = src.recv(65536)
                if not b:
                    break
                q.put((time.monotonic() + delay, b))
        except OSError:
            pass
        finally:
            q.put(None)

    def serve(c):
        u = socket.create_connection((MOCK_HOST, MOCK_PORT))
        threading.Thread(target=pipe, args=(c, u, DELAY), daemon=True).start()
        threading.Thread(target=pipe, args=(u, c, DELAY), daemon=True).start()

    while True:
        c, _ = srv.accept()
        threading.Thread(target=serve, args=(c,), daemon=True).start()


threading.Thread(target=relay, daemon=True).start()
time.sleep(0.5)

s = requests.Session()
requests.post(f"{a.mock}/__reset", timeout=30)
assert s.post(f"{a.app}/api/login", json={"user": a.user, "pass": a.password},
              timeout=30).json().get("ok"), "login failed"

total = a.mib * 1024 * 1024
sess = s.post(f"{a.app}/upload/session", json={"parent_id": "root", "name": "legs.bin"},
              timeout=60).json()
assert sess.get("ok"), sess
# point the fragment PUT at the laggy relay instead of the mock directly
up = sess["uploadUrl"].replace(f"{MOCK_HOST}:{MOCK_PORT}", f"127.0.0.1:{a.relay_port}")

payload = os.urandom(total)
t0 = time.time()
r = s.put(f"{a.app}/upload/proxy", params={"url": up},
          headers={"Content-Range": f"bytes 0-{total-1}/{total}",
                   "Content-Type": "application/octet-stream"},
          data=payload, timeout=900)
dt = time.time() - t0

landed = requests.get(f"{a.mock}/__items", timeout=30).json()["files"].get("legs.bin")
print(json.dumps({"fragment_mib": a.mib, "upstream_rtt_ms": a.rtt_ms,
                  "status": r.status_code, "seconds": round(dt, 2),
                  "MB_per_s": round(a.mib / dt, 2),
                  "bytes_landed": landed, "correct": landed == total}))
