// Shared puppeteer helpers: launch system chromium, log in through the gate, enter the app.
import puppeteer from 'puppeteer-core';

const CHROME = process.env.CHROME_PATH || '/usr/bin/chromium-browser';

export async function launch(opts = {}) {
  return puppeteer.launch({
    executablePath: CHROME,
    headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu',
           '--no-first-run', '--disable-features=site-per-process'],
    ...opts,
  });
}

export async function newPage(browser, { collectErrors = true } = {}) {
  const page = await browser.newPage();
  page.errors = [];
  page.logs = [];
  if (collectErrors) {
    page.on('pageerror', e => page.errors.push(String(e.message || e)));
    page.on('console', m => {
      page.logs.push(`${m.type()}: ${m.text()}`);
      if (m.type() === 'error') page.errors.push(m.text());
    });
  }
  return page;
}

export async function login(page, base, user = 'tester', pass = 'hunter2pass') {
  await page.goto(base + '/', { waitUntil: 'networkidle2' });
  // The browser profile is shared across tests, so we may already hold a session and be
  // looking at the app rather than the gate.
  await page.waitForSelector('#p-login.on, #tbody', { timeout: 15000 });
  if (await page.$('#p-login.on')) {
    await page.evaluate(() => { document.getElementById('l-user').value = '';
                                document.getElementById('l-pass').value = ''; });
    await page.type('#l-user', user);
    await page.type('#l-pass', pass);
    await Promise.all([
      page.waitForNavigation({ waitUntil: 'networkidle2', timeout: 20000 }).catch(() => {}),
      page.click('#l-btn'),
    ]);
  }
  await page.waitForSelector('#tbody tr, #empty', { timeout: 20000 });
  // wait for the initial listing to settle
  await page.waitForFunction(() => !/Loading/.test(document.getElementById('stMsg')?.textContent || ''),
                             { timeout: 20000 }).catch(() => {});
  return page;
}

export async function logout(page) {
  const cl = await page.target().createCDPSession();
  await cl.send('Network.clearBrowserCookies');
  await cl.detach();
}

// Inject N synthetic files into the real <input type=file> via DataTransfer, then fire change.
export async function uploadFiles(page, n, bytesEach = 1024) {
  return page.evaluate(async (n, bytesEach) => {
    const dt = new DataTransfer();
    for (let i = 0; i < n; i++) {
      const body = new Uint8Array(bytesEach);
      for (let j = 0; j < bytesEach; j++) body[j] = (i + j) % 251;
      dt.items.add(new File([body], `bulk${String(i).padStart(3, '0')}.bin`,
                            { type: 'application/octet-stream' }));
    }
    const inp = document.querySelector('input[type=file]');
    inp.files = dt.files;
    inp.dispatchEvent(new Event('change', { bubbles: true }));
    return dt.files.length;
  }, n, bytesEach);
}

export async function waitUploadDone(page, timeoutMs = 180000) {
  await page.waitForFunction(
    () => document.getElementById('utoast')?.style.display === 'none',
    { timeout: timeoutMs, polling: 300 });
}

export const ok = (() => {
  let pass = 0, fail = 0;
  const f = (name, cond, extra = '') => {
    console.log(`  ${cond ? '[OK]' : '[XX]'} ${name}${extra ? '  ' + extra : ''}`);
    cond ? pass++ : fail++;
    return cond;
  };
  f.tally = () => ({ pass, fail });
  return f;
})();
