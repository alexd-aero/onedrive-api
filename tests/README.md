# Offline test harness

These tests run the whole app against a **mock Microsoft Graph**, so nothing here touches a
real OneDrive or needs credentials.

```
tests/mock_graph.py   a fake Graph + CDN: quota, listings w/ pagination, item CRUD, and real
                      resumable upload sessions — including the behaviours that caused the
                      bugs (burst throttling with 429 + Retry-After, sequential-fragment
                      enforcement, and the 0-byte placeholder an open session leaves behind)
tests/harness.py      boots app.py against the mock with a pre-configured account, and can
                      also front it with a deliberately hostile reverse proxy (TLS-stripping,
                      mounted under a /od path prefix)
tests/restart.sh      (re)start the harness
```

## Run

```bash
./tests/restart.sh --port 3300 --mock 5999 --proxy 3400

python  tests/test_server.py      # server contract: throttling, uploads, batch delete, proxy headers
node    tests/test_ui.mjs         # browser: gate under a proxy, selection, delete, 25-file upload
node    tests/test_extra.mjs      # grid view, insecure-context login, gate error reporting
node    tests/check_js.mjs        # parse every inline <script> in app.py
python  test_suite.py http://127.0.0.1:3300 --user tester --pass hunter2pass
```

The browser tests need `puppeteer-core` plus a system Chromium:

```bash
cd tests && npm install
CHROME_PATH=/usr/bin/chromium-browser node test_ui.mjs   # defaults to this path
```

## Benchmark

`tests/bench_turbo.py` measures the host-proxied (turbo) upload path and samples the app
process's peak RSS, which is what exposed the store-and-forward buffering:

```bash
# run the mock separately so RSS is the app alone
python tests/mock_graph.py 5999 &
./tests/restart.sh --port 3300 --mock-url http://127.0.0.1:5999 --turbo
python tests/bench_turbo.py --mb 64 --chunk 32
```
