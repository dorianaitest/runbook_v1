// E2E check for the LibreChat citations spike (Step 5 of
// docs/spec/librechat-rag-citations-spike-spec.md). Run locally on the VPN, see ../README.md.
// Selectors follow LibreChat's own e2e tests (data-testid text-input, send-button, message-body).
import { mkdirSync } from 'node:fs';
import { chromium } from 'playwright-core';

const BASE = (process.env.LIBRECHAT_URL || 'https://chat.demo-ki-instanz.de').replace(/\/$/, '');
const EMAIL = process.env.LIBRECHAT_EMAIL;
const PASSWORD = process.env.LIBRECHAT_PASSWORD;
const HEADLESS = process.env.HEADLESS !== '0';
const SHOTS = new URL('./screenshots/', import.meta.url).pathname;

if (!EMAIL || !PASSWORD) {
  console.error('Set LIBRECHAT_EMAIL and LIBRECHAT_PASSWORD.');
  process.exit(2);
}
mkdirSync(SHOTS, { recursive: true });

const results = [];
const check = (name, ok, detail = '') => {
  results.push(ok);
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? `  (${detail})` : ''}`);
};

const browser = await chromium.launch({ headless: HEADLESS, executablePath: process.env.CHROME_PATH || undefined });
const context = await browser.newContext({ viewport: { width: 1400, height: 1000 } });
const page = await context.newPage();
const bodies = page.getByTestId('message-body');
const shot = (p, name) => p.screenshot({ path: `${SHOTS}${name}.png`, fullPage: true });

// Waits until there are `count` message bodies and the last one stops changing (streaming done).
async function waitForAnswer(count, timeout = 60_000) {
  const end = Date.now() + timeout;
  let last = '', stable = 0;
  while (Date.now() < end) {
    if ((await bodies.count()) >= count) {
      const text = await bodies.last().innerText();
      stable = text && text === last ? stable + 1 : 0;
      last = text;
      if (stable >= 4) return text;
    }
    await page.waitForTimeout(500);
  }
  throw new Error(`no finished answer after ${timeout} ms (have ${await bodies.count()} messages, want ${count})`);
}

async function ask(text, count) {
  await page.getByTestId('text-input').fill(text);
  await page.getByTestId('send-button').click();
  return waitForAnswer(count);
}

const citationLinks = () => bodies.last().locator('a[href*="/source/"]');

try {
  // 0. Login
  await page.goto(`${BASE}/login`, { waitUntil: 'domcontentloaded' });
  await page.locator('input[name="email"]').fill(EMAIL);
  await page.locator('input[name="password"]').fill(PASSWORD);
  await page.getByTestId('login-button').click();
  await page.getByTestId('text-input').waitFor({ timeout: 20_000 });
  check('login', true);

  // Pick the RAG-Demo spec via URL; fall back to the picker if the param is ignored.
  await page.goto(`${BASE}/c/new?spec=rag-demo`, { waitUntil: 'domcontentloaded' });
  await page.getByTestId('text-input').waitFor({ timeout: 20_000 });
  const selector = page.getByTestId('model-selector-button');
  if (!(await selector.innerText()).includes('RAG-Demo')) {
    await selector.click();
    await page.getByRole('option', { name: /RAG-Demo/ }).first().click();
  }
  check('RAG-Demo selected', (await selector.innerText()).includes('RAG-Demo'), (await selector.innerText()).trim());
  await shot(page, '0-new-chat');

  // 1. Cited answer
  const answer1 = await ask('SN 20-4471 steht mit E-214. Was bedeutet das und was ist zu tun?', 2);
  await shot(page, '1-cited-answer');
  check('answer mentions E-214', answer1.includes('E-214'));

  // 2. Citation links 1-5, all pointing at /source/
  const links = await citationLinks().evaluateAll((as) => as.map((a) => ({ text: a.textContent.trim(), href: a.href })));
  const numbers = new Set(links.map((l) => l.text).filter((t) => /^\[\d\]$/.test(t)));
  check('citation markers [1]-[5] are links', [1, 2, 3, 4, 5].every((n) => numbers.has(`[${n}]`)), [...numbers].join(' '));
  check('Quellen list entries are links', links.some((l) => l.text.startsWith('BA-M200-DE')), `${links.length} source links`);

  // 3. Click citation 2 -> new tab on the cited section, highlighted
  const marker2 = citationLinks().filter({ hasText: /^\[2\]$/ }).first();
  const popupPromise = context.waitForEvent('page', { timeout: 10_000 }).catch(() => null);
  await marker2.click();
  const popup = await popupPromise;
  if (!popup) {
    check('citation opens a new tab', false, `same tab now at ${page.url()}`);
  } else {
    await popup.waitForLoadState('domcontentloaded');
    check('citation opens a new tab', true);
    check('tab URL is BA-M200-DE#s-7.3.1', popup.url().endsWith('/source/BA-M200-DE#s-7.3.1'), popup.url());
    const hl = await popup.evaluate(() => {
      const t = document.querySelector(':target');
      return { id: t?.id, bg: t ? getComputedStyle(t).backgroundColor : null };
    });
    check('section 7.3.1 highlighted', hl.id === 's-7.3.1' && hl.bg === 'rgb(255, 243, 176)', JSON.stringify(hl));
    await shot(popup, '3-source-page');
    await popup.close();
  }

  // 4. Off-topic -> abstention without citations
  const answer2 = await ask('Wie wird das Wetter morgen?', 4);
  await shot(page, '4-abstention');
  check('off-topic answer abstains', answer2.includes('UNZUREICHENDE BELEGE'));
  check('abstention has no source links', (await citationLinks().count()) === 0);

  // 5. Follow-up in the same chat
  const answer3 = await ask('Und was mache ich bei E-214 direkt nach einem Backenwechsel?', 6);
  await shot(page, '5-follow-up');
  check('follow-up answered with citations', answer3.includes('E-214') && (await citationLinks().count()) > 0);
} catch (err) {
  check('script ran to the end', false, err.message.split('\n')[0]);
  await shot(page, 'error').catch(() => {});
} finally {
  await browser.close();
}

const failed = results.filter((ok) => !ok).length;
console.log(`\n${results.length - failed}/${results.length} passed. Screenshots: ${SHOTS}`);
process.exit(failed ? 1 : 0);
