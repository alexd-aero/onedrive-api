/* Grid-view selection, insecure-context login, and gate error reporting. */
import { launch, newPage, login, logout, ok } from './browser.mjs';

const APP  = process.env.APP  || 'http://127.0.0.1:3300';
const MOCK = process.env.MOCK || 'http://127.0.0.1:5999';
const post = (u, b) => fetch(u, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                  body: JSON.stringify(b || {}) }).then(r => r.json());

const browser = await launch();

/* ── grid view: same surgical selection, thumbnails not re-fetched ────────── */
console.log('\n[G] Grid view selection');
{
  await post(MOCK + '/__reset'); await post(MOCK + '/__seed', { n: 8 });
  const p = await newPage(browser);
  await login(p, APP);
  let thumbReqs = 0;
  p.on('request', r => { if (r.url().includes('/thumb?')) thumbReqs++; });
  await p.evaluate(() => setView('grid'));
  await new Promise(r => setTimeout(r, 900));
  const base = thumbReqs;
  const res = await p.evaluate(async () => {
    const tiles = [...document.querySelectorAll('.gi')];
    tiles[2].dataset.probe = 'keep';
    const before = getComputedStyle(tiles[2]).borderColor;
    tiles[2].querySelector('input[type=checkbox]').click();
    await new Promise(r => setTimeout(r, 60));
    const same = document.querySelectorAll('.gi')[2];
    return { sameNode: same === tiles[2], probeKept: same?.dataset.probe === 'keep',
             sel: same?.classList.contains('sel'),
             borderChanged: getComputedStyle(same).borderColor !== before,
             transform: getComputedStyle(same).transform,
             count: document.querySelectorAll('.gi').length };
  });
  ok('grid tile reused, not rebuilt', res.sameNode && res.probeKept);
  ok('tile gets .sel + visible change', res.sel && res.borderChanged);
  ok('tile lifts (animated transform)', res.transform !== 'none', res.transform);
  ok('tile count unchanged', res.count === 8, String(res.count));
  await new Promise(r => setTimeout(r, 500));
  ok('selecting did not re-fetch thumbnails', thumbReqs === base,
     `${base} before, ${thumbReqs} after`);
  await p.close();
}

/* ── login works without Web Crypto (plain http behind a proxy) ───────────── */
console.log('\n[C] Login in an insecure context (no crypto.subtle)');
{
  await post(MOCK + '/__reset'); await post(MOCK + '/__seed', { n: 3 });
  const p = await newPage(browser);
  await logout(p);
  // Emulate a non-secure context: subtle absent, exactly as on http:// over a LAN IP.
  await p.evaluateOnNewDocument(() => {
    Object.defineProperty(window.crypto, 'subtle', { get: () => undefined, configurable: true });
  });
  await p.goto(APP + '/', { waitUntil: 'networkidle2' });
  await p.waitForSelector('#p-login.on', { timeout: 15000 });
  ok('gate still renders without crypto.subtle', true);
  const noSubtle = await p.evaluate(() => SUBTLE === null);
  ok('SUBTLE resolved to null (fallback active)', noSubtle);
  await p.evaluate(() => { document.getElementById('l-user').value = '';
                           document.getElementById('l-pass').value = ''; });
  await p.type('#l-user', 'tester');
  await p.type('#l-pass', 'hunter2pass');
  await Promise.all([
    p.waitForNavigation({ waitUntil: 'networkidle2', timeout: 60000 }).catch(() => {}),
    p.click('#l-btn'),
  ]);
  await p.waitForSelector('#tbody tr, #empty', { timeout: 60000 }).catch(() => {});
  const inApp = await p.evaluate(() => !!document.getElementById('tbody'));
  const err = await p.evaluate(() => document.getElementById('l-err')?.textContent || '');
  ok('pure-JS PBKDF2 login succeeds', inApp, err || '');
  await p.close();
}

/* ── a broken /api/state names itself instead of rendering a blank card ───── */
console.log('\n[E] Gate reports a proxy that mangles /api/state');
{
  const p = await newPage(browser);
  await logout(p);
  await p.setRequestInterception(true);
  p.on('request', r => {
    if (r.url().includes('/api/state')) {
      return r.respond({ status: 502, contentType: 'text/html',
                         body: '<!doctype html><h1>502 Bad Gateway</h1>' });
    }
    r.continue();
  });
  await p.goto(APP + '/', { waitUntil: 'networkidle2' });
  await p.waitForSelector('#p-boot-err.on', { timeout: 15000 }).catch(() => {});
  const st = await p.evaluate(() => ({
    shown: !!document.querySelector('#p-boot-err.on'),
    msg: document.getElementById('boot-err-msg')?.textContent || '',
    anyPanel: [...document.querySelectorAll('.panel')].some(x => x.classList.contains('on')),
  }));
  ok('an explicit error panel is shown, not a blank card', st.shown && st.anyPanel);
  ok('the message explains the cause', /proxy|JSON|502/i.test(st.msg), st.msg.slice(0, 90));
  await p.close();
}

await browser.close();
const t = ok.tally();
console.log(`\n=== ${t.pass} passed, ${t.fail} failed ===`);
process.exit(t.fail ? 1 : 0);
