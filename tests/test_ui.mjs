/*
 * Browser regression tests for the six reported bugs, driven against tests/harness.py.
 * Start the harness first:  ./tests/restart.sh --port 3300 --mock 5999 --proxy 3400
 */
import { launch, newPage, login, uploadFiles, waitUploadDone, ok } from './browser.mjs';

const APP   = process.env.APP   || 'http://127.0.0.1:3300';
const PROXY = process.env.PROXY || 'http://127.0.0.1:3400/od';
const MOCK  = process.env.MOCK  || 'http://127.0.0.1:5999';

const post = (u, b) => fetch(u, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                  body: JSON.stringify(b || {}) }).then(r => r.json());
const items = () => fetch(MOCK + '/__items').then(r => r.json());
const sleep = ms => new Promise(r => setTimeout(r, ms));

const browser = await launch();

/* ── 1. gate renders, direct and behind a path-prefix proxy ──────────────── */
console.log('\n[1] Sign-in page renders (bare + behind a reverse proxy)');
for (const [label, url] of [['direct', APP + '/'], ['proxy /od', PROXY]]) {
  const p = await newPage(browser);
  await p.goto(url, { waitUntil: 'networkidle2' });
  await p.waitForSelector('#p-login.on', { timeout: 15000 }).catch(() => {});
  const st = await p.evaluate(() => ({
    login: !!document.querySelector('#p-login.on'),
    bootErr: !!document.querySelector('#p-boot-err.on'),
    errText: document.getElementById('boot-err-msg')?.textContent || '',
    hasForm: !!document.querySelector('#l-user') &&
             getComputedStyle(document.querySelector('#p-login')).display !== 'none',
  }));
  ok(`${label}: login panel visible`, st.login && st.hasForm,
     st.bootErr ? 'boot error: ' + st.errText.slice(0, 80) : '');
  const jsonErr = p.errors.filter(e => /not valid JSON|Unexpected token/.test(e));
  ok(`${label}: no unhandled JSON/parse error`, jsonErr.length === 0, jsonErr[0] || '');
  await p.close();
}

/* ── 1b. login actually works through the proxy prefix ───────────────────── */
console.log('\n[1b] Login + listing through the proxy prefix');
{
  await post(MOCK + '/__reset'); await post(MOCK + '/__seed', { n: 6 });
  const p = await newPage(browser);
  await login(p, PROXY);
  const n = await p.evaluate(() => document.querySelectorAll('#tbody tr').length);
  ok('proxied login reaches the file list', n === 6, `${n} rows`);
  const badUrls = p.errors.filter(e => /404/.test(e) && !/favicon/.test(e));
  ok('no request escaped the /od prefix', badUrls.length === 0, badUrls[0] || '');
  await p.close();
}

/* ── 2. selection: animates in place, no full re-render ──────────────────── */
console.log('\n[2] Selecting an item animates in place (no list rebuild)');
{
  await post(MOCK + '/__reset'); await post(MOCK + '/__seed', { n: 12 });
  const p = await newPage(browser);
  await login(p, APP);
  const res = await p.evaluate(async () => {
    const rows = [...document.querySelectorAll('#tbody tr')];
    const target = rows[3];
    target.dataset.probe = 'sentinel';                 // survives only if NOT rebuilt
    const imgsBefore = document.querySelectorAll('#grid img').length;
    const before = getComputedStyle(target).backgroundColor;
    const hasTransition = getComputedStyle(target).transitionDuration;
    await new Promise(r => setTimeout(r, 600));   // let the initial entry animation finish
    target.querySelector('input[type=checkbox]').click();
    await new Promise(r => setTimeout(r, 40));
    const same = document.querySelectorAll('#tbody tr')[3];
    return {
      sameNode: same === target,
      probeKept: same?.dataset.probe === 'sentinel',
      selClass: same?.classList.contains('sel'),
      bg: getComputedStyle(same).backgroundColor,
      before, hasTransition,
      selCount: document.getElementById('stSel').textContent,
      rowCount: document.querySelectorAll('#tbody tr').length,
      areaFresh: document.getElementById('area').classList.contains('fresh'),
    };
  });
  ok('row DOM node is reused, not rebuilt', res.sameNode && res.probeKept);
  ok('row gets .sel', res.selClass);
  ok('highlight actually changes', res.bg !== res.before, `${res.before} -> ${res.bg}`);
  ok('row has a CSS transition to animate', parseFloat(res.hasTransition) > 0, res.hasTransition);
  ok('entry animation not replayed on select', res.areaFresh === false);
  ok('status shows the selection', /1 selected/.test(res.selCount), res.selCount);
  ok('row count unchanged', res.rowCount === 12, String(res.rowCount));

  // select-all + partial state
  const all = await p.evaluate(async () => {
    document.getElementById('all').click();
    await new Promise(r => setTimeout(r, 30));
    const n = document.querySelectorAll('#tbody tr.sel').length;
    document.querySelectorAll('#tbody tr')[0].querySelector('input[type=checkbox]').click();
    await new Promise(r => setTimeout(r, 30));
    return { n, indeterminate: document.getElementById('all').indeterminate,
             after: document.querySelectorAll('#tbody tr.sel').length };
  });
  ok('select-all marks every row', all.n === 12, String(all.n));
  ok('partial selection shows indeterminate', all.indeterminate === true);
  ok('deselecting one leaves the rest', all.after === 11, String(all.after));
  await p.close();
}

/* ── 3. instant delete + the list actually updates ───────────────────────── */
console.log('\n[3] Delete is instant and the view updates');
{
  await post(MOCK + '/__reset'); await post(MOCK + '/__seed', { n: 10 });
  const p = await newPage(browser);
  p.on('dialog', d => d.accept());
  await login(p, APP);
  const r = await p.evaluate(async () => {
    const before = document.querySelectorAll('#tbody tr').length;
    const name = document.querySelectorAll('#tbody tr')[0].querySelector('.fname span').textContent;
    const t0 = performance.now();
    document.querySelectorAll('#tbody tr')[0]
      .querySelector('.ra.dng').click();
    // measure how long until the row is visually gone
    let ms = -1;
    for (let i = 0; i < 100; i++) {
      await new Promise(r => setTimeout(r, 10));
      const row = [...document.querySelectorAll('#tbody tr')]
        .find(x => x.querySelector('.fname span')?.textContent === name);
      if (!row || row.classList.contains('gone')) { ms = performance.now() - t0; break; }
    }
    return { before, name, ms };
  });
  ok('row disappears immediately (<400ms)', r.ms >= 0 && r.ms < 400, `${r.ms.toFixed(0)}ms`);
  await sleep(2500);
  const after = await p.evaluate(() => document.querySelectorAll('#tbody tr').length);
  const st = await items();
  ok('row stays gone after the background re-list', after === r.before - 1, `${after} rows`);
  ok('item really deleted server-side', !(r.name in st.files), r.name);

  // batch delete of a multi-selection
  const b = await p.evaluate(async () => {
    document.getElementById('all').click();
    await new Promise(r => setTimeout(r, 40));
    const n = document.querySelectorAll('#tbody tr.sel').length;
    const t0 = performance.now();
    document.querySelector('[onclick="delSelected()"]')?.click()
      || [...document.querySelectorAll('button,.tb')].find(x => /delete/i.test(x.textContent))?.click();
    let ms = -1;
    for (let i = 0; i < 200; i++) {
      await new Promise(r => setTimeout(r, 10));
      if (document.querySelectorAll('#tbody tr:not(.gone)').length === 0) { ms = performance.now() - t0; break; }
    }
    return { n, ms };
  });
  ok('multi-delete clears the view fast (<600ms)', b.ms >= 0 && b.ms < 600, `${b.ms.toFixed(0)}ms for ${b.n}`);
  await sleep(2500);
  const st2 = await items();
  ok('all selected items deleted server-side',
     Object.keys(st2.files).filter(f => f.startsWith('seed')).length === 0,
     JSON.stringify(Object.keys(st2.files)).slice(0, 80));
  await p.close();
}

/* ── 4. bulk upload past the throttle point ──────────────────────────────── */
console.log('\n[4] 25-file upload with Graph throttling createUploadSession at 20');
{
  await post(MOCK + '/__reset');
  await post(MOCK + '/__config', { throttle_after: 20, throttle_window: 8,
                                   frag_throttle_after: 15, frag_window: 8 });
  const p = await newPage(browser);
  await login(p, APP);
  await uploadFiles(p, 25, 4096);
  await waitUploadDone(p, 240000);
  const st = await items();
  const bulk = Object.entries(st.files).filter(([k]) => k.startsWith('bulk'));
  const full = bulk.filter(([, v]) => v === 4096);
  const zero = bulk.filter(([, v]) => v === 0);
  ok('all 25 files uploaded at full size', full.length === 25,
     `${full.length} full, ${zero.length} zero-byte, ${25 - bulk.length} missing`);
  ok('no 0-byte placeholders left behind', zero.length === 0, JSON.stringify(zero.map(z => z[0])));
  ok('no upload sessions leaked', st.open_sessions === 0, String(st.open_sessions));
  const shown = await p.evaluate(() => document.querySelectorAll('#tbody tr').length);
  ok('uploaded files appear in the list without a manual refresh', shown >= 25, `${shown} rows`);
  await p.close();
}

/* ── 5. large (resumable) upload still works, incl. turbo proxy path ─────── */
console.log('\n[5] Multi-fragment resumable upload (> small-file threshold)');
{
  await post(MOCK + '/__reset');
  const p = await newPage(browser);
  await login(p, APP);
  await p.evaluate(() => { SMALL_MAX = 64 * 1024; });     // force the resumable path
  await p.evaluate(async () => {
    const dt = new DataTransfer();
    dt.items.add(new File([new Uint8Array(300 * 1024).fill(7)], 'big.bin',
                          { type: 'application/octet-stream' }));
    const inp = document.querySelector('input[type=file]');
    inp.files = dt.files;
    inp.dispatchEvent(new Event('change', { bubbles: true }));
  });
  await waitUploadDone(p, 120000);
  const st = await items();
  ok('resumable upload lands complete', st.files['big.bin'] === 300 * 1024,
     `${st.files['big.bin']} bytes`);
  ok('no session left open', st.open_sessions === 0, String(st.open_sessions));
  await p.close();
}

/* ── 6. delete then re-upload the same name (tombstones must not hide it) ─ */
console.log('\n[6] Re-uploading a just-deleted name shows the new file');
{
  await post(MOCK + '/__reset');
  const p = await newPage(browser);
  p.on('dialog', d => d.accept());
  await login(p, APP);
  await uploadFiles(p, 1, 2048);
  await waitUploadDone(p);
  await p.waitForFunction(() => document.querySelectorAll('#tbody tr').length === 1,
                          { timeout: 20000 });
  await p.evaluate(async () => {
    document.querySelector('#tbody tr .ra.dng').click();
    await new Promise(r => setTimeout(r, 300));
  });
  await sleep(1200);
  let n = await p.evaluate(() => document.querySelectorAll('#tbody tr').length);
  ok('file is gone after delete', n === 0, `${n} rows`);
  await uploadFiles(p, 1, 2048);                      // same name again
  await waitUploadDone(p);
  await sleep(1800);
  n = await p.evaluate(() => document.querySelectorAll('#tbody tr:not(.gone)').length);
  const st = await items();
  ok('re-uploaded file is visible again', n === 1, `${n} rows`);
  ok('and present server-side at full size', st.files['bulk000.bin'] === 2048,
     String(st.files['bulk000.bin']));
  await p.close();
}

await browser.close();
const t = ok.tally();
console.log(`\n=== ${t.pass} passed, ${t.fail} failed ===`);
process.exit(t.fail ? 1 : 0);
