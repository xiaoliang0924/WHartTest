const { test } = require('node:test');
const assert = require('node:assert/strict');
const { chromium } = require('playwright');
const { queryFilterResults } = require('../lib/helpers');
test('query waits for asynchronous response and all masks and reports failures', async () => {
  const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
  const page = await browser.newPage();
  let status = 200;
  await page.route('https://fixture.test/**', async route => {
    if (route.request().url().endsWith('/search')) {
      await new Promise(resolve => setTimeout(resolve, 150));
      return route.fulfill({ status, contentType: 'application/json', body: '{}' });
    }
    await route.fulfill({ contentType: 'text/html; charset=utf-8', body: '<!doctype html><html><head><meta charset="utf-8"></head><body><button onclick="runQuery()">查询</button><div class="el-loading-mask" style="display:none;width:20px;height:20px"></div><div class="el-loading-mask" style="display:none;width:20px;height:20px"></div><script>async function runQuery(){document.querySelectorAll(".el-loading-mask").forEach(el=>el.style.display="block");await fetch("/search");document.body.dataset.done="true";document.querySelectorAll(".el-loading-mask").forEach(el=>el.style.display="none");}</script></body></html>' });
  });
  try {
    await page.goto('https://fixture.test/');
    await queryFilterResults(page);
    assert.equal(await page.locator('body').getAttribute('data-done'), 'true');
    assert.equal(await page.locator('.el-loading-mask:visible').count(), 0);
    status = 500;
    await assert.rejects(queryFilterResults(page), /查询期间请求失败/);
    await page.setContent('<button>查询</button><button>查询</button>');
    await assert.rejects(queryFilterResults(page), /不唯一/);
  } finally { await browser.close(); }
});
