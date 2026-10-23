// Renders one Home Assistant card (or a dashboard view) with Home Assistant's
// own frontend in headless Chromium and prints {"png": base64} or {"error"}.
//
// Started per request by bot/render_broker.py as the unprivileged `render`
// user. Input (stdin, JSON): {"card": {...}, "period": "today", "language": "de"}
// or {"dashboard": "dashboard-heating/raume"}; the broker has validated it.
// Environment: HA_URL, HA_TOKEN (the app's ha_token), CHROMIUM.
// The browser may only load Home Assistant's own origin.
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import puppeteer from 'puppeteer-core';

const WIDTH = 520;          // like a card in a phone-sized column
const SETTLE_MS = 2500;     // charts animate in (ECharts: 500 ms) after data arrives
const TIMEOUT_MS = 45000;
// Energy cards get their own data collection, whose period we preset the way
// the energy dashboard remembers the last picked one.
const COLLECTION_KEY = 'energy_homebot';

const out = (obj) => process.stdout.write(JSON.stringify(obj) + '\n');

async function readStdin() {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  return JSON.parse(Buffer.concat(chunks).toString('utf8'));
}

async function main() {
  const request = await readStdin();
  const base = new URL(process.env.HA_URL);
  const origin = base.origin;
  const profile = await mkdtemp(join(tmpdir(), 'homebot-render-'));
  const browser = await puppeteer.launch({
    executablePath: process.env.CHROMIUM || '/usr/bin/chromium',
    headless: true,
    userDataDir: profile,
    // No sandbox: the container has no user namespaces for it; the process
    // already runs as its own user without capabilities (see run.sh).
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu', '--no-first-run',
           '--disable-extensions', '--disable-sync', '--mute-audio', '--hide-scrollbars',
           '--disable-background-networking'],
  });
  try {
    const page = await browser.newPage();
    page.setDefaultTimeout(TIMEOUT_MS);
    await page.setViewport({ width: request.dashboard ? 1100 : WIDTH + 32, height: 1400, deviceScaleFactor: 2 });
    await page.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: 'light' }]);
    if (process.env.TZ) await page.emulateTimezone(process.env.TZ);
    await page.setRequestInterception(true);
    page.on('request', (req) => {
      const url = req.url();
      if (url.startsWith('data:') || url.startsWith('blob:')) return req.continue();
      try {
        if (new URL(url).origin === origin) return req.continue();
      } catch { /* fall through */ }
      return req.abort();
    });
    // Log in the way the frontend stores its own session; the token never
    // expires, so the frontend never tries to refresh it.
    await page.evaluateOnNewDocument((tokens, periodKey, period, language) => {
      localStorage.setItem('hassTokens', JSON.stringify(tokens));
      if (period) localStorage.setItem(periodKey, period);
      if (language) localStorage.setItem('selectedLanguage', JSON.stringify(language));
      localStorage.setItem('dockedSidebar', JSON.stringify('always_hidden'));  // just the dashboard
    }, {
      access_token: process.env.HA_TOKEN, token_type: 'Bearer', expires_in: 1e9,
      hassUrl: origin, clientId: origin + '/', expires: Date.now() + 1e12, refresh_token: '',
    }, `energy-default-period-_${COLLECTION_KEY}`, request.period || null, request.language || null);

    const target = request.dashboard ? `${origin}/${request.dashboard}` : `${origin}/lovelace`;
    await page.goto(target, { waitUntil: 'networkidle2' });
    // Ready like a dashboard: connected, with states, registries, themes and translations.
    await page.waitForFunction(() => {
      const hass = document.querySelector('home-assistant')?.hass;
      return hass && hass.connected && hass.states && hass.entities && hass.themes && hass.user
        && hass.localize && hass.localize('ui.common.cancel') !== '';
    });

    if (request.dashboard) {
      await new Promise((r) => setTimeout(r, SETTLE_MS));
      const png = await page.screenshot({ type: 'png' });
      return out({ png: Buffer.from(png).toString('base64') });
    }

    await page.waitForFunction(() => typeof window.loadCardHelpers === 'function');
    const card = { ...request.card };
    if (String(card.type || '').startsWith('energy-')) card.collection_key = COLLECTION_KEY;
    const problem = await page.evaluate(async (config, width) => {
      const ha = document.querySelector('home-assistant');
      const helpers = await window.loadCardHelpers();
      const el = helpers.createCardElement(config);
      el.hass = ha.hass;
      const box = document.createElement('div');
      box.id = 'homebot-card';
      box.style.cssText = `position:fixed;left:0;top:0;width:${width}px;padding:16px;z-index:2147483647;` +
        'background:var(--primary-background-color, #fafafa);box-sizing:content-box;';
      box.appendChild(el);
      // Inside the app element: cards get themes etc. from its context providers.
      (ha.shadowRoot || ha).appendChild(box);
      // Feed the card every new hass object, as a dashboard does.
      setInterval(() => { if (el.hass !== ha.hass) el.hass = ha.hass; }, 200);
      await new Promise((r) => setTimeout(r, 50));
      return el.tagName.toLowerCase() === 'hui-error-card' ? 'Home Assistant could not show this card' : null;
    }, card, WIDTH);
    if (problem) return out({ error: problem });
    await page.waitForNetworkIdle({ idleTime: 500 }).catch(() => {});
    // Charts measure their size when they first draw; attach the card again
    // now that it is laid out, so they draw at their real width.
    await page.evaluate(() => {
      const ha = document.querySelector('home-assistant');
      const box = (ha.shadowRoot || ha).getElementById('homebot-card');
      const el = box.firstElementChild;
      box.removeChild(el);
      box.appendChild(el);
    });
    await new Promise((r) => setTimeout(r, SETTLE_MS));
    const box = await page.$('pierce/#homebot-card');
    const png = await box.screenshot({ type: 'png' });
    out({ png: Buffer.from(png).toString('base64') });
  } finally {
    await browser.close().catch(() => {});
    await rm(profile, { recursive: true, force: true }).catch(() => {});
  }
}

main().catch((err) => {
  out({ error: String(err && err.name || 'Error') + ': ' + String(err && err.message || err).slice(0, 200) });
  process.exitCode = 1;
});
