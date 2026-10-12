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
    const html = '<meta charset="utf-8"><button>发送图片</button><button>发送文件</button><button>价格表</button>';
    const good = await request('run_steps', { steps: [
      { ope_key: 'goto', ope_value: { url: 'data:text/html,' + encodeURIComponent(html) } },
      { ope_key: 'assert_count', ope_value: { expected: 1 },
        element: { locator_type: 'role', locator_value: 'button[name=/图片/i]' } },
      { ope_key: 'assert_count', ope_value: { expected: 1 },
        element: { locator_type: 'role', locator_value: 'button[name="发送文件"]' } },
    ] });
    assert.equal(good.state.failed, false, JSON.stringify(good.state.step_results.map(({ screenshot, ...step }) => step)));
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
