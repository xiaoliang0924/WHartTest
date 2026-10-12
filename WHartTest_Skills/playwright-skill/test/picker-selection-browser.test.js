const { test } = require('node:test');
const assert = require('node:assert/strict');
const { chromium } = require('playwright');
const { selectFilterDialogOption } = require('../lib/helpers');
const html = (selected = false, delay = 0, broken = false) => `<div class="el-dialog"><ul><li class="picker-item" role="option" aria-selected="false"><span class="picker-item-name">人工报价附加</span></li><li id="wanted" class="picker-item${selected ? ' is-selected' : ''}" role="option" aria-selected="${selected}"><span class="picker-item-name">人工报价</span></li></ul><div class="picker-footer"><button>确定（${selected ? 1 : 0}）</button></div></div><script>window.clicks=0;document.querySelector('#wanted').onclick=e=>{window.clicks++;setTimeout(()=>{${broken ? "document.querySelector('button').textContent='确定（1）';" : "const el=document.querySelector('#wanted');const value=el.getAttribute('aria-selected')!=='true';el.setAttribute('aria-selected',String(value));el.classList.toggle('is-selected',value);"}},${delay});};</script>`;

test('picker selection is idempotent, checks the exact row, and handles async state', async () => {
  const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
  const page = await browser.newPage();
  const select = (extra = {}) => selectFilterDialogOption(page, '工单类型', '人工报价', { dialog: page.locator('.el-dialog'), skipSearch: true, ...extra });
  try {
    await page.setContent(html(false, 250));
    await select(); await select(); await select();
    assert.equal(await page.locator('#wanted').getAttribute('aria-selected'), 'true');
    assert.equal(await page.evaluate(() => window.clicks), 1);
    assert.equal(await page.locator('.picker-item').first().getAttribute('aria-selected'), 'false');
    await page.setContent(html(true));
    await select(); await select();
    assert.equal(await page.evaluate(() => window.clicks), 0);
    await page.setContent(html(false, 0, true));
    await assert.rejects(select({ selectionTimeout: 200 }), /目标选项行仍未选中/);
    await page.setContent('<div class="el-dialog"><ul><li class="picker-item"><label><input type="checkbox"><span>人工报价</span></label></li></ul></div>');
    await select(); await select();
    assert.equal(await page.locator('input').isChecked(), true);
    await page.setContent(html(false) + '<li class="picker-item"><span>人工报价</span></li>');
    // A duplicate outside the active dialog must not interfere.
    await select();
    await page.setContent(html(false).replace('</ul>', '<li class="picker-item"><span>人工报价</span></li></ul>'));
    await assert.rejects(select(), /匹配多个行/);
  } finally { await browser.close(); }
});
