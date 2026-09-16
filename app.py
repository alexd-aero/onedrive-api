#!/usr/bin/env python3
"""
OneDrive API — single-file Flask app.

What it does
------------
- Hosts a glassmorphism OneDrive explorer UI.
- Handles Microsoft device-code auth *inside the UI* (code + link + live status dot).
- Captures BOTH the access token and the refresh token, persists them to a `.env`
  in this same folder, and silently refreshes every 10 minutes so the session
  never dies mid-upload.
- Uploads go BROWSER -> MICROSOFT directly (resumable upload session), so a 512 MB
  file no longer has to crawl browser -> Flask -> Microsoft. A same-origin proxy
  fallback kicks in automatically if the browser is blocked by CORS.
- Previews/downloads stream the *raw* file with HTTP range support. Media points
  straight at Microsoft's signed CDN URL (no host-side buffering at all).
- Ace editor (edit + save), full-permission HTML preview, custom video player,
  and a custom glowing audio player with a live visualizer.

Run:  python app.py
Then open the URL it prints (default http://localhost:3000).
"""

import os
import time
import json
import threading
import requests
from flask import (
    Flask, request, jsonify, render_template_string, Response,
    stream_with_context, redirect,
)

# ── Config ────────────────────────────────────────────────────────────────────
PORT       = int(os.environ.get("PORT", 3000))
TENANT     = "common"
# Microsoft Graph Command Line Tools — a Microsoft first-party PUBLIC client that is
# pre-authorized for delegated Graph scopes (incl. Files.ReadWrite.All) via device code.
# We do NOT register an app; this is Microsoft's own client. The old Office client
# (d3590ed6-...) is NOT pre-authorized for Files.ReadWrite.All, which triggers the
# "users are not permitted to consent to first party applications" dead-end.
CLIENT_ID  = "14d82eec-204b-4c2f-b7e8-296a70dab67e"
SCOPES     = "Files.ReadWrite.All offline_access"
ENV_PATH   = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
REFRESH_EVERY = 10 * 60  # seconds

AUTH_BASE  = f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0"
GRAPH      = "https://graph.microsoft.com/v1.0"
# Turbo upload = stream browser->host->Microsoft (~44 MB/s but uses host egress).
# Off by default so a Wasmer deploy doesn't burn its bandwidth cap; set TURBO_UPLOAD=1 locally.
TURBO      = os.environ.get("TURBO_UPLOAD", "0") == "1"

# One pooled session for ALL outbound calls (Graph, CDN, upload proxy). Keeps TLS warm
# to graph.microsoft.com so /ls, /link etc. don't pay a fresh handshake every request.
from requests.adapters import HTTPAdapter
S = requests.Session()
S.mount("https://", HTTPAdapter(pool_connections=20, pool_maxsize=40, max_retries=0))

# ── Token state ───────────────────────────────────────────────────────────────
TOKENS = {"access_token": None, "refresh_token": None, "expires_at": 0, "email": None}
_device = {"device_code": None, "interval": 5, "active": False, "error": None}
_lock = threading.Lock()


# ── .env persistence (no extra deps) ──────────────────────────────────────────
def load_env():
    if not os.path.exists(ENV_PATH):
        return {}
    data = {}
    with open(ENV_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            data[k.strip()] = v.strip()
    return data


def save_env():
    lines = [
        "# OneDrive API tokens — auto-managed, do not share.",
        f"ACCESS_TOKEN={TOKENS['access_token'] or ''}",
        f"REFRESH_TOKEN={TOKENS['refresh_token'] or ''}",
        f"EXPIRES_AT={int(TOKENS['expires_at'])}",
        f"EMAIL={TOKENS['email'] or ''}",
    ]
    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# ── Token helpers ─────────────────────────────────────────────────────────────
def apply_token_response(res):
    """Store a token endpoint response. Returns True on success."""
    if "access_token" not in res:
        return False
    with _lock:
        TOKENS["access_token"] = res["access_token"]
        # refresh tokens rotate — keep the newest, fall back to the old one
        if res.get("refresh_token"):
            TOKENS["refresh_token"] = res["refresh_token"]
        TOKENS["expires_at"] = time.time() + int(res.get("expires_in", 3600))
    fetch_email()
    save_env()
    return True


def refresh_now():
    """Use the refresh token to mint a fresh access token."""
    rt = TOKENS.get("refresh_token")
    if not rt:
        return False
    try:
        res = S.post(f"{AUTH_BASE}/token", data={
            "client_id": CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": rt,
            "scope": SCOPES,
        }, timeout=20).json()
    except Exception as e:
        print(f"[refresh] network error: {e}")
        return False
    if not apply_token_response(res):
        print(f"[refresh] failed: {res.get('error_description', res)}")
        return False
    print(f"[refresh] token refreshed, valid until {time.strftime('%H:%M:%S', time.localtime(TOKENS['expires_at']))}")
    return True


def valid_token():
    """Return a non-expired access token, refreshing if needed."""
    if not TOKENS["access_token"]:
        return None
    if time.time() > TOKENS["expires_at"] - 120:
        refresh_now()
    return TOKENS["access_token"]


def fetch_email():
    try:
        me = S.get(
            f"{GRAPH}/me?$select=userPrincipalName,displayName",
            headers={"Authorization": f"Bearer {TOKENS['access_token']}"}, timeout=15,
        ).json()
        TOKENS["email"] = me.get("userPrincipalName") or me.get("displayName")
    except Exception:
        pass


def refresher_loop():
    while True:
        time.sleep(REFRESH_EVERY)
        if TOKENS.get("refresh_token"):
            refresh_now()


# ── Graph request helpers ─────────────────────────────────────────────────────
def H():
    return {"Authorization": f"Bearer {valid_token()}"}


def gh(path):
    return S.get(f"{GRAPH}{path}", headers=H(), timeout=30).json()


def gd(path):
    return S.delete(f"{GRAPH}{path}", headers=H(), timeout=30)


def gpost(path, body=None):
    return S.post(f"{GRAPH}{path}", headers={**H(), "Content-Type": "application/json"},
                         json=body, timeout=30).json()


def gpatch(path, body=None):
    return S.patch(f"{GRAPH}{path}", headers={**H(), "Content-Type": "application/json"},
                          json=body, timeout=30).json()


def signed_url(item_id):
    data = gh(f"/me/drive/items/{item_id}")
    if "error" in data:
        return None, data["error"].get("message", "Graph error")
    url = data.get("@microsoft.graph.downloadUrl")
    if not url:
        return None, "No download URL (folder or vault item)"
    return url, None


# ── Device-code auth flow ─────────────────────────────────────────────────────
def poll_device(device_code, interval):
    """Background: poll Microsoft until the user finishes the browser login."""
    _device["active"] = True
    _device["error"] = None
    while _device["active"]:
        time.sleep(interval)
        try:
            res = S.post(f"{AUTH_BASE}/token", data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": device_code,
                "client_id": CLIENT_ID,
            }, timeout=20).json()
        except Exception:
            continue
        if "access_token" in res:
            apply_token_response(res)
            _device["active"] = False
            print(f"[auth] connected as {TOKENS.get('email')}")
            return
        err = res.get("error")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval += 5
            continue
        # authorization_declined / expired_token / bad_verification_code
        _device["error"] = res.get("error_description", err)
        _device["active"] = False
        return


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = None  # uploads go direct to MS; proxy chunks are bounded


@app.route("/status")
def status():
    return jsonify({
        "authenticated": bool(TOKENS["access_token"]),
        "email": TOKENS.get("email"),
        "pending": _device["active"],
        "error": _device.get("error"),
    })


@app.route("/auth/start", methods=["POST"])
def auth_start():
    try:
        res = S.post(f"{AUTH_BASE}/devicecode",
                            data={"client_id": CLIENT_ID, "scope": SCOPES}, timeout=20).json()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})
    if "device_code" not in res:
        return jsonify({"ok": False, "error": res.get("error_description", "device code request failed")})
    _device["device_code"] = res["device_code"]
    _device["interval"] = res.get("interval", 5)
    threading.Thread(target=poll_device, args=(res["device_code"], _device["interval"]),
                     daemon=True).start()
    return jsonify({
        "ok": True,
        "user_code": res["user_code"],
        "verification_uri": res.get("verification_uri", "https://microsoft.com/devicelogin"),
        "expires_in": res.get("expires_in", 900),
    })


@app.route("/auth/reset", methods=["POST"])
def auth_reset():
    _device["active"] = False
    with _lock:
        TOKENS.update({"access_token": None, "refresh_token": None, "expires_at": 0, "email": None})
    try:
        if os.path.exists(ENV_PATH):
            os.remove(ENV_PATH)
    except Exception:
        pass
    return jsonify({"ok": True})


# ── Drive data ────────────────────────────────────────────────────────────────
@app.route("/storage")
def storage():
    q = gh("/me/drive").get("quota", {})
    return jsonify({"used": q.get("used", 0), "total": q.get("total", 0)})


@app.route("/ls")
def ls():
    item_id = request.args.get("id", "root")
    base = "/me/drive/root/children" if item_id == "root" else f"/me/drive/items/{item_id}/children"
    # $select keeps listings fast (full-item metadata is ~3x slower). Preview links come from
    # /link on demand, hidden behind hover-prefetch so opening still feels instant.
    items, url = [], f"{base}?$top=200&$select=id,name,size,folder,file,lastModifiedDateTime"
    while url:  # follow pagination so big folders list fully
        data = S.get(GRAPH + url if url.startswith("/") else url, headers=H(), timeout=30).json()
        if "error" in data:
            return jsonify({"error": data["error"].get("message", "error")})
        for i in data.get("value", []):
            items.append({
                "id": i["id"], "name": i["name"], "size": i.get("size"),
                "folder": "folder" in i, "modified": i.get("lastModifiedDateTime"),
            })
        url = data.get("@odata.nextLink")
    return jsonify({"items": items})


@app.route("/config")
def config():
    return jsonify({"turbo": TURBO})


@app.route("/link")
def link():
    """Return a fresh signed CDN url so the browser streams raw bytes directly from Microsoft."""
    url, err = signed_url(request.args.get("id"))
    if err:
        return jsonify({"error": err}), 404
    return jsonify({"url": url})


@app.route("/thumb")
def thumb():
    """
    Inline-serving thumbnail url for an image/video (Content-Disposition: inline, unlike the
    download.aspx content url which is attachment-only). Used for grid previews.
    """
    item_id = request.args.get("id")
    size = request.args.get("size", "medium")
    d = gh(f"/me/drive/items/{item_id}/thumbnails/0/{size}")
    if "error" in d or "url" not in d:
        return ("", 404)
    return redirect(d["url"], code=302)  # inline image, straight from the CDN


@app.route("/dl")
def dl():
    """302 straight to the signed CDN url — real raw streaming, range handled by Azure, zero host buffering."""
    url, err = signed_url(request.args.get("id"))
    if err:
        return err, 404
    return redirect(url, code=302)


@app.route("/stream")
def stream():
    """Same-origin range proxy. Used for the audio visualizer (needs same-origin) and as a CORS fallback."""
    item_id = request.args.get("id")
    url, err = signed_url(item_id)
    if err:
        return jsonify({"error": err}), 404
    upstream_headers = {}
    if "Range" in request.headers:
        upstream_headers["Range"] = request.headers["Range"]
    up = S.get(url, headers=upstream_headers, stream=True, timeout=60)
    resp_headers = {
        "Content-Type": up.headers.get("Content-Type", "application/octet-stream"),
        "Accept-Ranges": "bytes",
        "Cache-Control": "no-store",
    }
    for h in ("Content-Length", "Content-Range"):
        if h in up.headers:
            resp_headers[h] = up.headers[h]
    if request.args.get("dl"):
        resp_headers["Content-Disposition"] = f'attachment; filename="{request.args.get("name", "file")}"'
    return Response(stream_with_context(up.iter_content(256 * 1024)),
                    status=up.status_code if up.status_code in (200, 206) else 200,
                    headers=resp_headers)


@app.route("/raw")
def raw():
    """
    A stable, shareable RAW url that renders INLINE (does not download) and never expires.
    Microsoft's own content url (download.aspx) is attachment-only and its tempauth token signs
    the query string, so it can't be made inline. Instead we re-sign server-side on every hit and
    proxy with Content-Disposition: inline + range support — so /raw?id=... works forever (while the
    app runs) and opens in the browser like a raw file link.
    """
    item_id = request.args.get("id")
    url, err = signed_url(item_id)
    if err:
        return jsonify({"error": err}), 404
    upstream_headers = {}
    if "Range" in request.headers:
        upstream_headers["Range"] = request.headers["Range"]
    up = S.get(url, headers=upstream_headers, stream=True, timeout=60)
    resp_headers = {
        "Content-Type": up.headers.get("Content-Type", "application/octet-stream"),
        "Accept-Ranges": "bytes",
        "Content-Disposition": "inline",
        "Cache-Control": "public, max-age=3600",
    }
    for h in ("Content-Length", "Content-Range"):
        if h in up.headers:
            resp_headers[h] = up.headers[h]
    return Response(stream_with_context(up.iter_content(256 * 1024)),
                    status=up.status_code if up.status_code in (200, 206) else 200,
                    headers=resp_headers)


@app.route("/text")
def text():
    """Fetch a text file's contents server-side (avoids browser CORS for the editor)."""
    url, err = signed_url(request.args.get("id"))
    if err:
        return jsonify({"error": err}), 404
    r = S.get(url, timeout=60)
    return Response(r.content, content_type="text/plain; charset=utf-8")


@app.route("/save", methods=["POST"])
def save():
    """Overwrite an existing file's contents (editor save)."""
    d = request.get_json()
    r = S.put(f"{GRAPH}/me/drive/items/{d['id']}/content",
                     headers={**H(), "Content-Type": "text/plain"},
                     data=d["content"].encode("utf-8"), timeout=60)
    return jsonify({"ok": r.status_code in (200, 201)})


@app.route("/newfile", methods=["POST"])
def newfile():
    d = request.get_json()
    pid, name = d.get("parent_id", "root"), d["name"]
    if pid == "root":
        path = f"/me/drive/root:/{name}:/content"
    else:
        path = f"/me/drive/items/{pid}:/{name}:/content"
    r = S.put(f"{GRAPH}{path}", headers={**H(), "Content-Type": "text/plain"},
                     data=d.get("content", "").encode("utf-8"), timeout=60)
    return jsonify({"ok": r.status_code in (200, 201)})


@app.route("/mkdir", methods=["POST"])
def mkdir():
    d = request.get_json()
    pid = d["parent_id"]
    path = "/me/drive/root/children" if pid == "root" else f"/me/drive/items/{pid}/children"
    res = gpost(path, {"name": d["name"], "folder": {}, "@microsoft.graph.conflictBehavior": "rename"})
    return jsonify({"ok": "id" in res, "id": res.get("id")})


@app.route("/rename", methods=["POST"])
def rename():
    d = request.get_json()
    gpatch(f"/me/drive/items/{d['id']}", {"name": d["name"]})
    return jsonify({"ok": True})


@app.route("/delete", methods=["DELETE"])
def delete():
    gd(f"/me/drive/items/{request.args.get('id')}")
    return jsonify({"ok": True})


# ── Upload: browser talks straight to Microsoft ───────────────────────────────
@app.route("/upload/session", methods=["POST"])
def upload_session():
    """Create a resumable upload session; the browser PUTs chunks straight to Microsoft."""
    d = request.get_json()
    pid, name = d.get("parent_id", "root"), d["name"]
    if pid == "root":
        path = f"/me/drive/root:/{name}:/createUploadSession"
    else:
        path = f"/me/drive/items/{pid}:/{name}:/createUploadSession"
    res = gpost(path, {"item": {"@microsoft.graph.conflictBehavior": "replace",
                                "name": name}})
    if "uploadUrl" not in res:
        return jsonify({"ok": False, "error": res.get("error", res)})
    return jsonify({"ok": True, "uploadUrl": res["uploadUrl"]})


@app.route("/upload/proxy", methods=["PUT"])
def upload_proxy():
    """Fallback: forward one chunk to the upload session when the browser is CORS-blocked."""
    upload_url = request.args.get("url")
    if not upload_url:
        return jsonify({"error": "missing url"}), 400
    headers = {}
    if request.headers.get("Content-Range"):
        headers["Content-Range"] = request.headers["Content-Range"]
    chunk = request.get_data()
    headers["Content-Length"] = str(len(chunk))
    r = S.put(upload_url, headers=headers, data=chunk, timeout=300)
    return Response(r.content, status=r.status_code,
                    content_type=r.headers.get("Content-Type", "application/json"))


@app.route("/")
def index():
    return render_template_string(HTML)


# ══════════════════════════════════════════════════════════════════════════════
#  Front-end
# ══════════════════════════════════════════════════════════════════════════════
HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>OneDrive</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/ace/1.32.6/ace.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/ace/1.32.6/ext-modelist.min.js"></script>
<style>
:root{
  --accent:#7c6cff; --accent2:#a45cff; --accent-glow:rgba(124,108,255,.35);
  --danger:#ff5c7a; --ok:#3ee08f; --warn:#ffcf5c;
  --text0:#f4f4fb; --text1:#c2c2d8; --text2:#7c7c99;
  --glass:rgba(30,30,48,.55); --glass2:rgba(44,44,68,.55);
  --stroke:rgba(255,255,255,.09); --stroke2:rgba(255,255,255,.16);
  --radius:14px; --radius-lg:22px;
}
*{margin:0;padding:0;box-sizing:border-box}
html,body{height:100%}
body{
  font-family:'Segoe UI',system-ui,sans-serif;color:var(--text0);font-size:14px;overflow:hidden;
  background:
    radial-gradient(1200px 700px at 12% -10%, rgba(124,108,255,.25), transparent 60%),
    radial-gradient(1000px 800px at 110% 10%, rgba(164,92,255,.22), transparent 55%),
    radial-gradient(900px 900px at 50% 120%, rgba(62,224,143,.10), transparent 60%),
    #08070f;
}
.glass{background:var(--glass);backdrop-filter:blur(22px) saturate(140%);-webkit-backdrop-filter:blur(22px) saturate(140%);border:1px solid var(--stroke)}
button{font-family:inherit}
::-webkit-scrollbar{width:8px;height:8px}
::-webkit-scrollbar-thumb{background:var(--stroke2);border-radius:4px}
::-webkit-scrollbar-thumb:hover{background:var(--text2)}
input[type=checkbox]{width:15px;height:15px;accent-color:var(--accent);cursor:pointer}

/* ── Auth ── */
#auth{position:fixed;inset:0;display:flex;align-items:center;justify-content:center;z-index:50}
.auth-card{width:460px;padding:38px 34px;border-radius:var(--radius-lg);display:flex;flex-direction:column;gap:22px;
  box-shadow:0 30px 90px rgba(0,0,0,.6),0 0 0 1px var(--stroke) inset}
.auth-top{display:flex;align-items:center;gap:14px}
.brand-mark{width:46px;height:46px;border-radius:13px;display:flex;align-items:center;justify-content:center;font-size:20px;color:#fff;
  background:linear-gradient(135deg,var(--accent),var(--accent2));box-shadow:0 8px 26px var(--accent-glow)}
.auth-top h1{font-size:1.25rem;font-weight:650}
.auth-top p{font-size:.78rem;color:var(--text2);margin-top:2px}
.status-row{display:flex;align-items:center;gap:10px;font-size:.85rem;color:var(--text1)}
.dot{width:11px;height:11px;border-radius:50%;background:var(--text2);flex-shrink:0;transition:.3s}
.dot.pending{background:var(--warn);box-shadow:0 0 0 0 var(--warn);animation:pulse 1.3s infinite}
.dot.ok{background:var(--ok);box-shadow:0 0 14px var(--ok)}
.dot.err{background:var(--danger);box-shadow:0 0 14px var(--danger)}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(255,207,92,.5)}70%{box-shadow:0 0 0 9px rgba(255,207,92,0)}100%{box-shadow:0 0 0 0 rgba(255,207,92,0)}}
.code-box{background:rgba(0,0,0,.35);border:1px solid var(--stroke2);border-radius:var(--radius);padding:18px;display:flex;flex-direction:column;gap:12px}
.code-lbl{font-size:.72rem;color:var(--text2);text-transform:uppercase;letter-spacing:.08em}
.code-val{font-size:2rem;font-weight:700;letter-spacing:.22em;font-family:'Consolas',monospace;color:#fff;text-align:center;
  text-shadow:0 0 22px var(--accent-glow);cursor:pointer;user-select:all}
.copy-hint{font-size:.68rem;color:var(--text2);text-align:center}
.btn{border:none;border-radius:var(--radius);padding:13px;font-size:.95rem;font-weight:650;cursor:pointer;color:#fff;
  background:linear-gradient(135deg,var(--accent),var(--accent2));box-shadow:0 10px 30px var(--accent-glow);transition:transform .1s,filter .15s;
  display:flex;align-items:center;justify-content:center;gap:9px;text-decoration:none}
.btn:hover{filter:brightness(1.08)}
.btn:active{transform:scale(.98)}
.btn.ghost{background:var(--glass2);box-shadow:none;border:1px solid var(--stroke2)}
.link-btn{display:flex;align-items:center;justify-content:center;gap:9px;padding:12px;border-radius:var(--radius);
  border:1px solid var(--stroke2);background:rgba(0,0,0,.25);color:var(--text0);font-weight:600;text-decoration:none;font-size:.9rem}
.link-btn:hover{border-color:var(--accent)}

/* ── App shell ── */
#app{display:none;height:100%;padding:14px;gap:14px}
#app.show{display:flex}
#side{width:236px;flex-shrink:0;border-radius:var(--radius-lg);padding:16px;display:flex;flex-direction:column;gap:16px}
.side-brand{display:flex;align-items:center;gap:11px}
.side-brand .brand-mark{width:38px;height:38px;font-size:16px;border-radius:11px}
.side-brand h2{font-size:1rem;font-weight:650}
.side-brand small{font-size:.68rem;color:var(--text2)}
.store{padding:13px;border-radius:var(--radius);background:rgba(0,0,0,.22);border:1px solid var(--stroke)}
.store-top{display:flex;justify-content:space-between;font-size:.74rem;color:var(--text2);margin-bottom:9px}
.store-top b{color:var(--text1)}
.track{height:6px;border-radius:3px;background:rgba(255,255,255,.08);overflow:hidden}
.track>div{height:100%;border-radius:3px;width:0;background:linear-gradient(90deg,var(--accent),var(--accent2));transition:width .6s}
.store-sub{font-size:.68rem;color:var(--text2);margin-top:7px}
.nav{display:flex;flex-direction:column;gap:3px}
.nav-h{font-size:.66rem;color:var(--text2);text-transform:uppercase;letter-spacing:.09em;padding:8px 8px 4px}
.nav-i{display:flex;align-items:center;gap:11px;padding:9px 11px;border-radius:11px;color:var(--text1);font-size:.86rem;cursor:pointer;transition:.13s}
.nav-i:hover{background:rgba(255,255,255,.05);color:var(--text0)}
.nav-i.active{background:var(--accent-glow);color:#fff}
.nav-i i{width:17px;text-align:center}
.side-foot{margin-top:auto;display:flex;align-items:center;gap:10px;padding-top:13px;border-top:1px solid var(--stroke)}
.avatar{width:33px;height:33px;border-radius:50%;background:linear-gradient(135deg,var(--accent),var(--accent2));display:flex;align-items:center;justify-content:center;font-size:12px;color:#fff}
.side-foot .who{font-size:.78rem;color:var(--text1);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.side-foot .who small{display:block;color:var(--text2);font-size:.66rem;cursor:pointer}
.side-foot .who small:hover{color:var(--danger)}

#main{flex:1;display:flex;flex-direction:column;border-radius:var(--radius-lg);overflow:hidden;min-width:0}
#top{display:flex;align-items:center;gap:12px;padding:13px 16px;border-bottom:1px solid var(--stroke)}
#crumbs{display:flex;align-items:center;gap:5px;flex:1;flex-wrap:wrap;min-width:0}
.crumb{color:var(--accent);cursor:pointer;font-size:.86rem;padding:3px 7px;border-radius:7px}
.crumb:hover{background:var(--accent-glow);color:#fff}
.crumb.cur{color:var(--text1);cursor:default}
.sep{color:var(--text2);font-size:.7rem}
.search{position:relative}
.search i{position:absolute;left:12px;top:50%;transform:translateY(-50%);color:var(--text2);font-size:12px}
.search input{width:180px;padding:8px 12px 8px 32px;border-radius:11px;border:1px solid var(--stroke2);background:rgba(0,0,0,.25);color:var(--text0);outline:none;transition:.2s;font-size:.84rem}
.search input:focus{width:230px;border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-glow)}
.ico{width:34px;height:34px;border-radius:11px;border:1px solid var(--stroke2);background:rgba(0,0,0,.22);color:var(--text1);cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:13px}
.ico:hover{color:#fff;border-color:var(--accent)}
#tools{display:flex;align-items:center;gap:8px;padding:11px 16px;border-bottom:1px solid var(--stroke);flex-wrap:wrap}
.tb{display:flex;align-items:center;gap:7px;padding:8px 14px;border-radius:11px;border:1px solid var(--stroke2);background:rgba(255,255,255,.04);color:var(--text1);font-size:.82rem;cursor:pointer;white-space:nowrap;transition:.13s}
.tb:hover{color:#fff;background:rgba(255,255,255,.08)}
.tb.pri{background:linear-gradient(135deg,var(--accent),var(--accent2));color:#fff;border-color:transparent;box-shadow:0 6px 20px var(--accent-glow)}
.tb.dng:hover{background:rgba(255,92,122,.16);border-color:var(--danger);color:var(--danger)}
.tb i{font-size:12px}
.tb-sep{width:1px;height:22px;background:var(--stroke);margin:0 3px}
.view-toggle{margin-left:auto;display:flex;gap:6px}
.vb{width:32px;height:32px;border-radius:9px;border:1px solid var(--stroke2);background:rgba(0,0,0,.2);color:var(--text2);cursor:pointer}
.vb.active,.vb:hover{color:#fff;border-color:var(--accent)}

#area{flex:1;overflow:auto;position:relative}
#area.drag::after{content:'Drop to upload';position:absolute;inset:12px;border:2.5px dashed var(--accent);border-radius:var(--radius-lg);
  display:flex;align-items:center;justify-content:center;font-size:1.4rem;color:#fff;background:var(--accent-glow);z-index:5;pointer-events:none}
table{width:100%;border-collapse:collapse}
thead th{position:sticky;top:0;background:rgba(20,20,34,.9);backdrop-filter:blur(8px);padding:10px 14px;font-size:.7rem;color:var(--text2);text-transform:uppercase;letter-spacing:.07em;text-align:left;cursor:pointer;user-select:none;z-index:2}
thead th:hover{color:var(--text1)}
tbody tr{border-bottom:1px solid rgba(255,255,255,.04);cursor:pointer;transition:background .1s}
tbody tr:hover{background:rgba(255,255,255,.04)}
tbody tr.sel{background:var(--accent-glow)}
td{padding:9px 14px;font-size:.86rem}
.c-chk{width:38px}.c-ic{width:34px;padding-right:0!important}
.c-size{width:96px;text-align:right;color:var(--text2);font-variant-numeric:tabular-nums;font-size:.8rem}
.c-type{width:78px;color:var(--text2);font-size:.8rem}
.c-date{width:128px;color:var(--text2);font-size:.8rem}
.c-act{width:118px;text-align:right}
.fname{display:flex;align-items:center;gap:9px}
.fname span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:44ch}
.fi{font-size:16px}
.fi.folder{color:#ffcf5c}.fi.image{color:#5cc8ff}.fi.video{color:#ff7ce0}.fi.audio{color:#3ee08f}
.fi.pdf{color:#ff5c7a}.fi.doc{color:#5c9dff}.fi.sheet{color:#3ee08f}.fi.code{color:#ffcf5c}.fi.archive{color:#ffb15c}.fi.def{color:var(--text2)}
.racts{display:flex;gap:5px;justify-content:flex-end;opacity:0;transition:.15s}
tr:hover .racts{opacity:1}
.ra{width:26px;height:26px;border-radius:8px;border:1px solid var(--stroke2);background:rgba(0,0,0,.25);color:var(--text2);cursor:pointer;font-size:11px}
.ra:hover{color:#fff;border-color:var(--accent)}
.ra.dng:hover{color:var(--danger);border-color:var(--danger)}

#grid{display:none;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:12px;padding:16px}
.gi{position:relative;border-radius:var(--radius);border:1px solid var(--stroke);background:rgba(255,255,255,.03);padding:18px 12px 13px;display:flex;flex-direction:column;align-items:center;gap:11px;cursor:pointer;transition:.15s;text-align:center}
.gi:hover{background:rgba(255,255,255,.06);transform:translateY(-2px)}
.gi.sel{border-color:var(--accent);background:var(--accent-glow)}
.gi .fi{font-size:38px}
.gi .gn{font-size:.78rem;word-break:break-word;line-height:1.35}
.gi .gs{font-size:.7rem;color:var(--text2)}
.gi .gthumb{width:100%;height:92px;object-fit:cover;border-radius:10px;background:rgba(0,0,0,.25)}
.gi .gchk{position:absolute;top:8px;left:8px;opacity:0;z-index:2}
.gi:hover .gchk,.gi.sel .gchk{opacity:1}

#empty{display:none;flex-direction:column;align-items:center;justify-content:center;height:100%;gap:14px;color:var(--text2)}
#empty i{font-size:52px;opacity:.28}
#status{display:flex;align-items:center;justify-content:space-between;padding:7px 16px;border-top:1px solid var(--stroke);font-size:.74rem;color:var(--text2)}
.spin{display:inline-block;width:13px;height:13px;border:2px solid var(--stroke2);border-top-color:var(--accent);border-radius:50%;animation:sp .7s linear infinite}
@keyframes sp{to{transform:rotate(360deg)}}

/* ── Context menu ── */
#ctx{display:none;position:fixed;z-index:400;min-width:186px;padding:6px;border-radius:var(--radius);box-shadow:0 16px 48px rgba(0,0,0,.6)}
.ci{display:flex;align-items:center;gap:11px;padding:9px 12px;border-radius:9px;font-size:.84rem;color:var(--text1);cursor:pointer}
.ci:hover{background:rgba(255,255,255,.07);color:#fff}
.ci i{width:15px;text-align:center;color:var(--text2)}
.ci:hover i{color:var(--accent)}
.ci.dng{color:var(--danger)} .ci.dng i{color:var(--danger)}
.ci-sep{height:1px;background:var(--stroke);margin:5px 0}

/* ── Modals ── */
.mask{display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);backdrop-filter:blur(5px);z-index:300;align-items:center;justify-content:center}
.mask.open{display:flex}
.dlg{border-radius:var(--radius-lg);padding:24px;width:420px;box-shadow:0 24px 70px rgba(0,0,0,.6)}
.dlg h3{font-size:1rem;margin-bottom:16px;display:flex;align-items:center;gap:9px}
.dlg input{width:100%;padding:11px 13px;border-radius:11px;border:1px solid var(--stroke2);background:rgba(0,0,0,.3);color:var(--text0);outline:none;font-size:.9rem;margin-bottom:16px}
.dlg input:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-glow)}
.dlg-acts{display:flex;gap:9px;justify-content:flex-end}
.mb{padding:9px 17px;border-radius:11px;font-size:.85rem;cursor:pointer;border:1px solid var(--stroke2);background:var(--glass2);color:var(--text1)}
.mb.pri{background:linear-gradient(135deg,var(--accent),var(--accent2));color:#fff;border-color:transparent;font-weight:600}

/* ── Preview ── */
#pv{display:none;position:fixed;inset:0;z-index:250;background:rgba(4,4,10,.72);backdrop-filter:blur(8px);align-items:center;justify-content:center;padding:26px}
#pv.open{display:flex}
#pvbox{width:min(1180px,94vw);height:min(88vh,900px);border-radius:var(--radius-lg);display:flex;flex-direction:column;overflow:hidden;box-shadow:0 30px 90px rgba(0,0,0,.7)}
#pvhead{display:flex;align-items:center;gap:12px;padding:13px 16px;border-bottom:1px solid var(--stroke)}
#pvhead .pt{flex:1;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#pvhead .pm{font-size:.75rem;color:var(--text2)}
#pvbody{flex:1;overflow:auto;display:flex;background:#07060d;position:relative}
#pvbody img{max-width:100%;max-height:100%;margin:auto;object-fit:contain;padding:20px}
#pvbody iframe{width:100%;height:100%;border:none;background:#fff}
.pvload{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;gap:10px;color:var(--text2)}
#ace{width:100%;height:100%}

/* ── Custom video player ── */
.vp{margin:auto;width:100%;max-width:960px;padding:20px}
.vp-shell{position:relative;border-radius:18px;overflow:hidden;background:#000;box-shadow:0 0 0 1px var(--stroke2),0 20px 60px rgba(0,0,0,.6)}
.vp video{display:block;width:100%;max-height:64vh;background:#000}
.vp-ctrl{position:absolute;left:0;right:0;bottom:0;padding:12px 14px 10px;display:flex;flex-direction:column;gap:8px;
  background:linear-gradient(transparent,rgba(0,0,0,.82));opacity:0;transition:.25s}
.vp-shell:hover .vp-ctrl,.vp-shell.paused .vp-ctrl{opacity:1}
.vp-bar{height:6px;border-radius:3px;background:rgba(255,255,255,.2);cursor:pointer;position:relative}
.vp-buf{position:absolute;height:100%;border-radius:3px;background:rgba(255,255,255,.25);width:0}
.vp-fill{position:absolute;height:100%;border-radius:3px;background:linear-gradient(90deg,var(--accent),var(--accent2));width:0}
.vp-bar:hover .vp-fill{box-shadow:0 0 12px var(--accent-glow)}
.vp-row{display:flex;align-items:center;gap:14px;color:#fff}
.vp-row button{background:none;border:none;color:#fff;cursor:pointer;font-size:15px;opacity:.9}
.vp-row button:hover{opacity:1;color:var(--accent)}
.vp-time{font-size:.76rem;font-variant-numeric:tabular-nums;color:#e6e6f2}
.vp-vol{width:78px;height:4px;accent-color:var(--accent)}
.vp-spacer{flex:1}
.vp-big{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;pointer-events:none}
.vp-big i{font-size:64px;color:#fff;opacity:0;transform:scale(.6);text-shadow:0 4px 30px rgba(0,0,0,.6)}
.vp-big.show i{animation:pop .5s ease}
@keyframes pop{0%{opacity:.9;transform:scale(.7)}100%{opacity:0;transform:scale(1.3)}}

/* ── Custom audio player (glowing, dynamic) ── */
.ap{margin:auto;width:min(560px,90%);padding:26px}
.ap-card{position:relative;border-radius:26px;padding:26px 24px 22px;overflow:hidden;
  background:linear-gradient(160deg,rgba(40,32,72,.85),rgba(22,18,40,.9));
  border:1px solid var(--stroke2);box-shadow:0 22px 60px rgba(0,0,0,.55)}
.ap-card::before{content:'';position:absolute;inset:-2px;border-radius:28px;padding:2px;z-index:0;
  background:conic-gradient(from var(--ang,0deg),var(--accent),var(--accent2),#3ee08f,var(--accent));
  -webkit-mask:linear-gradient(#000 0 0) content-box,linear-gradient(#000 0 0);-webkit-mask-composite:xor;mask-composite:exclude;
  opacity:.0;transition:opacity .4s;filter:blur(.4px)}
.ap-card.playing::before{opacity:.9;animation:spin-ang 6s linear infinite}
@keyframes spin-ang{to{--ang:360deg}}
.ap-inner{position:relative;z-index:1;display:flex;flex-direction:column;gap:18px}
.ap-disc{width:120px;height:120px;border-radius:50%;margin:0 auto;display:flex;align-items:center;justify-content:center;
  background:radial-gradient(circle at 50% 50%,#2a2350,#141026 70%);border:1px solid var(--stroke2);
  box-shadow:0 0 0 6px rgba(0,0,0,.3),0 0 40px var(--accent-glow)}
.ap-disc i{font-size:40px;color:var(--accent);text-shadow:0 0 22px var(--accent-glow)}
.ap-card.playing .ap-disc{animation:disc 8s linear infinite}
@keyframes disc{to{transform:rotate(360deg)}}
.ap-name{text-align:center;font-weight:600;font-size:.95rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ap-viz{display:flex;align-items:flex-end;justify-content:center;gap:3px;height:46px}
.ap-viz span{width:4px;border-radius:2px;background:linear-gradient(var(--accent),var(--accent2));height:6px;transition:height .09s}
.ap-bar{height:6px;border-radius:3px;background:rgba(255,255,255,.14);cursor:pointer;position:relative}
.ap-fill{position:absolute;height:100%;border-radius:3px;width:0;background:linear-gradient(90deg,var(--accent),var(--accent2));box-shadow:0 0 12px var(--accent-glow)}
.ap-row{display:flex;align-items:center;gap:16px;justify-content:center}
.ap-row button{width:46px;height:46px;border-radius:50%;border:none;cursor:pointer;color:#fff;font-size:16px;
  background:linear-gradient(135deg,var(--accent),var(--accent2));box-shadow:0 8px 24px var(--accent-glow)}
.ap-row .mini{width:36px;height:36px;background:rgba(255,255,255,.08);box-shadow:none;color:var(--text1)}
.ap-time{display:flex;justify-content:space-between;font-size:.72rem;color:var(--text2);font-variant-numeric:tabular-nums}

/* upload toast */
#utoast{display:none;position:fixed;bottom:22px;right:22px;z-index:500;width:320px;padding:15px 16px;border-radius:var(--radius);box-shadow:0 16px 48px rgba(0,0,0,.5)}
.ut-h{display:flex;align-items:center;gap:9px;margin-bottom:11px}
.ut-h i{color:var(--accent)}
.ut-h span{flex:1;font-size:.85rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ut-h small{color:var(--text2);font-size:.72rem}
.ut-track{height:6px;border-radius:3px;background:rgba(255,255,255,.1);overflow:hidden}
.ut-fill{height:100%;width:0;border-radius:3px;background:linear-gradient(90deg,var(--accent),var(--accent2));transition:width .15s}
.ut-rate{font-size:.7rem;color:var(--text2);margin-top:6px;text-align:right}
</style>
</head>
<body>

<!-- AUTH -->
<div id="auth">
  <div class="auth-card glass">
    <div class="auth-top">
      <div class="brand-mark"><i class="fa-brands fa-microsoft"></i></div>
      <div><h1>OneDrive</h1><p>Device-code sign-in</p></div>
    </div>
    <div class="status-row"><span class="dot" id="dot"></span><span id="statusText">Checking session…</span></div>
    <div id="preAuth">
      <button class="btn" id="startBtn" onclick="startAuth()"><i class="fa-solid fa-right-to-bracket"></i> Sign in with Microsoft</button>
    </div>
    <div id="codeWrap" style="display:none;flex-direction:column;gap:16px">
      <div class="code-box">
        <div class="code-lbl">Your code</div>
        <div class="code-val" id="codeVal" onclick="copyCode()">— — — — —</div>
        <div class="copy-hint" id="copyHint">tap to copy</div>
      </div>
      <a class="link-btn" id="verifyLink" href="https://microsoft.com/devicelogin" target="_blank"><i class="fa-solid fa-arrow-up-right-from-square"></i> Open microsoft.com/devicelogin</a>
      <div class="status-row" style="justify-content:center"><span class="dot pending" id="waitDot"></span><span style="font-size:.8rem;color:var(--text2)">Waiting for you to approve…</span></div>
    </div>
  </div>
</div>

<!-- APP -->
<div id="app">
  <aside id="side" class="glass">
    <div class="side-brand">
      <div class="brand-mark"><i class="fa-brands fa-microsoft"></i></div>
      <div><h2>OneDrive</h2><small>Explorer</small></div>
    </div>
    <div class="store">
      <div class="store-top"><span><i class="fa-solid fa-hard-drive"></i> Storage</span><b id="stPct">—</b></div>
      <div class="track"><div id="stFill"></div></div>
      <div class="store-sub" id="stSub"></div>
    </div>
    <div class="nav">
      <div class="nav-h">Navigation</div>
      <div class="nav-i active" onclick="goRoot()"><i class="fa-solid fa-house"></i> My Drive</div>
      <div class="nav-h" style="margin-top:6px">Create</div>
      <label class="nav-i" style="cursor:pointer"><i class="fa-solid fa-arrow-up-from-bracket"></i> Upload files<input type="file" multiple hidden onchange="upload(this.files)"></label>
      <div class="nav-i" onclick="mkdir()"><i class="fa-solid fa-folder-plus"></i> New folder</div>
      <div class="nav-i" onclick="newTextFile()"><i class="fa-solid fa-file-circle-plus"></i> New file</div>
    </div>
    <div class="side-foot">
      <div class="avatar"><i class="fa-solid fa-user"></i></div>
      <div class="who"><span id="who">Connected</span><small onclick="signOut()">Sign out</small></div>
    </div>
  </aside>

  <section id="main" class="glass">
    <div id="top">
      <div id="crumbs"></div>
      <div class="search"><i class="fa-solid fa-magnifying-glass"></i><input id="filter" placeholder="Filter…" oninput="setFilter(this.value)"></div>
      <button class="ico" onclick="up()" title="Up"><i class="fa-solid fa-arrow-up"></i></button>
      <button class="ico" onclick="refresh()" title="Refresh"><i class="fa-solid fa-rotate-right"></i></button>
    </div>
    <div id="tools">
      <label class="tb pri"><i class="fa-solid fa-arrow-up-from-bracket"></i> Upload<input type="file" multiple hidden onchange="upload(this.files)"></label>
      <button class="tb" onclick="mkdir()"><i class="fa-solid fa-folder-plus"></i> Folder</button>
      <div class="tb-sep"></div>
      <button class="tb" onclick="dlSelected()"><i class="fa-solid fa-download"></i> Download</button>
      <button class="tb" onclick="renameSel()"><i class="fa-solid fa-pencil"></i> Rename</button>
      <button class="tb dng" onclick="delSelected()"><i class="fa-solid fa-trash"></i> Delete</button>
      <div class="tb-sep"></div>
      <button class="tb" id="turboBtn" onclick="toggleTurbo()" title="Turbo routes uploads through the server (~44 MB/s) instead of browser→Microsoft direct (~13 MB/s). Uses server bandwidth — leave OFF on Wasmer."><i class="fa-solid fa-bolt"></i> Turbo</button>
      <div class="view-toggle">
        <button class="vb active" id="vList" onclick="setView('list')"><i class="fa-solid fa-list"></i></button>
        <button class="vb" id="vGrid" onclick="setView('grid')"><i class="fa-solid fa-border-all"></i></button>
      </div>
    </div>
    <div id="area">
      <table id="tbl">
        <thead><tr>
          <th class="c-chk"><input type="checkbox" id="all" onclick="selAll(this)"></th>
          <th class="c-ic"></th>
          <th onclick="sortBy('name')">Name</th>
          <th class="c-type" onclick="sortBy('type')">Type</th>
          <th class="c-size" onclick="sortBy('size')">Size</th>
          <th class="c-date" onclick="sortBy('date')">Modified</th>
          <th class="c-act"></th>
        </tr></thead>
        <tbody id="tbody"></tbody>
      </table>
      <div id="grid"></div>
      <div id="empty"><i class="fa-solid fa-folder-open"></i><p>This folder is empty</p></div>
    </div>
    <div id="status"><span id="stMsg">Ready</span><span id="stSel"></span></div>
  </section>
</div>

<!-- preview -->
<div id="pv"><div id="pvbox" class="glass">
  <div id="pvhead">
    <span id="pvicon"></span><span class="pt" id="pvtitle">Preview</span><span class="pm" id="pvmeta"></span>
    <button class="tb" id="pvSave" style="display:none" onclick="saveEdit()"><i class="fa-solid fa-floppy-disk"></i> Save</button>
    <button class="tb" id="pvRun" style="display:none" onclick="runHtml()"><i class="fa-solid fa-play"></i> Preview</button>
    <button class="tb" id="pvRawBtn" onclick="copyRaw(pvItem&&pvItem.id)" title="Copy a raw inline link that opens in the browser instead of downloading"><i class="fa-solid fa-link"></i> Raw link</button>
    <button class="tb" onclick="pvDownload()"><i class="fa-solid fa-download"></i></button>
    <button class="ico" onclick="closePv()"><i class="fa-solid fa-xmark"></i></button>
  </div>
  <div id="pvbody"></div>
</div></div>

<!-- rename / new -->
<div class="mask" id="renM"><div class="dlg glass">
  <h3><i class="fa-solid fa-pencil" style="color:var(--accent)"></i> Rename</h3>
  <input id="renInput"><div class="dlg-acts"><button class="mb" onclick="closeMask('renM')">Cancel</button><button class="mb pri" onclick="doRename()">Rename</button></div>
</div></div>

<div id="ctx" class="glass">
  <div class="ci" onclick="ctxOpen()"><i class="fa-solid fa-eye"></i> Open / Preview</div>
  <div class="ci" onclick="ctxDl()"><i class="fa-solid fa-download"></i> Download</div>
  <div class="ci" onclick="ctxRaw()"><i class="fa-solid fa-link"></i> Copy raw link</div>
  <div class="ci-sep"></div>
  <div class="ci" onclick="ctxRen()"><i class="fa-solid fa-pencil"></i> Rename</div>
  <div class="ci" onclick="ctxCopy()"><i class="fa-solid fa-copy"></i> Copy name</div>
  <div class="ci-sep"></div>
  <div class="ci dng" onclick="ctxDel()"><i class="fa-solid fa-trash"></i> Delete</div>
</div>

<div id="utoast" class="glass">
  <div class="ut-h"><i class="fa-solid fa-arrow-up-from-bracket"></i><span id="utName">Uploading…</span><small id="utCount"></small></div>
  <div class="ut-track"><div class="ut-fill" id="utFill"></div></div>
  <div class="ut-rate" id="utRate"></div>
</div>

<script>
const IMG=new Set(['jpg','jpeg','png','gif','webp','bmp','svg','avif','ico']);
const VID=new Set(['mp4','webm','mov','mkv','m4v','ogv']);
const AUD=new Set(['mp3','wav','ogg','flac','aac','m4a','opus']);
const TXT=new Set(['txt','md','markdown','py','js','mjs','ts','tsx','jsx','css','scss','json','xml','csv','tsv','sh','bash','yaml','yml','log','ini','toml','rs','go','c','cc','cpp','h','hpp','java','php','rb','swift','kt','sql','html','htm','svg','vue','r','pl','lua','dart','env','gitignore','dockerfile','conf','cfg']);
const HTMLX=new Set(['html','htm']);

let stack=[{id:'root',name:'My Drive'}], items=[], sel={}, ctxItem=null, pvItem=null, renTarget=null;
let sort={col:'name',asc:true}, view='list', filter='';
const ext=n=>(n||'').split('.').pop().toLowerCase();
const $=id=>document.getElementById(id);

/* ── Auth ── */
let authTimer=null;
async function poll(){
  const s=await fetch('/status').then(r=>r.json()).catch(()=>({}));
  if(s.authenticated){ clearInterval(authTimer); enterApp(s.email); return; }
  if(s.pending){ setDot('pending','Waiting for approval…'); }
  else if(s.error){ setDot('err','Sign-in failed: '+s.error); $('codeWrap').style.display='none'; $('preAuth').style.display='block'; $('startBtn').innerHTML='<i class="fa-solid fa-rotate-right"></i> Try again'; }
}
function setDot(cls,txt){ $('dot').className='dot '+cls; if(txt)$('statusText').textContent=txt; }
async function startAuth(){
  $('startBtn').innerHTML='<span class="spin"></span> Requesting code…';
  const r=await fetch('/auth/start',{method:'POST'}).then(r=>r.json());
  if(!r.ok){ setDot('err','Error: '+r.error); $('startBtn').innerHTML='<i class="fa-solid fa-rotate-right"></i> Retry'; return; }
  $('preAuth').style.display='none'; $('codeWrap').style.display='flex';
  $('codeVal').textContent=r.user_code; $('verifyLink').href=r.verification_uri;
  setDot('pending','Enter the code to continue');
  if(authTimer) clearInterval(authTimer); authTimer=setInterval(poll,2500);
}
function copyCode(){ navigator.clipboard.writeText($('codeVal').textContent.trim()); $('copyHint').textContent='copied!'; setTimeout(()=>$('copyHint').textContent='tap to copy',1500); }
async function enterApp(email){
  setDot('ok','Connected');
  $('auth').style.display='none'; $('app').classList.add('show');
  if(email){ $('who').textContent=email; }
  // turbo default from server (TURBO_UPLOAD env), overridable per-browser
  try{ const cfg=await fetch('/config').then(r=>r.json()); TURBO=(localStorage.getItem('od_turbo')??(cfg.turbo?'1':'0'))==='1'; }catch(e){}
  $('turboBtn').classList.toggle('pri',TURBO);
  loadStorage(); load('root');
}
async function signOut(){ await fetch('/auth/reset',{method:'POST'}); location.reload(); }

/* ── Listing ── */
function setStatus(m,raw){ const e=$('stMsg'); raw?e.innerHTML=m:e.textContent=m; }
async function loadStorage(){
  const d=await fetch('/storage').then(r=>r.json()).catch(()=>({}));
  if(d.total){ const p=d.used/d.total*100; $('stFill').style.width=p.toFixed(1)+'%'; $('stPct').textContent=p.toFixed(3)+'%';
    $('stSub').textContent=fmtSize(d.used)+' of '+fmtSize(d.total);
    if(p>90)$('stFill').style.background='linear-gradient(90deg,var(--danger),#ff8c5c)'; }
}
async function load(id){
  sel={}; $('all').checked=false; setStatus('<span class="spin"></span> Loading…',true);
  const d=await fetch('/ls?id='+encodeURIComponent(id)).then(r=>r.json());
  if(d.error){ setStatus('Error: '+d.error); return; }
  items=d.items; render(); crumbs(); setStatus(items.length+' items'); selStatus();
}
function drill(id,name){ stack.push({id,name}); load(id); }
function goRoot(){ stack=[{id:'root',name:'My Drive'}]; load('root'); }
function up(){ if(stack.length>1)stack.pop(); load(stack[stack.length-1].id); }
function refresh(){ load(stack[stack.length-1].id); }
function goto(id){ const i=stack.findIndex(p=>p.id===id); if(i>=0)stack=stack.slice(0,i+1); load(id); }
function setFilter(q){ filter=q; render(); }
function crumbs(){
  $('crumbs').innerHTML=stack.map((p,i)=>i<stack.length-1
    ? `<span class="crumb" onclick="goto('${p.id}')">${esc(p.name)}</span><span class="sep"><i class="fa-solid fa-chevron-right"></i></span>`
    : `<span class="crumb cur">${esc(p.name)}</span>`).join('');
}
function sortBy(c){ sort.col===c?sort.asc=!sort.asc:(sort={col:c,asc:true}); render(); }
function setView(v){ view=v; $('vList').classList.toggle('active',v==='list'); $('vGrid').classList.toggle('active',v==='grid'); render(); }

function render(){
  const q=filter.toLowerCase();
  let list=q?items.filter(i=>i.name.toLowerCase().includes(q)):[...items];
  list.sort((a,b)=>{ if(a.folder!==b.folder)return a.folder?-1:1;
    let va,vb; const c=sort.col;
    if(c==='size'){va=a.size||0;vb=b.size||0;} else if(c==='date'){va=a.modified||'';vb=b.modified||'';}
    else if(c==='type'){va=ext(a.name);vb=ext(b.name);} else {va=a.name.toLowerCase();vb=b.name.toLowerCase();}
    return sort.asc?(va>vb?1:va<vb?-1:0):(va<vb?1:va>vb?-1:0); });
  $('empty').style.display=list.length?'none':'flex';
  $('tbl').style.display=view==='list'?'':'none'; $('grid').style.display=view==='grid'?'grid':'none';
  view==='list'?renderList(list):renderGrid(list);
}
function renderList(list){
  const tb=$('tbody'); tb.innerHTML='';
  for(const it of list){
    const f=it.folder, e=ext(it.name), tr=document.createElement('tr');
    if(sel[it.id])tr.classList.add('sel');
    tr.innerHTML=`
      <td class="c-chk"><input type="checkbox" ${sel[it.id]?'checked':''} onchange="toggle(this,'${it.id}')" onclick="event.stopPropagation()"></td>
      <td class="c-ic"><i class="${faicon(it)} fi ${cls(it)}"></i></td>
      <td><div class="fname"><span title="${esc(it.name)}">${esc(it.name)}</span></div></td>
      <td class="c-type">${f?'Folder':(e.toUpperCase()||'File')}</td>
      <td class="c-size">${f?'—':fmtSize(it.size)}</td>
      <td class="c-date">${fmtDate(it.modified)}</td>
      <td class="c-act"><div class="racts">
        ${!f?`<button class="ra" title="Open" onclick="event.stopPropagation();openItem(${jj(it)})"><i class="fa-solid fa-eye"></i></button>`:''}
        <button class="ra" title="Download" onclick="event.stopPropagation();download('${it.id}','${esc(it.name)}')"><i class="fa-solid fa-download"></i></button>
        <button class="ra" title="Rename" onclick="event.stopPropagation();openRename(${jj(it)})"><i class="fa-solid fa-pencil"></i></button>
        <button class="ra dng" title="Delete" onclick="event.stopPropagation();delOne('${it.id}')"><i class="fa-solid fa-trash"></i></button>
      </div></td>`;
    tr.onclick=e2=>{ if(e2.target.type==='checkbox')return; f?drill(it.id,it.name):openItem(it); };
    tr.oncontextmenu=e2=>{ e2.preventDefault(); showCtx(e2,it); };
    if(!f){ tr.onmouseenter=()=>prefetch(it.id); tr.onmouseleave=prefetchCancel; }
    tb.appendChild(tr);
  }
}
function renderGrid(list){
  const g=$('grid'); g.innerHTML='';
  for(const it of list){
    const d=document.createElement('div'); d.className='gi'+(sel[it.id]?' sel':'');
    const e=ext(it.name), thumbable=IMG.has(e)||VID.has(e)||e==='pdf';
    const visual = thumbable
      ? `<img class="gthumb" src="/thumb?id=${encodeURIComponent(it.id)}" loading="lazy" onerror="this.style.display='none';this.nextElementSibling.style.display=''"><i class="${faicon(it)} fi ${cls(it)}" style="display:none"></i>`
      : `<i class="${faicon(it)} fi ${cls(it)}"></i>`;
    d.innerHTML=`<input type="checkbox" class="gchk" ${sel[it.id]?'checked':''} onchange="toggle(this,'${it.id}')" onclick="event.stopPropagation()">
      ${visual}
      <div class="gn" title="${esc(it.name)}">${esc(it.name)}</div>
      <div class="gs">${it.folder?'Folder':fmtSize(it.size)}</div>`;
    d.onclick=e2=>{ if(e2.target.type==='checkbox')return; it.folder?drill(it.id,it.name):openItem(it); };
    d.oncontextmenu=e2=>{ e2.preventDefault(); showCtx(e2,it); };
    if(!it.folder){ d.onmouseenter=()=>prefetch(it.id); d.onmouseleave=prefetchCancel; }
    g.appendChild(d);
  }
}
function faicon(it){ if(it.folder)return 'fa-solid fa-folder'; const e=ext(it.name);
  if(IMG.has(e))return 'fa-solid fa-image'; if(VID.has(e))return 'fa-solid fa-film'; if(AUD.has(e))return 'fa-solid fa-music';
  if(e==='pdf')return 'fa-solid fa-file-pdf'; if(['doc','docx'].includes(e))return 'fa-solid fa-file-word';
  if(['xls','xlsx'].includes(e))return 'fa-solid fa-file-excel'; if(['ppt','pptx'].includes(e))return 'fa-solid fa-file-powerpoint';
  if(['zip','rar','7z','tar','gz'].includes(e))return 'fa-solid fa-file-zipper'; if(TXT.has(e))return 'fa-solid fa-file-code';
  return 'fa-solid fa-file'; }
function cls(it){ if(it.folder)return 'folder'; const e=ext(it.name);
  if(IMG.has(e))return 'image'; if(VID.has(e))return 'video'; if(AUD.has(e))return 'audio';
  if(e==='pdf')return 'pdf'; if(['doc','docx','ppt','pptx'].includes(e))return 'doc'; if(['xls','xlsx'].includes(e))return 'sheet';
  if(TXT.has(e))return 'code'; if(['zip','rar','7z','tar','gz'].includes(e))return 'archive'; return 'def'; }

/* ── Selection ── */
function toggle(cb,id){ cb.checked?sel[id]=items.find(i=>i.id===id):delete sel[id]; render(); selStatus(); }
function selAll(m){ if(m.checked)items.forEach(i=>sel[i.id]=i); else sel={}; render(); selStatus(); }
function selStatus(){ const n=Object.keys(sel).length; $('stSel').textContent=n?n+' selected':''; }

/* ── Download: 302 straight to Microsoft CDN (raw stream, no host buffering) ── */
async function download(id,name){ const url=await getLink(id); const a=document.createElement('a'); a.href=url||('/stream?id='+encodeURIComponent(id)+'&dl=1&name='+encodeURIComponent(name)); a.download=name; document.body.appendChild(a); a.click(); a.remove(); }
function dlSelected(){ const ids=Object.keys(sel); if(!ids.length)return toast('Select files first'); ids.forEach(id=>{const it=sel[id]; if(it&&!it.folder)download(id,it.name);}); }

/* ── Preview / open ── */
// Fresh Microsoft-CDN url per item, cached ~50min (token lives ~59min). Zero host egress.
const linkCache={};
async function getLink(id,force){
  const c=linkCache[id];
  if(!force && c && Date.now()-c.t < 50*60*1000) return c.url;
  const d=await fetch('/link?id='+encodeURIComponent(id)).then(r=>r.json()).catch(()=>({}));
  if(d.url){ linkCache[id]={url:d.url,t:Date.now()}; return d.url; }
  return null;
}
// Prefetch the CDN link on hover (debounced) so clicking a file opens its preview instantly.
let pfTimer=null;
function prefetch(id){ clearTimeout(pfTimer); pfTimer=setTimeout(()=>getLink(id).catch(()=>{}),120); }
function prefetchCancel(){ clearTimeout(pfTimer); }
// Self-healing: if a signed url lapses mid-playback, re-fetch and resume at the same spot.
// 8s cooldown stops truly-unsupported codecs from looping forever.
function attachRelink(el,id){
  let last=0, lastTry=0;
  el.addEventListener('timeupdate',()=>{ if(el.currentTime)last=el.currentTime; });
  el.addEventListener('error',async()=>{
    if(Date.now()-lastTry<8000) return; lastTry=Date.now();
    const wasPlaying=!el.paused, url=await getLink(id,true); if(!url)return;
    el.src=url; el.load();
    el.addEventListener('loadedmetadata',()=>{ try{el.currentTime=last;}catch(e){} if(wasPlaying)el.play(); },{once:true});
  });
}

async function openItem(it){
  pvItem=it; const e=ext(it.name);
  $('pvicon').innerHTML=`<i class="${faicon(it)} ${cls(it)}"></i>`; $('pvtitle').textContent=it.name;
  $('pvmeta').textContent=it.size?fmtSize(it.size):''; $('pvSave').style.display='none'; $('pvRun').style.display='none';
  const body=$('pvbody'); body.innerHTML='<div class="pvload"><span class="spin"></span> Loading…</div>';
  $('pv').classList.add('open');
  if(TXT.has(e)){ editor(body,it,e); return; }
  const link=await getLink(it.id);          // direct MS CDN url (CORS *, range, ~59min)
  if(pvItem!==it) return;                    // user navigated away while fetching
  if(!link){ body.innerHTML='<div class="pvload">Could not get a stream link.</div>'; return; }
  if(IMG.has(e)){ body.innerHTML=`<img src="${link}">`; }
  else if(e==='pdf'){ body.innerHTML=`<iframe src="${link}"></iframe>`; }
  else if(VID.has(e)){ videoPlayer(body,link,it); }
  else if(AUD.has(e)){ audioPlayer(body,link,it); }
  else { body.innerHTML=`<div class="pvload" style="flex-direction:column;gap:16px"><i class="${faicon(it)}" style="font-size:56px;opacity:.3"></i><p>No inline preview for .${e}</p><button class="tb pri" onclick="pvDownload()"><i class="fa-solid fa-download"></i> Download</button></div>`; }
}
function closePv(){ $('pv').classList.remove('open'); document.querySelectorAll('#pvbody audio,#pvbody video').forEach(m=>{try{m.pause();m.src='';}catch(e){}}); if(window._aud){try{window._aud.pause();}catch(e){} window._aud=null;} if(window._viz){cancelAnimationFrame(window._viz);window._viz=null;} $('pvbody').innerHTML=''; pvItem=null; aceEd=null; }
function pvDownload(){ if(pvItem)download(pvItem.id,pvItem.name); }
$('pv').onclick=e=>{ if(e.target===$('pv'))closePv(); };

/* ── Ace editor ── */
let aceEd=null;
async function editor(body,it,e){
  body.innerHTML='<div id="ace"></div>';
  $('pvSave').style.display=''; if(HTMLX.has(e))$('pvRun').style.display='';
  let txt='';
  try{ const lk=await getLink(it.id); txt=await fetch(lk).then(r=>{if(!r.ok)throw 0;return r.text();}); }  // direct CDN (CORS *)
  catch(e){ txt=await fetch('/text?id='+encodeURIComponent(it.id)).then(r=>r.text()).catch(()=>''); }
  aceEd=ace.edit('ace');
  aceEd.setTheme('ace/theme/tomorrow_night');
  const ml=ace.require('ace/ext/modelist');
  aceEd.session.setMode(ml.getModeForPath(it.name).mode);
  aceEd.setValue(txt,-1);
  aceEd.setOptions({fontSize:'13px',showPrintMargin:false,wrap:true,tabSize:2});
}
async function saveEdit(){
  if(!aceEd||!pvItem)return; const btn=$('pvSave'); btn.innerHTML='<span class="spin"></span> Saving…';
  const r=await fetch('/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:pvItem.id,content:aceEd.getValue()})}).then(r=>r.json());
  btn.innerHTML=r.ok?'<i class="fa-solid fa-check"></i> Saved':'<i class="fa-solid fa-xmark"></i> Failed';
  setTimeout(()=>btn.innerHTML='<i class="fa-solid fa-floppy-disk"></i> Save',1600); refresh();
}
function runHtml(){
  if(!aceEd)return; const w=window.open('','_blank'); w.document.open(); w.document.write(aceEd.getValue()); w.document.close();
}

/* ── Custom video player ── */
function videoPlayer(body,src,it){
  body.innerHTML=`<div class="vp"><div class="vp-shell paused" id="vpShell">
    <video id="vpv" src="${src}" playsinline></video>
    <div class="vp-big" id="vpBig"><i class="fa-solid fa-play"></i></div>
    <div class="vp-ctrl">
      <div class="vp-bar" id="vpBar"><div class="vp-buf" id="vpBuf"></div><div class="vp-fill" id="vpFill"></div></div>
      <div class="vp-row">
        <button id="vpPlay"><i class="fa-solid fa-play"></i></button>
        <button id="vpBack"><i class="fa-solid fa-rotate-left"></i></button>
        <button id="vpFwd"><i class="fa-solid fa-rotate-right"></i></button>
        <span class="vp-time" id="vpTime">0:00 / 0:00</span>
        <span class="vp-spacer"></span>
        <button id="vpMute"><i class="fa-solid fa-volume-high"></i></button>
        <input type="range" class="vp-vol" id="vpVol" min="0" max="1" step="0.05" value="1">
        <button id="vpFs"><i class="fa-solid fa-expand"></i></button>
      </div>
    </div></div></div>`;
  const v=$('vpv'),shell=$('vpShell'),bar=$('vpBar'),fill=$('vpFill'),buf=$('vpBuf'),big=$('vpBig');
  attachRelink(v,it.id);   // resume-in-place if the CDN token lapses mid-play
  const pIco=()=>$('vpPlay').querySelector('i');
  const flash=k=>{ big.querySelector('i').className='fa-solid fa-'+k; big.classList.remove('show'); void big.offsetWidth; big.classList.add('show'); };
  const play=()=>{ v.paused?v.play():v.pause(); };
  $('vpPlay').onclick=play; $('vpv').onclick=play;
  v.onplay=()=>{ pIco().className='fa-solid fa-pause'; shell.classList.remove('paused'); flash('play'); };
  v.onpause=()=>{ pIco().className='fa-solid fa-play'; shell.classList.add('paused'); flash('pause'); };
  $('vpBack').onclick=()=>v.currentTime-=10; $('vpFwd').onclick=()=>v.currentTime+=10;
  v.ontimeupdate=()=>{ fill.style.width=(v.currentTime/v.duration*100||0)+'%'; $('vpTime').textContent=ft(v.currentTime)+' / '+ft(v.duration); };
  v.onprogress=()=>{ if(v.buffered.length)buf.style.width=(v.buffered.end(v.buffered.length-1)/v.duration*100||0)+'%'; };
  bar.onclick=e=>{ const r=bar.getBoundingClientRect(); v.currentTime=(e.clientX-r.left)/r.width*v.duration; };
  $('vpMute').onclick=()=>{ v.muted=!v.muted; $('vpMute').querySelector('i').className='fa-solid fa-volume-'+(v.muted?'xmark':'high'); };
  $('vpVol').oninput=e=>{ v.volume=e.target.value; v.muted=false; };
  $('vpFs').onclick=()=>{ shell.requestFullscreen?shell.requestFullscreen():shell.webkitRequestFullscreen&&shell.webkitRequestFullscreen(); };
}
function ft(s){ if(!s||isNaN(s))return '0:00'; s=Math.floor(s); const h=Math.floor(s/3600),m=Math.floor(s%3600/60),x=s%60; return (h?h+':'+String(m).padStart(2,'0'):m)+':'+String(x).padStart(2,'0'); }

/* ── Custom glowing audio player w/ visualizer ── */
function audioPlayer(body,src,it){
  const bars=Array.from({length:40},()=>'<span></span>').join('');
  body.innerHTML=`<div class="ap"><div class="ap-card" id="apCard"><div class="ap-inner">
    <div class="ap-disc"><i class="fa-solid fa-music"></i></div>
    <div class="ap-name">${esc(it.name)}</div>
    <div class="ap-viz" id="apViz">${bars}</div>
    <div class="ap-bar" id="apBar"><div class="ap-fill" id="apFill"></div></div>
    <div class="ap-time"><span id="apCur">0:00</span><span id="apDur">0:00</span></div>
    <div class="ap-row">
      <button class="mini" id="apBack"><i class="fa-solid fa-backward"></i></button>
      <button id="apPlay"><i class="fa-solid fa-play"></i></button>
      <button class="mini" id="apFwd"><i class="fa-solid fa-forward"></i></button>
    </div></div></div></div>`;
  const a=new Audio(); a.crossOrigin='anonymous'; a.src=src; window._aud=a;  // crossOrigin BEFORE src so FFT works off the CDN
  attachRelink(a,it.id);
  const card=$('apCard'),fill=$('apFill'),bar=$('apBar'),viz=$('apViz').children;
  const pIco=()=>$('apPlay').querySelector('i');
  $('apPlay').onclick=()=>{ a.paused?a.play():a.pause(); };
  $('apBack').onclick=()=>a.currentTime-=10; $('apFwd').onclick=()=>a.currentTime+=10;
  a.onplay=()=>{ pIco().className='fa-solid fa-pause'; card.classList.add('playing'); startViz(); };
  a.onpause=()=>{ pIco().className='fa-solid fa-play'; card.classList.remove('playing'); };
  a.ontimeupdate=()=>{ fill.style.width=(a.currentTime/a.duration*100||0)+'%'; $('apCur').textContent=ft(a.currentTime); };
  a.onloadedmetadata=()=>$('apDur').textContent=ft(a.duration);
  bar.onclick=e=>{ const r=bar.getBoundingClientRect(); a.currentTime=(e.clientX-r.left)/r.width*a.duration; };
  // Real FFT visualizer (same-origin /stream => CORS-safe); fallback to animated bars.
  let analyser=null,data=null;
  function startViz(){
    if(!analyser){ try{
      const ctx=new (window.AudioContext||window.webkitAudioContext)();
      const srcNode=ctx.createMediaElementSource(a); analyser=ctx.createAnalyser(); analyser.fftSize=128;
      srcNode.connect(analyser); analyser.connect(ctx.destination); data=new Uint8Array(analyser.frequencyBinCount);
      ctx.resume();
    }catch(e){ analyser='fake'; } }
    loopViz();
  }
  function loopViz(){
    if(a.paused){ if(window._viz){cancelAnimationFrame(window._viz);window._viz=null;} return; }
    if(analyser&&analyser!=='fake'){ analyser.getByteFrequencyData(data);
      for(let i=0;i<viz.length;i++){ const v=data[i*2]||0; viz[i].style.height=(6+v/255*40)+'px'; } }
    else { for(let i=0;i<viz.length;i++){ viz[i].style.height=(6+Math.random()*38*(0.4+0.6*Math.sin(Date.now()/200+i)))+'px'; } }
    window._viz=requestAnimationFrame(loopViz);
  }
}

/* ── Upload: browser -> Microsoft direct, proxy fallback ── */
// OneDrive resumable sessions REJECT parallel/out-of-order fragments (tested: 63/64 fail),
// so multi-stream S3-style upload is impossible. Direct browser->MS is HTTP/2 flow-limited
// (~13 MB/s); 10 MiB is the sweet spot. Turbo streams through the host (~44 MB/s, HTTP/1.1)
// at the cost of server egress. XHR gives smooth sub-chunk progress (no more 6s dead start).
let TURBO=false;
const CHUNK_DIRECT=10*1024*1024;   // 10 MiB (=32×320KiB)
const CHUNK_TURBO =20*1024*1024;   // 20 MiB

function toggleTurbo(){ TURBO=!TURBO; localStorage.setItem('od_turbo',TURBO?'1':'0'); $('turboBtn').classList.toggle('pri',TURBO); }

function putChunk(url, blob, range, proxy, onprog){
  return new Promise(resolve=>{
    const xhr=new XMLHttpRequest();
    xhr.open('PUT', proxy ? '/upload/proxy?url='+encodeURIComponent(url) : url);
    xhr.setRequestHeader('Content-Range', range);
    xhr.upload.onprogress=e=>{ if(e.lengthComputable&&onprog) onprog(e.loaded); };
    xhr.onload=()=>resolve(xhr.status);
    xhr.onerror=()=>resolve(0);
    xhr.send(blob);
  });
}
async function upload(files){
  if(!files||!files.length)return;
  const parent=stack[stack.length-1].id;
  $('utoast').style.display='block'; $('utName').textContent='Preparing…'; $('utFill').style.width='0%'; $('utRate').textContent='';
  for(let i=0;i<files.length;i++){ await uploadOne(files[i],parent,i+1,files.length); }
  $('utoast').style.display='none'; refresh(); loadStorage();
}
async function uploadOne(file,parent,idx,total){
  $('utName').textContent=file.name; $('utCount').textContent=idx+' / '+total;
  $('utFill').style.width='0%'; $('utRate').textContent='preparing…';
  const s=await fetch('/upload/session',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({parent_id:parent,name:file.name})}).then(r=>r.json());
  if(!s.ok){ toast('Upload failed: session error'); return; }
  const url=s.uploadUrl, size=file.size;
  let useProxy=TURBO, CHUNK=useProxy?CHUNK_TURBO:CHUNK_DIRECT, offset=0, t0=Date.now();
  while(offset<size){
    const end=Math.min(offset+CHUNK,size), blob=file.slice(offset,end), base=offset;
    const range=`bytes ${offset}-${end-1}/${size}`;
    const prog=loaded=>{ const cur=base+loaded, sec=(Date.now()-t0)/1000||0.001;
      $('utFill').style.width=(cur/size*100)+'%';
      const eta=(size-cur)/((cur/sec)||1);
      $('utRate').textContent=(cur/1048576/sec).toFixed(1)+' MB/s '+(useProxy?'⚡ turbo':'· direct')+(cur<size?'  ·  '+fmtT(eta)+' left':''); };
    let st=await putChunk(url,blob,range,useProxy,prog);
    if(![200,201,202].includes(st) && !useProxy){ useProxy=true; st=await putChunk(url,blob,range,true,prog); } // direct blocked -> proxy
    if(![200,201,202].includes(st)){ toast('Upload failed at '+fmtSize(offset)+' (HTTP '+st+')'); return; }
    offset=end; $('utFill').style.width=(offset/size*100)+'%';
  }
  const sec=(Date.now()-t0)/1000||1; $('utRate').textContent='done · '+(size/1048576/sec).toFixed(1)+' MB/s avg';
}
function fmtT(s){ if(!isFinite(s)||s<0)return '—'; s=Math.round(s); return s<60?s+'s':Math.floor(s/60)+'m '+(s%60)+'s'; }

/* ── Folder / new file / rename / delete ── */
async function mkdir(){ const n=prompt('Folder name:'); if(!n)return; await fetch('/mkdir',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({parent_id:stack[stack.length-1].id,name:n})}); refresh(); }
async function newTextFile(){ const n=prompt('File name (e.g. notes.txt):'); if(!n)return; await fetch('/newfile',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({parent_id:stack[stack.length-1].id,name:n,content:''})}); refresh(); }
function openRename(it){ renTarget=it; $('renInput').value=it.name; $('renM').classList.add('open'); setTimeout(()=>{$('renInput').focus();$('renInput').select();},40); }
function renameSel(){ const ids=Object.keys(sel); if(ids.length!==1)return toast('Select exactly one item'); openRename(sel[ids[0]]); }
async function doRename(){ if(!renTarget)return; const n=$('renInput').value.trim(); if(!n||n===renTarget.name){closeMask('renM');return;} await fetch('/rename',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:renTarget.id,name:n})}); closeMask('renM'); refresh(); }
function closeMask(id){ $(id).classList.remove('open'); }
$('renInput').addEventListener('keydown',e=>{ if(e.key==='Enter')doRename(); if(e.key==='Escape')closeMask('renM'); });
async function delOne(id){ if(!confirm('Delete this item? This cannot be undone.'))return; await fetch('/delete?id='+encodeURIComponent(id),{method:'DELETE'}); refresh(); loadStorage(); }
async function delSelected(){ const ids=Object.keys(sel); if(!ids.length)return toast('Select files first'); if(!confirm(`Delete ${ids.length} item(s)?`))return; setStatus('<span class="spin"></span> Deleting…',true); for(const id of ids)await fetch('/delete?id='+encodeURIComponent(id),{method:'DELETE'}); sel={}; refresh(); loadStorage(); }

/* ── Context menu ── */
function showCtx(e,it){ ctxItem=it; sel={[it.id]:it}; render(); selStatus(); const m=$('ctx'); m.style.display='block';
  m.style.left=Math.min(e.clientX,innerWidth-196)+'px'; m.style.top=Math.min(e.clientY,innerHeight-260)+'px'; }
function hideCtx(){ $('ctx').style.display='none'; }
function ctxOpen(){ if(ctxItem&&!ctxItem.folder)openItem(ctxItem); else if(ctxItem)drill(ctxItem.id,ctxItem.name); hideCtx(); }
function ctxDl(){ if(ctxItem&&!ctxItem.folder)download(ctxItem.id,ctxItem.name); hideCtx(); }
function ctxRen(){ if(ctxItem)openRename(ctxItem); hideCtx(); }
function ctxCopy(){ if(ctxItem)navigator.clipboard.writeText(ctxItem.name); hideCtx(); }
function ctxRaw(){ if(ctxItem&&!ctxItem.folder)copyRaw(ctxItem.id); hideCtx(); }
// Raw inline link: opens in the browser instead of downloading, re-signs server-side so it never expires.
function copyRaw(id){ if(!id)return; const u=location.origin+'/raw?id='+encodeURIComponent(id);
  navigator.clipboard.writeText(u).then(()=>toast('Raw link copied — opens inline, never expires')).catch(()=>toast(u)); }
function ctxDel(){ if(ctxItem)delOne(ctxItem.id); hideCtx(); }
document.addEventListener('click',hideCtx);
document.addEventListener('keydown',e=>{ if(e.key==='Escape'){closePv();closeMask('renM');hideCtx();} });

/* ── Drag & drop upload ── */
const area=$('area');
['dragenter','dragover'].forEach(ev=>area.addEventListener(ev,e=>{e.preventDefault();area.classList.add('drag');}));
['dragleave','drop'].forEach(ev=>area.addEventListener(ev,e=>{e.preventDefault();if(ev==='drop'||e.target===area)area.classList.remove('drag');}));
area.addEventListener('drop',e=>{ area.classList.remove('drag'); if(e.dataTransfer.files.length)upload(e.dataTransfer.files); });

/* ── Helpers ── */
function fmtSize(b){ if(!b)return '0 B'; const u=['B','KB','MB','GB','TB']; let i=0; while(b>=1024&&i<4){b/=1024;i++;} return b.toFixed(b<10&&i?1:0)+' '+u[i]; }
function fmtDate(s){ if(!s)return ''; return new Date(s).toLocaleDateString(undefined,{year:'numeric',month:'short',day:'numeric'}); }
function esc(s){ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }
function jj(o){ return "'"+JSON.stringify(o).replace(/'/g,'&#39;').replace(/"/g,'&quot;')+"'"; }
let _t; function toast(m){ setStatus(m); clearTimeout(_t); _t=setTimeout(()=>setStatus('Ready'),2500); }
// unwrap the jj() escaping when called
const _openItem=openItem, _openRename=openRename;
openItem=o=>{ if(typeof o==='string')o=JSON.parse(o.replace(/&#39;/g,"'").replace(/&quot;/g,'"')); _openItem(o); };
openRename=o=>{ if(typeof o==='string')o=JSON.parse(o.replace(/&#39;/g,"'").replace(/&quot;/g,'"')); _openRename(o); };

/* boot */
poll();
</script>
</body>
</html>"""


_booted = False


def boot():
    """Load persisted tokens + start the refresher. Runs on import (for gunicorn/Wasmer) and main()."""
    global _booted
    if _booted:
        return
    _booted = True
    env = load_env()
    if env.get("REFRESH_TOKEN"):
        TOKENS["refresh_token"] = env["REFRESH_TOKEN"]
        TOKENS["access_token"] = env.get("ACCESS_TOKEN") or None
        TOKENS["expires_at"] = float(env.get("EXPIRES_AT") or 0)
        TOKENS["email"] = env.get("EMAIL") or None
    # Background refresher is belt-and-suspenders; valid_token() also refreshes lazily per
    # request, so this is safe to skip on runtimes with limited threading (e.g. WASIX).
    try:
        threading.Thread(target=refresher_loop, daemon=True).start()
    except Exception as e:
        print(f"[boot] background refresher unavailable ({e}); using lazy refresh")


boot()  # so `gunicorn app:app` / Wasmer WSGI pick up saved tokens without running main()


def main():
    if TOKENS.get("refresh_token"):
        print("[boot] found saved token, refreshing…")
        refresh_now()
    print("\n" + "=" * 56)
    print(f"  OneDrive API  ->  http://localhost:{PORT}   (turbo={'on' if TURBO else 'off'})")
    print("=" * 56 + "\n")
    from werkzeug.serving import run_simple
    run_simple("0.0.0.0", PORT, app, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
