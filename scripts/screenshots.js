// Regenerate docs/screenshots against the demo server:
//   docker run -d --name smsmonster-demo -v "$PWD":/app -e SMSMONSTER_DATA_DIR=/tmp/demo smsmonster:dev python tests/demo_server.py
//   docker run --rm --network container:smsmonster-demo -v "$PWD/docs/screenshots":/out -v "$PWD/scripts":/s:ro mcr.microsoft.com/playwright:v1.64.1-noble bash -c 'cd /tmp && npm i -s playwright@1.64.1 && NODE_PATH=/tmp/node_modules node /s/screenshots.js'
const { chromium, request } = require('playwright');
const base = 'http://127.0.0.1:8080/';
(async () => {
  const api = await request.newContext({ baseURL: base, extraHTTPHeaders: { 'Content-Type': 'application/json' } });
  await api.patch('api/modems/861234567890123', { data: { name: 'Office SIM', phone_number: '+491701234567' } });
  await api.patch('api/modems/861234567890456', { data: { name: 'Alerts SIM', phone_number: '+491709876543' } });
  await api.post('api/messages', { data: { to: '+491761234567', text: 'Backup finished on nas-01: 412 GB, 0 errors.', modem_id: '861234567890123' } });
  await api.post('api/messages', { data: { to: '+491525551234', text: 'Hi Lena, the meeting moved to 15:30 in room B. See you there! 😊', modem_id: '861234567890456' } });
  await api.post('api/messages', { data: { to: '+4915112345678', text: 'Garage door has been open for 20 minutes.' } });
  await api.post('api/webhooks', { data: { url: 'http://homeassistant.local:8123/api/webhook/smsmonster', secret: 's3cret', events: ['message.received', 'call.received'] } });
  await api.post('api/webhooks', { data: { url: 'https://n8n.example.org/webhook/sms-sent', events: ['message.sent', 'message.failed'] } });
  await new Promise((r) => setTimeout(r, 3000));
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 2, colorScheme: 'dark' });
  const page = await ctx.newPage();
  const shot = (n) => page.screenshot({ path: `/out/${n}.png` });
  await page.goto(base + '#modems'); await page.waitForSelector('#modem-list .modem, #modem-list > *'); await page.waitForTimeout(1500);
  await shot('modems');
  await page.goto(base + '#messages'); await page.waitForSelector('#msg-list li'); await page.waitForTimeout(800);
  await shot('messages');
  await page.goto(base + '#send'); await page.waitForTimeout(500);
  await page.fill('#s-to', '+491761234567');
  await page.fill('#s-text', 'Hello from SMS Monster 👋 — this message is long enough to be split into two parts so the counter shows the part math.');
  await page.waitForTimeout(300); await shot('send');
  await page.goto(base + '#webhooks'); await page.waitForSelector('#hook-list li'); await page.waitForTimeout(500);
  await shot('webhooks');
  await page.goto(base + '#modems'); await page.waitForTimeout(1000);
  await page.getByText('AT console').first().click();
  for (const c of ['AT+CSQ', 'AT+COPS?', 'AT+CPIN?']) { await page.fill('#at-cmd', c); await page.press('#at-cmd', 'Enter'); await page.waitForTimeout(700); }
  await shot('at-console');
  await page.keyboard.press('Escape'); await page.waitForTimeout(300);
  await page.getByText('USSD').first().click();
  await page.fill('#ussd-code', '*100#'); await page.press('#ussd-code', 'Enter'); await page.waitForTimeout(1500);
  await shot('ussd');
  await page.keyboard.press('Escape');
  const light = await browser.newContext({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 2, colorScheme: 'light' });
  const dp = await light.newPage();
  await dp.goto(base + '#messages'); await dp.waitForSelector('#msg-list li'); await dp.waitForTimeout(800);
  await dp.screenshot({ path: '/out/messages-light.png' });
  const mob = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, isMobile: true, colorScheme: 'dark' });
  const mp = await mob.newPage();
  await mp.goto(base + '#modems'); await mp.waitForTimeout(1500);
  await mp.screenshot({ path: '/out/mobile-modems.png' });
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
