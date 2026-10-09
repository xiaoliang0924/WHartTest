'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const readline = require('node:readline');
const path = require('node:path');

test('real recorder preserves database references through synthetic navigation and iframe operations', async () => {
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
    const html = '<iframe id="fixture-frame" srcdoc="<input id=field>"></iframe>';
    const result = await request('run_steps', { steps: [
      { ope_key: 'goto', ope_value: { url: 'data:text/html,' + encodeURIComponent(html) }, synthetic: true },
      { detail_id: 103, element_id: 503, ope_key: 'fill', ope_value: { value: 'fixture-text' },
        element: { locator_type: 'id', locator_value: 'field', is_iframe: true, iframe_locator: '#fixture-frame' } },
      { detail_id: 104, element_id: 504, ope_key: 'assert_count', ope_value: { expected: 2, timeout: 1 },
        element: { locator_type: 'id', locator_value: 'fixture-frame' } },
    ] });
    assert.equal(result.state.failed, true);
    const [navigation, fill, failed] = result.state.step_results;
    assert.equal(navigation.synthetic, true);
    assert.equal(navigation.detail_id, null);
    assert.equal(fill.status, 'success', fill.message);
    assert.equal(fill.detail_id, 103);
    assert.equal(fill.element_id, 503);
    assert.equal(fill.ope_key, 'fill');
    assert.equal(failed.status, 'failed');
    assert.equal(failed.detail_id, 104);
    assert.equal(failed.element_id, 504);
    assert.equal(failed.ope_key, 'assert_count');
    assert.ok(failed.screenshot);
  } finally {
    try { await request('close'); } finally { lines.close(); child.kill('SIGKILL'); }
  }
});
