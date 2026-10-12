'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { assertLocatorCount, parseExpectedCount, matchesPageUrl } = require('./step_assertions');

test('count assertion accepts an empty result without waiting for visibility', async () => {
  const locator = { count: async () => 0 };
  assert.equal(await assertLocatorCount(locator, '0', { timeoutMs: 5 }), 0);
});

test('count assertion waits until the expected number of rows appears', async () => {
  let current = 0;
  const locator = { count: async () => current };
  const result = await assertLocatorCount(locator, 2, {
    timeoutMs: 100,
    pollIntervalMs: 1,
    wait: async () => { current = 2; },
  });
  assert.equal(result, 2);
});

test('count assertion reports a mismatch after its bounded timeout', async () => {
  const locator = { count: async () => 1 };
  await assert.rejects(
    assertLocatorCount(locator, 0, {
      timeoutMs: 2,
      pollIntervalMs: 1,
      wait: async () => {},
    }),
    /预期元素数量=0，实际=1/,
  );
});

test('count assertion rejects invalid expectations', () => {
  assert.throws(() => parseExpectedCount('-1'), /非负整数/);
  assert.throws(() => parseExpectedCount('many'), /非负整数/);
});

test('URL contains matches detail routes without matching their list route', () => {
  global.window = { location: { href: 'https://test.bot.by56.com/work-order/tickets' } };
  const expectedUrl = 'https://test.bot.by56.com/work-order/tickets/';
  assert.equal(matchesPageUrl({ expectedUrl, mode: 'contains' }), false);
  global.window.location.href = 'https://test.bot.by56.com/work-order/tickets/5457';
  assert.equal(matchesPageUrl({ expectedUrl, mode: 'contains' }), true);
  delete global.window;
});

test('URL path matching ignores query and trailing slash', () => {
  global.window = { location: { href: 'https://example.test/tickets/?page=2' } };
  assert.equal(matchesPageUrl({ expectedUrl: 'https://example.test/tickets', mode: 'path' }), true);
  delete global.window;
});
