const { test } = require('node:test');
const assert = require('node:assert/strict');
const { chromium } = require('playwright');
const { assertTableColumnEquals } = require('../lib/helpers');
const plain = (rows, id = 'orders') => `<table id="${id}"><thead><tr><th>工单号</th><th>工单类型</th></tr></thead><tbody>${rows.map(values => `<tr>${values.map(value => `<td>${value}</td>`).join('')}</tr>`).join('')}</tbody></table>`;
const calendar = '<table class="el-date-table"><tbody><tr><td>1</td><td>8</td></tr><tr><td>15</td><td>22</td></tr></tbody></table>';

test('real table assertions exclude calendars and reject ambiguous/misaligned targets', async () => {
  const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
  const page = await browser.newPage();
  try {
    await page.setContent(plain([['100', '人工报价'], ['101', '人工报价']]) + calendar);
    assert.ok((await page.locator('tbody tr td:nth-child(2)').allTextContents()).includes('8'));
    assert.equal((await assertTableColumnEquals(page, '工单类型', '人工报价')).status, 'pass');
    await page.setContent(plain([['100', '人工报价'], ['101', '运价查询']]) + calendar);
    const mismatch = await assertTableColumnEquals(page, '工单类型', '人工报价');
    assert.equal(mismatch.status, 'fail');
    assert.equal(mismatch.mismatchCount, 1);
    assert.match(mismatch.message, /第2行=运价查询/);
    await page.setContent(plain([['100', '人工报价']]) + plain([['101', '运价查询']], 'other'));
    assert.equal((await assertTableColumnEquals(page, '工单类型', '人工报价')).status, 'unverifiable');
    assert.equal((await assertTableColumnEquals(page, '工单类型', '人工报价', { tableSelector: '#orders' })).status, 'pass');
    await page.setContent(plain([]) + calendar);
    assert.equal((await assertTableColumnEquals(page, '工单类型', '人工报价')).status, 'unverifiable');
    await page.setContent(plain([['100', '人工报价', 'extra']]));
    assert.equal((await assertTableColumnEquals(page, '工单类型', '人工报价')).status, 'unverifiable');
    await page.setContent(plain([['100', '人工报价']]));
    assert.equal((await assertTableColumnEquals(page, '优先级', '高')).status, 'unverifiable');
    await page.setContent(`<div class="el-table"><div class="el-table__header-wrapper"><table class="el-table__header"><thead><tr><th>工单号</th><th>工单类型</th></tr></thead></table></div><div class="el-table__header-wrapper is-hidden" style="display:none"><table class="el-table__header"><thead><tr><th>other</th></tr></thead></table></div><div class="el-table__body-wrapper"><table class="el-table__body"><tbody><tr><td>100</td><td>人工报价</td></tr></tbody></table></div></div>${calendar}`);
    const result = await assertTableColumnEquals(page, '工单类型', '人工报价');
    assert.equal(result.status, 'pass');
    assert.equal(result.rowCount, 1);
  } finally { await browser.close(); }
});
