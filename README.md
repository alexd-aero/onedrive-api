# OneDrive API

A single-file Flask app that turns a personal (or permissive work) OneDrive into a fast,
glassmorphism file explorer — with **in-app device-code sign-in**, **zero-egress streaming
straight from Microsoft's CDN**, an Ace code editor, and custom media players.

![status](https://img.shields.io/badge/single%20file-app.py-7c6cff) ![python](https://img.shields.io/badge/python-3.12-3ee08f)

## Features

- **In-app auth** — device-code flow right in the UI (code + link + live yellow→green status).
  No app registration; uses Microsoft's own *Graph Command Line Tools* public client, which is
  pre-authorized for `Files.ReadWrite.All`. Tokens persist to `.env` and refresh every 10 min.
- **Raw streaming, ~0 host bandwidth** — video, audio, images, PDF and the editor's text load
  stream *directly* from `*.microsoftpersonalcontent.com` (CORS-enabled, HTTP-range/seekable).
  A 512 MB movie costs the host ~1 KB of metadata, not 512 MB — ideal for Wasmer's bandwidth cap.
  Signed URLs live ~59 min; a self-healing handler re-links and resumes in place if one lapses.
- **Uploads** — browser→Microsoft direct (resumable session, ~0 host egress). Optional **⚡ Turbo**
  routes through the host (~2× faster, uses host bandwidth) for local/unlimited deploys.
  *OneDrive rejects parallel fragments, so S3-style multi-stream is impossible — this is measured.*
- **Editor** — dark Ace editor for text/code with save; full-permission HTML preview.
- **Players** — custom video player (buffer bar, seek, skip, fullscreen) and a glowing, animated
  audio player with a real WebAudio FFT visualizer (works off the cross-origin CDN stream).
- Grid thumbnails, hover-prefetch (instant preview), drag-and-drop upload, sort/filter, context menu.

## Run locally

```bash
pip install -r requirements.txt
python app.py           # http://localhost:3000
TURBO_UPLOAD=1 python app.py   # enable faster host-proxied uploads (uses host bandwidth)
```

Open the URL, click **Sign in with Microsoft**, enter the code at the link shown. Done.

## Deploy

**Wasmer Edge** (keep Turbo OFF so streaming/uploads don't touch the bandwidth cap):
```bash
wasmer deploy
```

**Docker / any PaaS** (Render, Railway, Fly, …):
```bash
docker build -t onedrive-api . && docker run -p 8080:8080 onedrive-api
```
Runs `gunicorn -w 1 --threads 8 app:app` (single worker: the token + device-code poller live in
one process; scale with threads, not workers).

> First sign-in on a fresh deploy happens in the browser. To pre-seed a deployment, copy a working
> `.env` (contains `REFRESH_TOKEN`) into the container — it is **git-ignored** and must never be committed.

## Test

```bash
python test_suite.py                  # smoke + stress against localhost (must be signed in)
python test_suite.py --upload=512     # add a 512 MiB upload benchmark
python test_suite.py https://your.app # against a deployed instance
```

## Notes / limits

- Works on personal OneDrive and work/school tenants that permit device-code flow + user consent.
  Locked-down tenants (Conditional Access blocking device code, or disabled user consent) will refuse
  sign-in — that's a tenant policy, not a bug.
- Single-stream browser upload tops out around ~13 MB/s (OneDrive HTTP/2 flow control); Turbo ~2×.
- Microsoft's download token is **attachment-only** by design — there is no URL tweak for an inline
  "raw" view (the token signs the query string; edits return 401). Inline URLs exist only via the
  thumbnail endpoint (`/thumb`), used here for grid previews.
