'use strict';

const { test, before, after } = require('node:test');
const assert = require('node:assert/strict');
const { createServer } = require('node:http');
const { createRequire } = require('node:module');
const { loginWithCredentials } = require('./environment_login');
const { chromium } = process.env.PLAYWRIGHT_SKILL_DIR
  ? createRequire(`${process.env.PLAYWRIGHT_SKILL_DIR}/package.json`)('playwright')
  : require('playwright');

let browser, server, origin;
const params = { username: 'fixture-user', password: 'fixture-secret' };
const form = (submit = '登录', hidden = false) => `
  <section role="tabpanel" aria-label="账号密码">
  ${hidden ? '<input type="password" hidden>' : ''}
  <label>账号<input placeholder="请输入账号"></label>
  <label>密码<input type="password" placeholder="请输入密码"></label>
  <button onclick="top.location.href='/business'">${submit}</button></section>`;

before(async () => {
  server = createServer((req, res) => {
    res.setHeader('Content-Type', 'text/html; charset=utf-8');
    const route = new URL(req.url, 'http://fixture.local').pathname;
    if (route === '/business') return res.end('<nav>业务页面</nav>');
    if (route === '/frame') return res.end(form('登录并继续', true));
    if (route === '/nested') return res.end('<iframe width="500" height="400" src="/frame"></iframe>');
    if (route === '/direct') return res.end(form());
    if (route === '/custom') return res.end('<input id="account"><input id="secret" type="password"><button id="submit" onclick="location.href=\'/business\'">继续</button>');
    if (route === '/missing') return res.end('<h1>登录页无法加载</h1>');
    if (route === '/rejected') return res.end('<label>账号<input></label><label>密码<input type="password"></label><button onclick="document.getElementById(\'error\').textContent=\'登录失败\'">登录</button><p id="error"></p>');
    const nested = route === '/iframe-nested';
    const delayed = route === '/delayed';
    res.end(`<div id="entry"></div><div id="modal" role="dialog"></div><script>
      function showEntry() {
        const button = document.createElement('button');
        button.textContent = '手动登录';
        button.onclick = () => {
          const iframe = document.createElement('iframe');
          iframe.src = '${nested ? '/nested' : '/frame'}';
          iframe.width = 500; iframe.height = 400;
          document.getElementById('modal').appendChild(iframe);
        };
        document.getElementById('entry').appendChild(button);
      }
      ${delayed ? 'setTimeout(showEntry, 200)' : 'showEntry()'};
    </script>`);
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  origin = `http://127.0.0.1:${server.address().port}`;
  browser = await chromium.launch({ headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
});
after(async () => {
  if (browser) await browser.close();
  if (server) await new Promise(resolve => server.close(resolve));
});

for (const route of ['/direct', '/iframe', '/iframe-nested', '/delayed']) {
  test(`fresh browser login succeeds for ${route}`, async () => {
    const context = await browser.newContext();
    const page = await context.newPage();
    try {
      const result = await loginWithCredentials(page, { ...params, url: origin + route });
      assert.equal(result.ok, true, result.error);
      assert.equal(page.url(), origin + '/business');
      assert.ok(result.state.duration < 10, 'must not wait for a detached password field');
    } finally { await context.close(); }
  });
}

test('custom selectors still work without a manual-login entry', async () => {
  const context = await browser.newContext();
  const page = await context.newPage();
  try {
    const result = await loginWithCredentials(page, {
      ...params, url: origin + '/custom', account_selector: '#account',
      password_selector: '#secret', submit_selector: '#submit', success_selector: 'nav',
    });
    assert.equal(result.ok, true, result.error);
  } finally { await context.close(); }
});

test('missing password form captures diagnostics without two extra cleanup timeouts', async () => {
  const context = await browser.newContext();
  const page = await context.newPage();
  try {
    const result = await loginWithCredentials(page, { ...params, url: origin + '/missing?token=private' });
    assert.equal(result.ok, false);
    assert.match(result.error, /主页面及嵌入登录页面/);
    assert.equal(result.state.page_url, origin + '/missing');
    assert.match(result.state.screenshot, /^data:image\/jpeg;base64,/);
    assert.ok(result.state.duration < 20);
  } finally { await context.close(); }
});

test('rejected login clears both credentials before capturing a screenshot', async () => {
  const context = await browser.newContext();
  const page = await context.newPage();
  try {
    const result = await loginWithCredentials(page, {
      ...params, url: origin + '/rejected', success_selector: '#not-present',
    });
    assert.equal(result.ok, false);
    assert.match(result.error, /验证登录成功/);
    assert.match(result.state.screenshot, /^data:image\/jpeg;base64,/);
    assert.deepEqual(await page.locator('input').evaluateAll(es => es.map(e => e.value)), ['', '']);
    assert.ok(!JSON.stringify(result).includes(params.password));
    assert.ok(!JSON.stringify(result).includes(params.username));
  } finally { await context.close(); }
});
