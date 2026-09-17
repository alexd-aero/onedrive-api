/* Browser turbo path: one continuous stream to the host, host drives Microsoft. */
import { launch, newPage, login, waitUploadDone, ok } from './browser.mjs';

const APP  = process.env.APP  || 'http://127.0.0.1:3300';
const MOCK = process.env.MOCK || 'http://127.0.0.1:5999';
const post = (u, b) => fetch(u, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                  body: JSON.stringify(b || {}) }).then(r => r.json());
const items = () => fetch(MOCK + '/__items').then(r => r.json());

const browser = await launch();

console.log('\n[T] Turbo uses the single-request stream path');
{
  await post(MOCK + '/__reset');
  const p = await newPage(browser);
  await login(p, APP);
  const on = await p.evaluate(() => TURBO);
  ok('turbo is on (from server config)', on === true, String(on));

  const calls = [];
  p.on('request', r => {
    const u = r.url();
    if (u.includes('/upload/')) calls.push(r.method() + ' ' + u.split('?')[0].replace(APP, ''));
  });

  const MB = 12;
  await p.evaluate(async mb => {
    const dt = new DataTransfer();
    dt.items.add(new File([new Uint8Array(mb * 1024 * 1024).fill(9)], 'turbo.bin',
                          { type: 'application/octet-stream' }));
    const inp = document.querySelector('input[type=file]');
    inp.files = dt.files;
    inp.dispatchEvent(new Event('change', { bubbles: true }));
  }, MB);
  await waitUploadDone(p, 240000);

  const st = await items();
  ok('file lands complete', st.files['turbo.bin'] === MB * 1024 * 1024,
     `${st.files['turbo.bin']} of ${MB * 1024 * 1024}`);
  ok('no session leaked', st.open_sessions === 0, String(st.open_sessions));
  ok('used ONE /upload/stream request, not per-fragment PUTs',
     calls.filter(c => c.includes('/upload/stream')).length === 1 &&
     calls.filter(c => c.includes('/upload/proxy')).length === 0,
     JSON.stringify(calls));
  const shown = await p.evaluate(() => document.querySelectorAll('#tbody tr').length);
  ok('appears in the list without a manual refresh', shown >= 1, `${shown} rows`);
  if (p.errors.length) console.log('  page errors:', p.errors.slice(0, 3));
  await p.close();
}

console.log('\n[T2] Turbo off falls back to per-fragment resumable');
{
  await post(MOCK + '/__reset');
  const p = await newPage(browser);
  await login(p, APP);
  await p.evaluate(() => { TURBO = false; });
  const calls = [];
  p.on('request', r => { const u = r.url(); if (u.includes('/upload/')) calls.push(u.split('?')[0]); });
  await p.evaluate(async () => {
    SMALL_MAX = 64 * 1024;
    const dt = new DataTransfer();
    dt.items.add(new File([new Uint8Array(300 * 1024).fill(3)], 'frag.bin',
                          { type: 'application/octet-stream' }));
    const inp = document.querySelector('input[type=file]');
    inp.files = dt.files;
    inp.dispatchEvent(new Event('change', { bubbles: true }));
  });
  await waitUploadDone(p, 120000);
  const st = await items();
  ok('resumable path still lands the file', st.files['frag.bin'] === 300 * 1024,
     String(st.files['frag.bin']));
  ok('and it did not use /upload/stream',
     calls.filter(c => c.includes('/upload/stream')).length === 0, JSON.stringify(calls));
  await p.close();
}

await browser.close();
const t = ok.tally();
console.log(`\n=== ${t.pass} passed, ${t.fail} failed ===`);
process.exit(t.fail ? 1 : 0);
