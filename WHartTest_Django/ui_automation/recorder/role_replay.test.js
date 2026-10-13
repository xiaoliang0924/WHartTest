'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const readline = require('node:readline');
const path = require('node:path');

test('real recorder named-role count assertions detect existing buttons', async () => {
  assert.ok(process.env.PLAYWRIGHT_SKILL_DIR, 'Set PLAYWRIGHT_SKILL_DIR to the fixture Playwright installation');
  const child = spawn(process.execPath, [path.join(__dirname, 'recorder_server.js'),
    '--skill-dir', process.env.PLAYWRIGHT_SKILL_DIR], { env: { ...process.env, HEADLESS: 'true' } });
  const pending = new Map();
  let sequence = 0;
  let stderr = '';
  child.stderr.on('data', chunk => { stderr = (stderr + chunk).slice(-4000); });
  const lines = readline.createInterface({ input: child.stdout });
  lines.on('line', line => {
    let data;
    try { data = JSON.parse(line); } catch (_) { return; }
    const callback = pending.get(data.id);
    if (callback) { pending.delete(data.id); callback(data); }
  });
  const request = (method, params = {}) => new Promise((resolve, reject) => {
    const id = String(++sequence);
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`${method} timed out: ${stderr}`)); }, 40000);
    pending.set(id, result => { clearTimeout(timer); resolve(result); });
    child.stdin.write(JSON.stringify({ id, method, params }) + '\n');
  });
  try {
    const started = await request('start', { url: 'about:blank', viewport: { width: 800, height: 600 } });
    assert.equal(started.ok, true, started.error);
    const html = '<meta charset="utf-8"><button>发送图片</button><button>发送文件</button><button>价格表</button><button>确定（1）</button>';
    const good = await request('run_steps', { steps: [
      { ope_key: 'goto', ope_value: { url: 'data:text/html,' + encodeURIComponent(html) } },
      { ope_key: 'assert_count', ope_value: { expected: 1 },
        element: { locator_type: 'role', locator_value: 'button[name=/图片/i]' } },
      { ope_key: 'assert_count', ope_value: { expected: 1 },
        element: { locator_type: 'role', locator_value: 'button[name="发送文件"]' } },
    ] });
    assert.equal(good.state.failed, false, JSON.stringify(good.state.step_results.map(({ screenshot, ...step }) => step)));
    const dynamicCount = await request('run_steps', { steps: [
      { ope_key: 'click', element: { locator_type: 'role', locator_value: 'button[name="确定"]' } },
    ] });
    assert.equal(dynamicCount.state.failed, false,
      JSON.stringify(dynamicCount.state.step_results.map(({ screenshot, ...step }) => step)));
    const announcementHtml = '<meta charset="utf-8"><button id="business-action" onclick="document.body.dataset.clicked=\'yes\'">业务操作</button>' +
      '<div class="el-overlay" style="position:fixed;inset:0;background:#0008;z-index:1000">' +
      '<div class="el-modal-dialog" role="dialog" style="position:absolute;inset:20% 15%;background:white;padding:24px">' +
      '<p>发现新版本：新增代班转接功能</p><button onclick="this.closest(\'.el-overlay\').remove()">我知道了</button></div></div>';
    const noticeReplay = await request('run_steps', { steps: [
      { ope_key: 'goto', ope_value: { url: 'data:text/html,' + encodeURIComponent(announcementHtml) } },
      { ope_key: 'click', element: { locator_type: 'role', locator_value: 'button[name="业务操作"]' } },
    ] });
    assert.equal(noticeReplay.state.failed, false,
      JSON.stringify(noticeReplay.state.step_results.map(({ screenshot, ...step }) => step)));
    const noticeOutcome = await request('eval', { code: '({overlay:!!document.querySelector(".el-overlay"), clicked:document.body.dataset.clicked})' });
    assert.deepEqual(noticeOutcome.state.eval.result, { overlay: false, clicked: 'yes' });
    const legacySelectHtml = '<meta charset="utf-8"><style>.el-select-dropdown{display:none}</style>' +
      '<div class="filter-field"><label>工单状态</label><div><div class="el-select">' +
      '<div class="el-input"><div class="el-input__wrapper" onclick="document.querySelector(\'.el-select-dropdown\').style.display=\'block\'"><input role="combobox" readonly><span class="el-select__selected-item">全部</span></div></div></div></div></div>' +
      '<div class="el-select-dropdown"><li class="el-select-dropdown__item" onclick="document.querySelector(\'.el-select__selected-item\').textContent=\'待处理\';this.parentElement.style.display=\'none\'">待处理</li></div>' +
      '<div class="el-modal-dialog" role="dialog" style="position:fixed;inset:0;background:#0008;z-index:1000"><p>发现新版本</p><button onclick="this.parentElement.remove()">我知道了</button></div>';
    const legacySelectReplay = await request('run_steps', { steps: [
      { ope_key: 'goto', ope_value: { url: 'data:text/html,' + encodeURIComponent(legacySelectHtml) } },
      { ope_key: 'select_filter_option', ope_value: { value: '待处理' },
        element: { locator_type: 'xpath', locator_value: "//label[normalize-space()='工单状态']/following-sibling::div//input[@role='combobox']" } },
    ] });
    assert.equal(legacySelectReplay.state.failed, false,
      JSON.stringify(legacySelectReplay.state.step_results.map(({ screenshot, ...step }) => step)));
    const legacySelectOutcome = await request('eval', { code: 'document.querySelector(".el-select__selected-item").textContent.trim()' });
    assert.equal(legacySelectOutcome.state.eval.result, '待处理');
    const dropdownNoticeOutcome = await request('eval', { code: '!!document.querySelector(".el-modal-dialog")' });
    assert.equal(dropdownNoticeOutcome.state.eval.result, false);
    const readonlyClickHtml = '<meta charset="utf-8"><div class="el-select"><div class="el-input__wrapper" onclick="document.body.dataset.wrapperClicked=\'yes\'"><input id="readonly-combobox" role="combobox" readonly></div></div>';
    const readonlyClickReplay = await request('run_steps', { steps: [
      { ope_key: 'goto', ope_value: { url: 'data:text/html,' + encodeURIComponent(readonlyClickHtml) } },
      { ope_key: 'click', element: { locator_type: 'css', locator_value: '#readonly-combobox' } },
    ] });
    assert.equal(readonlyClickReplay.state.failed, false,
      JSON.stringify(readonlyClickReplay.state.step_results.map(({ screenshot, ...step }) => step)));
    const readonlyClickOutcome = await request('eval', { code: 'document.body.dataset.wrapperClicked' });
    assert.equal(readonlyClickOutcome.state.eval.result, 'yes');
    const antSelectHtml = '<meta charset="utf-8"><div class="ant-select"><div class="ant-select-selector" onclick="document.querySelector(\'[role=option]\').style.display=\'block\'"><span class="ant-select-selection-item">全部</span></div></div>' +
      '<div role="option" style="display:none" onclick="document.querySelector(\'.ant-select-selection-item\').textContent=\'待处理\';this.style.display=\'none\'">待处理</div>';
    const antSelectReplay = await request('run_steps', { steps: [
      { ope_key: 'goto', ope_value: { url: 'data:text/html,' + encodeURIComponent(antSelectHtml) } },
      { ope_key: 'select_filter_option', ope_value: { value: '待处理' },
        element: { locator_type: 'css', locator_value: '.ant-select' } },
    ] });
    assert.equal(antSelectReplay.state.failed, false,
      JSON.stringify(antSelectReplay.state.step_results.map(({ screenshot, ...step }) => step)));
    const antSelectOutcome = await request('eval', { code: 'document.querySelector(".ant-select-selection-item").textContent.trim()' });
    assert.equal(antSelectOutcome.state.eval.result, '待处理');
    const nativeSelectHtml = '<meta charset="utf-8"><select id="status"><option value="all">全部</option><option value="pending">待处理</option></select><button>价格表</button>';
    const nativeSelectReplay = await request('run_steps', { steps: [
      { ope_key: 'goto', ope_value: { url: 'data:text/html,' + encodeURIComponent(nativeSelectHtml) } },
      { ope_key: 'select_filter_option', ope_value: { value: '待处理' },
        element: { locator_type: 'css', locator_value: '#status' } },
    ] });
    assert.equal(nativeSelectReplay.state.failed, false,
      JSON.stringify(nativeSelectReplay.state.step_results.map(({ screenshot, ...step }) => step)));
    const nativeSelectOutcome = await request('eval', { code: 'document.querySelector("#status").value' });
    assert.equal(nativeSelectOutcome.state.eval.result, 'pending');
    const bad = await request('run_steps', { steps: [
      { detail_id: 201, ope_key: 'assert_count', ope_value: { expected: 0 },
        element: { locator_type: 'role', locator_value: 'button[name=/价格表/i]' } },
    ] });
    assert.equal(bad.state.failed, true);
    assert.equal(bad.state.step_results[0].detail_id, 201);
    assert.match(bad.state.step_results[0].message, /实际=1/);
  } finally {
    try { await request('close'); } finally { lines.close(); child.kill('SIGKILL'); }
  }
});
