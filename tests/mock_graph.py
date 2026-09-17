#!/usr/bin/env python3
"""
A mock Microsoft Graph + CDN, good enough to exercise app.py end to end offline.

Implements the slice of Graph the app touches: drive quota, children listing (with
pagination), item create/patch/delete, content download, and *real* resumable upload
sessions that assemble fragments and enforce Graph's actual rules (sequential ranges,
throttling after a burst of createUploadSession calls).

Run standalone:  python tests/mock_graph.py [port]
"""
import json
import os
import re
import sys
import threading
import time
import uuid

from flask import Flask, Response, jsonify, request

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = None

_lock = threading.Lock()

# item_id -> {"name","parent","folder","content","modified"}
ITEMS = {}
SESSIONS = {}           # upload_id -> {"item_id","name","parent","size","buf"}
SESSION_CREATES = []     # timestamps, for throttle simulation
FRAG_PUTS = []           # timestamps of fragment PUTs, for throttle simulation

# Knobs the tests flip to emulate real-world Graph behaviour.
CFG = {
    "throttle_after": 0,     # >0: 429 createUploadSession after N creates in throttle_window
    "throttle_window": 10.0,
    "session_ttl": 0,        # >0: sessions expire (410 Gone) after N seconds
    "chunk_delay": 0.0,      # per-fragment latency, to emulate WAN RTT
    "frag_throttle_after": 0,  # >0: 429 fragment PUTs after N in frag_window
    "frag_window": 10.0,
    "cors": True,            # real MS upload sessions are CORS-enabled; off => proxy fallback
}


def _reset():
    with _lock:
        ITEMS.clear()
        SESSIONS.clear()
        SESSION_CREATES.clear()
        CFG.update({"throttle_after": 0, "throttle_window": 10.0,
                    "session_ttl": 0, "chunk_delay": 0.0,
                    "frag_throttle_after": 0, "frag_window": 10.0, "cors": True})
        FRAG_PUTS.clear()
        ITEMS["root"] = {"name": "root", "parent": None, "folder": True,
                         "content": b"", "modified": _now()}


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _item_json(iid):
    it = ITEMS[iid]
    d = {"id": iid, "name": it["name"], "lastModifiedDateTime": it["modified"],
         "size": 0 if it["folder"] else len(it["content"])}
    if it["folder"]:
        d["folder"] = {"childCount": sum(1 for x in ITEMS.values() if x["parent"] == iid)}
    else:
        d["file"] = {"mimeType": "application/octet-stream"}
        d["@microsoft.graph.downloadUrl"] = (
            f"{request.host_url.rstrip('/')}/cdn/{iid}?tempauth=mock")
    return d


def _children(pid):
    return [k for k, v in ITEMS.items() if v["parent"] == pid]


# ── test control plane ────────────────────────────────────────────────────────
@app.route("/__reset", methods=["POST"])
def ctl_reset():
    _reset()
    return jsonify({"ok": True})


@app.route("/__config", methods=["POST"])
def ctl_config():
    CFG.update(request.get_json(force=True) or {})
    return jsonify(CFG)


@app.route("/__items")
def ctl_items():
    """Flat dump: {name: size} for every non-folder item, for assertions."""
    return jsonify({"files": {v["name"]: len(v["content"])
                              for v in ITEMS.values() if not v["folder"]},
                    "folders": [v["name"] for v in ITEMS.values()
                                if v["folder"] and v["parent"]],
                    "open_sessions": len(SESSIONS)})


@app.route("/__seed", methods=["POST"])
def ctl_seed():
    """Seed N files into root so listing/selection tests have data."""
    n = int((request.get_json(force=True) or {}).get("n", 30))
    with _lock:
        for i in range(n):
            iid = uuid.uuid4().hex[:12]
            ITEMS[iid] = {"name": f"seed{i:03d}.txt", "parent": "root", "folder": False,
                          "content": f"seed {i}".encode(), "modified": _now()}
    return jsonify({"ok": True, "n": n})


# ── oauth ─────────────────────────────────────────────────────────────────────
@app.route("/oauth2/v2.0/token", methods=["POST"])
def token():
    return jsonify({"access_token": "mock-at-" + uuid.uuid4().hex[:8],
                    "refresh_token": "mock-rt", "expires_in": 3600})


@app.route("/oauth2/v2.0/devicecode", methods=["POST"])
def devicecode():
    return jsonify({"device_code": "mock-dc", "user_code": "MOCK-123",
                    "verification_uri": "https://microsoft.com/devicelogin", "interval": 1})


# ── graph ─────────────────────────────────────────────────────────────────────
@app.route("/v1.0/me")
def me():
    return jsonify({"userPrincipalName": "mock@example.com", "displayName": "Mock"})


@app.route("/v1.0/me/drive")
def drive():
    used = sum(len(v["content"]) for v in ITEMS.values() if not v["folder"])
    return jsonify({"quota": {"used": used, "total": 5 * 1024**3}})


@app.route("/v1.0/me/drive/root/children")
@app.route("/v1.0/me/drive/items/<pid>/children")
def children(pid="root"):
    kids = _children(pid)
    top = int(request.args.get("$top", 200))
    skip = int(request.args.get("$skip", 0))
    page = kids[skip:skip + top]
    out = {"value": [_item_json(k) for k in page]}
    if skip + top < len(kids):
        out["@odata.nextLink"] = (f"{request.host_url.rstrip('/')}{request.path}"
                                  f"?$top={top}&$skip={skip + top}")
    return jsonify(out)


@app.route("/v1.0/me/drive/items/<iid>", methods=["GET", "PATCH", "DELETE"])
def item(iid):
    if iid not in ITEMS:
        return jsonify({"error": {"code": "itemNotFound", "message": "not found"}}), 404
    if request.method == "DELETE":
        with _lock:
            stack = [iid]
            while stack:
                cur = stack.pop()
                stack.extend(_children(cur))
                ITEMS.pop(cur, None)
        return ("", 204)
    if request.method == "PATCH":
        ITEMS[iid]["name"] = (request.get_json(force=True) or {}).get("name", ITEMS[iid]["name"])
    return jsonify(_item_json(iid))


@app.route("/v1.0/me/drive/items/<iid>/content", methods=["GET", "PUT"])
def content(iid):
    if request.method == "PUT":
        if iid in ITEMS:
            ITEMS[iid]["content"] = request.get_data()
            return jsonify(_item_json(iid))
        return jsonify({"error": {"code": "itemNotFound"}}), 404
    if iid not in ITEMS:
        return jsonify({"error": {"code": "itemNotFound"}}), 404
    return Response(ITEMS[iid]["content"], content_type="application/octet-stream")


@app.route("/v1.0/me/drive/items/<iid>/thumbnails/0/<size>")
def thumb(iid, size):
    return jsonify({"url": f"{request.host_url.rstrip('/')}/cdn/{iid}?thumb=1"})


def _create_child(parent, name, folder=False, content=b""):
    for k in _children(parent):
        if ITEMS[k]["name"] == name:
            ITEMS[k].update(content=content, modified=_now())
            return k
    iid = uuid.uuid4().hex[:12]
    ITEMS[iid] = {"name": name, "parent": parent, "folder": folder,
                  "content": content, "modified": _now()}
    return iid


@app.route("/v1.0/me/drive/root/children", methods=["POST"])
@app.route("/v1.0/me/drive/items/<pid>/children", methods=["POST"])
def mkchild(pid="root"):
    d = request.get_json(force=True) or {}
    with _lock:
        iid = _create_child(pid, d.get("name", "untitled"), folder="folder" in d)
    return jsonify(_item_json(iid)), 201


@app.route("/v1.0/me/drive/root:/<path:p>:/content", methods=["PUT"])
def put_by_path_root(p):
    with _lock:
        iid = _create_child("root", p.split("/")[-1], content=request.get_data())
    return jsonify(_item_json(iid))


@app.route("/v1.0/me/drive/items/<pid>:/<path:p>:/content", methods=["PUT"])
def put_by_path(pid, p):
    with _lock:
        iid = _create_child(pid, p.split("/")[-1], content=request.get_data())
    return jsonify(_item_json(iid))


# ── resumable upload sessions (the real rules) ────────────────────────────────
def _new_session(parent, name):
    now = time.time()
    with _lock:
        SESSION_CREATES[:] = [t for t in SESSION_CREATES if now - t < CFG["throttle_window"]]
        if CFG["throttle_after"] and len(SESSION_CREATES) >= CFG["throttle_after"]:
            return None, ("throttle", 429)
        SESSION_CREATES.append(now)
        sid = uuid.uuid4().hex
        # Personal OneDrive reserves the name immediately: an open session shows up in
        # listings as a 0-byte item until the final fragment commits it. This is exactly
        # what users see as "uploaded as 0b" when a bulk upload half-fails.
        placeholder = _create_child(parent, name, content=b"")
        SESSIONS[sid] = {"name": name, "parent": parent, "buf": bytearray(),
                         "size": None, "created": now, "item_id": placeholder}
    return sid, None


@app.route("/v1.0/me/drive/root:/<path:p>:/createUploadSession", methods=["POST"])
def mksession_root(p):
    return _mksession("root", p.split("/")[-1])


@app.route("/v1.0/me/drive/items/<pid>:/<path:p>:/createUploadSession", methods=["POST"])
def mksession(pid, p):
    return _mksession(pid, p.split("/")[-1])


def _mksession(pid, name):
    sid, err = _new_session(pid, name)
    if err:
        return jsonify({"error": {"code": "activityLimitReached",
                                  "message": "Throttled. Retry later.",
                                  "retryAfterSeconds": 3}}), 429
    return jsonify({
        "uploadUrl": f"{request.host_url.rstrip('/')}/upsession/{sid}",
        "expirationDateTime": _now(),
    })


def _cors(resp):
    if CFG["cors"]:
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Methods"] = "PUT, DELETE, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Range, Content-Type"
    return resp


@app.route("/upsession/<sid>", methods=["OPTIONS"])
def upsession_pre(sid):
    return _cors(Response("", status=204))


@app.route("/upsession/<sid>", methods=["PUT", "DELETE"])
def upsession(sid):
    if request.method == "DELETE":
        s = SESSIONS.pop(sid, None)
        if s and s.get("item_id") in ITEMS and not ITEMS[s["item_id"]]["content"]:
            ITEMS.pop(s["item_id"], None)   # drop the 0-byte placeholder
        return _cors(Response("", status=204))
    s = SESSIONS.get(sid)
    if not s:
        return _cors(jsonify({"error": {"code": "resourceModified",
                                        "message": "session not found"}})), 404
    if CFG["session_ttl"] and time.time() - s["created"] > CFG["session_ttl"]:
        SESSIONS.pop(sid, None)
        return _cors(jsonify({"error": {"code": "resourceModified",
                                        "message": "session expired"}})), 410
    now = time.time()
    with _lock:
        FRAG_PUTS[:] = [t for t in FRAG_PUTS if now - t < CFG["frag_window"]]
        if CFG["frag_throttle_after"] and len(FRAG_PUTS) >= CFG["frag_throttle_after"]:
            return _cors(jsonify({"error": {"code": "activityLimitReached",
                                            "message": "Throttled. Retry later.",
                                            "retryAfterSeconds": 2}})), 429
        FRAG_PUTS.append(now)
    cr = request.headers.get("Content-Range", "")
    m = re.match(r"bytes (\d+)-(\d+)/(\d+)", cr)
    if not m:
        return _cors(jsonify({"error": {"code": "invalidRange",
                                        "message": f"bad Content-Range {cr!r}"}})), 400
    start, end, total = int(m.group(1)), int(m.group(2)), int(m.group(3))
    body = request.get_data()
    if CFG["chunk_delay"]:
        time.sleep(CFG["chunk_delay"])
    if len(body) != end - start + 1:
        return _cors(jsonify({"error": {"code": "invalidRange", "message":
                                        f"declared {end-start+1} bytes, got {len(body)}"}})), 400
    with _lock:
        s["size"] = total
        # Graph requires strictly sequential fragments; out-of-order is rejected.
        if start != len(s["buf"]):
            return _cors(jsonify({"error": {"code": "invalidRange", "message":
                                            f"expected offset {len(s['buf'])}, got {start}"}})), 416
        s["buf"].extend(body)
        if len(s["buf"]) >= total:
            iid = _create_child(s["parent"], s["name"], content=bytes(s["buf"]))
            SESSIONS.pop(sid, None)
            return _cors(jsonify(_item_json(iid))), 201
        nxt = len(s["buf"])
    return _cors(jsonify({"expirationDateTime": _now(),
                          "nextExpectedRanges": [f"{nxt}-{total-1}"]})), 202


# ── fake CDN ──────────────────────────────────────────────────────────────────
@app.route("/cdn/<iid>")
def cdn(iid):
    if iid not in ITEMS:
        return ("", 404)
    data = ITEMS[iid]["content"]
    rng = request.headers.get("Range")
    hdrs = {"Accept-Ranges": "bytes", "Access-Control-Allow-Origin": "*"}
    if rng and (m := re.match(r"bytes=(\d*)-(\d*)", rng)):
        a = int(m.group(1) or 0)
        b = int(m.group(2)) if m.group(2) else len(data) - 1
        b = min(b, len(data) - 1)
        hdrs["Content-Range"] = f"bytes {a}-{b}/{len(data)}"
        return Response(data[a:b + 1], status=206, headers=hdrs,
                        content_type="application/octet-stream")
    return Response(data, headers=hdrs, content_type="application/octet-stream")


_reset()

if __name__ == "__main__":
    from werkzeug.serving import run_simple
    run_simple("127.0.0.1", int(sys.argv[1]) if len(sys.argv) > 1 else 5999,
               app, threaded=True, use_reloader=False)
