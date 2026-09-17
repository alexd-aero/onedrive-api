# OneDrive API

A single-file Flask app that turns a personal (or permissive work) OneDrive into a fast,
glassmorphism file explorer — with **in-app device-code sign-in**, **zero-egress streaming
straight from Microsoft's CDN**, an Ace code editor, and custom media players.

![status](https://img.shields.io/badge/single%20file-app.py-7c6cff) ![python](https://img.shields.io/badge/python-3.12-3ee08f)

## Features

- **Self-hosted auth gate** — a setup screen locks the whole instance behind a username + password
  (Basic, or **AES-256-GCM Secure** mode that encrypts the token at rest with your
  password). All crypto runs in the browser (Web Crypto); every API route is session-gated.
- **Multiple accounts** — connect several Microsoft accounts and switch between them from the
  sidebar (Add account / switch / remove). Switching is instant and needs no redeploy.
- **In-app Microsoft auth** — device-code flow right in the UI (code + link + live yellow→green status).
  No app registration; uses Microsoft's own *Graph Command Line Tools* public client, which is
  pre-authorized for `Files.ReadWrite.All`. Tokens persist to `.env` and refresh every 10 min.
- **Raw streaming, ~0 host bandwidth** — video, audio, images, PDF and the editor's text load
  stream *directly* from `*.microsoftpersonalcontent.com` (CORS-enabled, HTTP-range/seekable).
  A 512 MB movie costs the host ~1 KB of metadata, not 512 MB — ideal for Wasmer's bandwidth cap.
  Signed URLs live ~59 min; a self-healing handler re-links and resumes in place if one lapses.
- **Uploads** — browser→Microsoft direct (resumable session, ~0 host egress). Files under 8 MiB
  go up in a **single request** (no upload session), which is what keeps a 25-file drop from
  tripping Graph's burst throttle. Every request retries with backoff on 429/5xx (honouring
  `Retry-After`), and a file that still fails has its session cancelled so it can't leave a
  0-byte stub behind. Optional **⚡ Turbo** routes through the host for local/unlimited deploys;
  the host *streams* fragments through rather than buffering them, so host memory stays flat
  regardless of fragment size.
  *OneDrive rejects parallel fragments, so S3-style multi-stream within one file is impossible —
  this is measured.*
- **Editor** — dark Ace editor for text/code with save; full-permission HTML preview.
- **Players** — custom video player (buffer bar, seek, skip, fullscreen) and a glowing, animated
  audio player with a real WebAudio FFT visualizer (works off the cross-origin CDN stream).
- Grid thumbnails, hover-prefetch (instant preview), drag-and-drop upload, sort/filter, context menu.
- **Instant, optimistic UI** — selecting a row animates that row in place instead of rebuilding
  the list; delete/rename/create apply immediately and reconcile with Graph in the background
  (Graph's listings are eventually consistent, so re-listing straight after a write used to show
  the *old* state and look like nothing had happened).
- **Reverse-proxy ready** — honours `X-Forwarded-Proto/Host/Prefix` and resolves its own base
  path, so it works mounted under a prefix (`https://host/od`). The sign-in screen reports what
  went wrong instead of rendering a blank card, and falls back to a pure-JS PBKDF2 when served
  over plain http (where browsers don't expose `crypto.subtle`).

## Setup model (GitOps)

Config lives in **two files in the repo root**: `creds.yml` (your login) and `token.yml` (the
Microsoft token). They ship as placeholders. The in-app setup screen generates a code for each; you
paste it into the file and commit. This is deliberate — hosts like Wasmer have an ephemeral
filesystem, so config can't be written at runtime; committing it to your **private** fork is the
durable store. (Running locally, the files are also written for you automatically.)

## Deploy to Wasmer

1. **Fork this repo**, then `git clone` your fork.
2. On GitHub: **Settings → General → Danger Zone → "Leave fork network"** (unlink from the upstream
   fork network). **Wait for it to finish.**
3. Then, still in **Danger Zone**, set the repo to **Private**. *(Do this before adding any tokens —
   `creds.yml`/`token.yml` will hold secrets.)*
4. Go to **wasmer.app → Deploy → connect this GitHub repo**. Let Wasmer **auto-detect the
   environment**. The start command is `python app.py` (from the `Procfile`) — **not gunicorn**,
   which can't run on Wasmer's WASIX Python (no `AF_UNIX`). If Wasmer asks for a run/start command,
   use `python app.py`. Deploy.
5. Open your app URL. `/` shows the **setup screen**:
   - **Step 1 — Credentials.** Pick **Basic** or **Secure** (AES-256-GCM), set a username + password.
     It gives you a code (auto-copied). In your fork, open **`creds.yml`** in the root, **replace the
     whole file** with the code, commit → Wasmer redeploys.
   - **Log in** with those credentials.
   - **Step 2 — Microsoft.** Click connect, enter the device code at the link, approve. It gives you a
     second code (auto-copied). Replace the whole of **`token.yml`** with it, commit → redeploy.
6. Done. Log in and use it. The Microsoft token is **effectively permanent** — it self-refreshes and
   rides a ~90-day sliding window, so it only needs re-connecting if you change your Microsoft
   password, revoke access, or leave it unused for 90+ days.

**Basic vs Secure:** *Basic* — password gates the UI; the app runs headless after each deploy.
*Secure* — the Microsoft token in `token.yml` is AES-256-GCM encrypted with your password (all crypto
runs in your browser via Web Crypto), so the repo alone can't unlock OneDrive; the tradeoff is you log
in once after each cold-start to decrypt it.

## Run locally

```bash
pip install -r requirements.txt
python app.py                  # http://localhost:3000, then do the setup screen (files written for you)
TURBO_UPLOAD=1 python app.py   # faster host-proxied uploads (uses host bandwidth; fine locally)
```

**Docker / any PaaS** (Render, Railway, Fly, …): `docker build -t onedrive-api . && docker run -p 8080:8080 onedrive-api`
— runs `gunicorn -w 1 --threads 8 app:app` (single worker: token + device-code poller live in one
process; scale with threads, not workers).

## Test

Offline tests run the whole app against a mock Microsoft Graph — no credentials, no real
OneDrive. See **[`tests/README.md`](tests/README.md)**:

```bash
./tests/restart.sh --port 3300 --mock 5999 --proxy 3400
python tests/test_server.py     # throttling, uploads, batch delete, proxy headers
node   tests/test_ui.mjs        # real Chromium: gate behind a proxy, selection, delete, 25-file upload
node   tests/test_extra.mjs     # grid view, insecure-context login, gate error reporting
```


```bash
python test_suite.py --user alex --pass yourpass            # smoke + stress (24 checks)
python test_suite.py --user alex --pass yourpass --upload=512   # add a 512 MiB upload benchmark
python test_suite.py https://your.app --user alex --pass yourpass
```
Covers auth gating (unauth blocked, wrong-password rejected), login/session, latency, CRUD,
range streaming, `/raw` + `/view`, and 25-way concurrency.

## Hosting on a shared platform (Wasmer etc.)

Public shared hosts run automated abuse detection. An OAuth **device-code** login page carrying
**cloud-provider branding** looks like a phishing kit to those scanners, so a public deploy can get
auto-disabled even though it's legitimate (it only ever sends you to the provider's *real* sign-in
page and never sees your password). This UI is intentionally **de-branded** (generic name/icon, no
provider logos) to minimise that signal — but a public device-code page can still be flagged. For a
guaranteed-stable deploy, **self-host** (e.g. a home server / Pi) and reach it privately.

## Notes / limits

- Works on personal OneDrive and work/school tenants that permit device-code flow + user consent.
  Locked-down tenants (Conditional Access blocking device code, or disabled user consent) will refuse
  sign-in — that's a tenant policy, not a bug.
- Single-stream browser upload tops out around ~13 MB/s (OneDrive HTTP/2 flow control); Turbo ~2×.
- Microsoft's download token is **attachment-only** by design — there is no URL tweak for an inline
  "raw" view (the token signs the query string; edits return 401). The **Raw link** button copies a
  `/view?id=…` URL: a ~3 KB HTML shell that renders the file via a media tag / `fetch`→blob pointing
  **straight at the CDN**, so the bytes go browser↔CDN direct — **zero host egress**, renders inline
  instead of downloading, and never expires (re-signs each load). (`/raw?id=…` still exists as a
  byte-level inline proxy for when you need a direct-bytes URL, e.g. embedding in someone else's `<img>`.)
